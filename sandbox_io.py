"""Retain a bounded sandbox's output mount, then import only named regular files."""
import json
import os
import select
import tempfile
from pathlib import Path

from files import regular_file

# Bubblewrap closes setup descriptors before exec. A read-only gate inode lets
# the parent retain the tmpfs before even a very short-lived command starts.
GATE = "/.theme-styles-gate"
WAIT_FOR_PARENT = (
    "import os,sys,time\n"
    f"with open({GATE!r}, 'rb', buffering=0) as gate:\n"
    " while not gate.read(1): time.sleep(0.01)\n"
    "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n"
)


class SandboxCommand(list):
    def __init__(self, command, directory, outputs):
        super().__init__(command)
        self.directory = Path(directory)
        self.outputs = outputs


class SandboxOutput:
    def __init__(self, command, stack):
        self.command = command
        self.gate = stack.enter_context(tempfile.NamedTemporaryFile(prefix="theme-styles-gate-"))
        self.info = stack.enter_context(tempfile.TemporaryFile())
        self.directory_fd = None
        self.pid_fd = None
        stack.callback(self.close)
        self.pass_fds = (self.info.fileno(),)
        split = command.index("--")
        self.argv = [*command[:split], "--info-fd", str(self.info.fileno()),
                     "--ro-bind", self.gate.name, GATE, "--",
                     "/usr/bin/python3", "-c", WAIT_FOR_PARENT, *command[split + 1:]]

    def start(self):
        """Called while polling; only release the command after pinning its mount."""
        if self.directory_fd is not None:
            return
        self.info.seek(0)
        try:
            pid = json.loads(self.info.read(4096))["child-pid"]
        except (ValueError, KeyError):
            return  # Bubblewrap hasn't finished publishing its setup information.
        try:
            root = os.open(f"/proc/{int(pid)}/root", os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return
        try:
            try:
                marker = os.stat(GATE.lstrip("/"), dir_fd=root)
            except FileNotFoundError:
                return  # info-fd is published before pivot_root.
            expected = os.fstat(self.gate.fileno())
            if (marker.st_dev, marker.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("Could not verify the sandbox output filesystem.")
            self.directory_fd = os.open(str(self.command.directory).lstrip("/"),
                                        os.O_RDONLY | os.O_DIRECTORY, dir_fd=root)
            self.pid_fd = os.pidfd_open(int(pid))
            self.gate.write(b"1")
            self.gate.flush()
        finally:
            os.close(root)

    def collect(self):
        """The process tree must be dead before inspecting untrusted files."""
        if self.directory_fd is None:
            return
        if self.pid_fd is None or not select.select([self.pid_fd], [], [], 2)[0]:
            raise ValueError("Sandbox processes did not stop before output import.")
        source = Path(f"/proc/self/fd/{self.directory_fd}")
        for name, limit in self.command.outputs.items():
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid sandbox output name.")
            try:
                with (regular_file(source / name, limit=limit) as handle,
                      regular_file(self.command.directory / name, write=True) as target):
                    remaining = limit
                    while chunk := handle.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError("Sandbox output exceeds its size limit.")
                        target.write(chunk)
            except FileNotFoundError:
                continue

    def close(self):
        if self.directory_fd is not None:
            os.close(self.directory_fd)
        if self.pid_fd is not None:
            os.close(self.pid_fd)
