"""Bounded subprocess execution with cancellation and process-tree cleanup."""
import os
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from errors import ProcessCancelled, ProcessFailed, ProcessTimedOut
from files import regular_file


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
    """Use files instead of pipes so output/stdin cannot bypass the deadline."""
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
        process = subprocess.Popen(argv, cwd=cwd, stdin=incoming if stdin is not None else subprocess.DEVNULL,
                                   stdout=outgoing, stderr=subprocess.STDOUT if merge_stderr else errors, start_new_session=True,
                                   env=env, umask=0o077)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                check_cancel()
                if time.monotonic() >= deadline:
                    raise ProcessTimedOut(f"{Path(argv[0]).name} timed out. You can try again.")
                time.sleep(0.05)
            check_cancel()
            if check and process.returncode:
                detail = f" Details: {log}" if log else ""
                raise ProcessFailed(f"{Path(argv[0]).name} failed (exit {process.returncode})." + detail)
            outgoing.seek(0)
            # Keep discovery/image metadata bounded; full worker output stays in its log.
            text = outgoing.read(4 * 1024 * 1024).decode(errors="replace")
            if errors is not None:
                errors.seek(0)
            detail = errors.read(4 * 1024 * 1024).decode(errors="replace") if errors is not None else ""
            return subprocess.CompletedProcess(argv, process.returncode, stdout=text, stderr=detail)
        finally:
            stop_process_group(process)
