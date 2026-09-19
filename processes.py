"""Bounded subprocess execution with cancellation and process-tree cleanup."""
import os
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from errors import ProcessCancelled, ProcessFailed, ProcessOutputLimit, ProcessTimedOut
from files import regular_file
from sandbox_io import SandboxCommand, SandboxOutput

MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def process_start(pid):
    try:
        # Everything after the final ')' starts at proc stat field 3.
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return ""


def stop_process_group(process):
    """Reap the harness and terminate its remaining tools, even if it exited."""
    def send(sig):
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            return False

    if send(signal.SIGTERM):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            process.poll()
            if not send(0):
                break
            time.sleep(0.05)
        # Waiting only for the parent misses tools which ignore SIGTERM.
        send(signal.SIGKILL)
    process.wait()



def run(argv, *, cwd=None, timeout, stdin=None, env=None, log=None, cancel=None, check=True, merge_stderr=True):
    """Bound captured output while enforcing cancellation and process deadlines."""
    def check_cancel():
        if cancel is not None and Path(cancel).exists():
            raise ProcessCancelled("Generation cancelled.")

    check_cancel()
    with ExitStack() as stack:
        incoming = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        outgoing = stack.enter_context(regular_file(log, write=True) if log and merge_stderr else tempfile.TemporaryFile())
        errors = None if merge_stderr else stack.enter_context(regular_file(log, write=True) if log else tempfile.TemporaryFile())
        if stdin is not None:
            incoming.write(stdin.encode() if isinstance(stdin, str) else stdin)
            incoming.seek(0)
        output = SandboxOutput(argv, stack) if isinstance(argv, SandboxCommand) else None
        selector = stack.enter_context(selectors.DefaultSelector())
        process = subprocess.Popen(output.argv if output else argv, cwd=cwd,
                                   stdin=incoming if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                                   start_new_session=True, env=env, umask=0o077,
                                   pass_fds=output.pass_fds if output else ())
        for pipe, destination in ((process.stdout, outgoing), (process.stderr, errors)):
            if pipe is not None:
                stack.enter_context(pipe)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, destination)
        deadline = time.monotonic() + timeout
        written = 0
        stopped = False

        def stop():
            nonlocal stopped
            if not stopped:
                stop_process_group(process)
                stopped = True

        try:
            while selector.get_map() or process.poll() is None:
                check_cancel()
                if time.monotonic() >= deadline:
                    raise ProcessTimedOut(f"{Path(argv[0]).name} timed out. You can try again.")
                if output:
                    output.start()
                if process.poll() is not None:
                    stop()  # Descendants must not keep output pipes alive indefinitely.
                for key, _events in selector.select(timeout=0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = MAX_LOG_BYTES - written
                    key.data.write(chunk[:remaining])
                    written += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        detail = f" Details: {log}" if log else ""
                        raise ProcessOutputLimit("Process stopped because its log output exceeded "
                                                 f"{MAX_LOG_BYTES // (1024 * 1024)} MiB." + detail)
            check_cancel()
            stop()
            if output:
                output.collect()
            if check and process.returncode:
                detail = f" Details: {log}" if log else ""
                raise ProcessFailed(f"{Path(argv[0]).name} failed (exit {process.returncode})." + detail)
            outgoing.seek(0)
            text = outgoing.read(MAX_CAPTURE_BYTES).decode(errors="replace")
            if errors is not None:
                errors.seek(0)
            detail = errors.read(MAX_CAPTURE_BYTES).decode(errors="replace") if errors is not None else ""
            return subprocess.CompletedProcess(argv, process.returncode, stdout=text, stderr=detail)
        finally:
            stop()
