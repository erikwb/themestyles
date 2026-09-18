"""Exercise real local processes; no agent accounts or desktop state are used."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest

from theme_styles import Styles, StylesError


class ProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.service = Styles(home=self.workspace, data=self.workspace / "data")

    def test_large_stdin_reaches_harness_intact(self):
        text = "snow 🌨\n" * 100000
        self.service.run_process(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
            self.workspace, "agent.log", timeout=5, stdin=text)
        self.assertEqual((self.workspace / "agent.log").read_text().strip(), str(len(text)))

    def test_timeout_and_cancel_work_when_harness_does_not_read_stdin(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                marker = self.workspace / "cancel"
                timer = threading.Timer(0.1, marker.touch) if cancel else None
                if timer:
                    timer.start()
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(StylesError, "cancelled" if cancel else "timed out"):
                        self.service.run_process(
                            [sys.executable, "-c", "import time; time.sleep(2)"],
                            self.workspace, "agent.log", timeout=5 if cancel else 0.1, stdin="x" * 1000000)
                    self.assertLess(time.monotonic() - started, 1.5)
                finally:
                    if timer:
                        timer.join()
                    marker.unlink(missing_ok=True)

    def test_tools_ignoring_sigterm_stop_even_after_harness_exits(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                child_code = """import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('child.pid').write_text(str(os.getpid()))
time.sleep(30)
"""
                parent_code = (
                    "import subprocess, sys, time\nfrom pathlib import Path\n"
                    f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                    "while not Path('child.pid').exists(): time.sleep(0.01)\n"
                    + ("Path('cancel').touch()\ntime.sleep(30)\n" if cancel else ""))
                try:
                    if cancel:
                        with self.assertRaisesRegex(StylesError, "cancelled"):
                            self.service.run_process([sys.executable, "-c", parent_code],
                                                     self.workspace, "agent.log", timeout=5)
                    else:
                        self.service.run_process([sys.executable, "-c", parent_code],
                                                 self.workspace, "agent.log", timeout=5)
                    pid = int((self.workspace / "child.pid").read_text())
                    # An orphan may briefly be a zombie until init reaps it.
                    deadline = time.monotonic() + 1
                    while self.running(pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(self.running(pid), "Image tool survived harness cleanup")
                finally:
                    path = self.workspace / "child.pid"
                    if path.exists():
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        path.unlink()
                    (self.workspace / "cancel").unlink(missing_ok=True)

    @staticmethod
    def running(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
