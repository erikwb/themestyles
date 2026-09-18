"""Attack the actual process/filesystem boundaries without calling an image API."""
import os
import stat
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import support as fixtures
from support import StyleFixture

from files import harden_tree, regular_file
from security import POLICY, convert_image, sandbox
from theme_styles import GenerationError


class SecurityTests(StyleFixture, unittest.TestCase):
    def job_workspace(self):
        job = self.request()
        return job, self.service.root("alpha") / "jobs" / job["id"]

    def test_log_open_rejects_symlink_hardlink_and_fifo_before_truncating(self):
        victim = self.home / "victim"
        victim.write_text("untouched")
        log = self.home / "agent.log"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    log.symlink_to(victim)
                elif kind == "hardlink":
                    os.link(victim, log)
                else:
                    os.mkfifo(log)
                with self.assertRaises((OSError, ValueError)):
                    self.service.run_process([sys.executable, "-c", "print('overwrite')"],
                                             self.home, log.name, timeout=2)
                self.assertEqual(victim.read_text(), "untouched")
                log.unlink()

    def test_aether_ignores_preplanted_render_symlink(self):
        job, workspace = self.job_workspace()
        outside = self.home / "outside"
        outside.mkdir()
        (workspace / "rendered").symlink_to(outside)
        image = workspace / "wallpaper.png"
        fixtures.png(image)
        rendered, _ = self.service.render(job, workspace, image)
        self.assertTrue((rendered / "colors.toml").is_file())
        self.assertEqual(list(outside.iterdir()), [])

    def test_palette_comments_and_arbitrary_strings_do_not_reach_agent(self):
        job, workspace = self.job_workspace()
        palette = self.service.root("alpha") / "original/theme/colors.toml"
        palette.write_text(palette.read_text() + '\n# EVIL_COMMENT\nEVIL_KEY = "#123456"\ncolor1 = "EVIL_VALUE"\n')
        with patch.object(self.service, "run_process"), self.assertRaises(GenerationError):
            self.service.generate_image(job, workspace)
        prompt = (workspace / "agent/prompt.txt").read_text()
        self.assertNotIn("EVIL_", prompt)
        self.assertIn("#121822", prompt)

    def test_agent_output_links_and_special_files_are_rejected(self):
        job, workspace = self.job_workspace()
        output = workspace / "agent/wallpaper.png"
        victim = self.home / "victim.png"
        fixtures.png(victim)
        original = victim.read_bytes()
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    output.symlink_to(victim)
                elif kind == "hardlink":
                    os.link(victim, output)
                else:
                    os.mkfifo(output)
                with patch.object(self.service, "run_process"), self.assertRaises(GenerationError) as error:
                    self.service.generate_image(job, workspace)
                self.assertEqual(error.exception.code, "invalid_image")
                self.assertEqual(victim.read_bytes(), original)
                output.unlink()

    def test_unsafe_failure_report_is_not_read(self):
        job, workspace = self.job_workspace()
        report = workspace / "agent/failure.json"
        os.mkfifo(report)
        self.assertIsNone(self.service.image_failure(job, workspace, reported_only=True))
        report.unlink()
        victim = self.home / "secret.json"
        victim.write_text('{"error_code":"unsupported"}')
        report.symlink_to(victim)
        self.assertIsNone(self.service.image_failure(job, workspace, reported_only=True))

    def test_real_agent_cannot_read_home_write_backend_or_change_inputs(self):
        job, workspace = self.job_workspace()
        secret = self.home / ".ssh/id_ed25519"
        secret.parent.mkdir()
        secret.write_text("private")
        config = self.home / ".codex"
        config.mkdir()
        (config / "auth.json").write_text("fake login")
        attack = self.home / "fake-agent"
        attack.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
home = Path(os.environ["HOME"])
assert not (home / ".ssh/id_ed25519").exists()
assert "SSH_AUTH_SOCK" not in os.environ
assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ
assert "UNRELATED_SECRET" not in os.environ
assert "GITHUB_TOKEN" not in os.environ
assert (home / ".codex/auth.json").read_text() == "fake login"
(home / ".codex/auth.json").write_text("private sandbox change")
workspace = Path.cwd()
assert not (workspace.parent / "request.json").exists()
reference = next(workspace.glob("reference.*"))
try:
    reference.write_bytes(b"replace")
except OSError:
    pass
else:
    raise AssertionError("reference was writable")
try:
    Path("prompt.txt").unlink()
except OSError:
    pass
else:
    raise AssertionError("prompt was replaceable")
# Even a guessed backend path refers only to the container's private root.
(workspace.parent / "aether.log").symlink_to(home / ".ssh/id_ed25519")
(workspace / "wallpaper.png").write_bytes(reference.read_bytes())
''')
        attack.chmod(0o700)
        with patch("agents.safe_binary", return_value=str(attack)), \
                patch.dict(os.environ, {"UNRELATED_SECRET": "private", "GITHUB_TOKEN": "unrelated"}):
            image = self.service.generate_image(job, workspace)
        self.assertTrue(image.is_file())
        self.assertFalse((workspace / "aether.log").exists())
        self.assertEqual((config / "auth.json").read_text(), "fake login")
        self.assertEqual(secret.read_text(), "private")

    def test_private_permissions_upgrade_does_not_follow_links(self):
        root = self.service.store.data
        root.chmod(0o755)
        nested = root / "old-job"
        nested.mkdir(mode=0o755)
        log = nested / "agent.log"
        log.write_text("private prompt")
        log.chmod(0o644)
        outside = self.home / "external"
        outside.write_text("outside")
        outside.chmod(0o644)
        (nested / "link").symlink_to(outside)
        harden_tree(root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        with regular_file(nested / "new.log", write=True) as handle:
            handle.write(b"private")
        self.assertEqual(stat.S_IMODE((nested / "new.log").stat().st_mode), 0o600)

    def test_detached_agent_child_cannot_survive_the_sandbox(self):
        job, workspace = self.job_workspace()
        attack = self.home / "fake-agent"
        attack.write_text('''#!/usr/bin/env python3
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(1); Path('escaped').touch()"], start_new_session=True)
Path("failure.json").write_text('{"error_code":"unsupported"}')
''')
        attack.chmod(0o700)
        with patch("agents.safe_binary", return_value=str(attack)), self.assertRaises(GenerationError):
            self.service.generate_image(job, workspace)
        # Wait outside the sandbox longer than the child's scheduled write.
        subprocess.run([sys.executable, "-c", "import time; time.sleep(1.2)"], check=True)
        self.assertFalse((workspace / "agent/escaped").exists())

    def test_image_sandbox_has_no_host_network_namespace(self):
        argv, env = sandbox([sys.executable, "-c", "import os; print(os.readlink('/proc/self/ns/net'))"],
                            Path("/image-home"), self.home, writable=[self.home])
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5, check=True)
        self.assertNotEqual(result.stdout.strip(), os.readlink("/proc/self/ns/net"))

    def test_image_policy_rejects_vectors_and_excessive_dimensions(self):
        source = self.home / "image.png"
        destination = self.home / "normalized.png"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="256"/>')
        with self.assertRaises(ValueError):
            convert_image(source, destination)
        fixtures.png(source)
        content = bytearray(source.read_bytes())
        content[16:20] = struct.pack("!I", 100000)
        content[29:33] = struct.pack("!I", zlib.crc32(content[12:29]))
        source.write_bytes(content)
        with self.assertRaises(ValueError):
            convert_image(source, destination)
        self.assertFalse(destination.exists())

    def test_policy_blocks_delegate_coders_even_with_explicit_format(self):
        source = self.home / "payload.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        argv, env = sandbox(["magick", "SVG:" + str(source), "PNG:" + str(self.home / "out.png")],
                            Path("/image-home"), self.home, readonly=[POLICY], writable=[self.home],
                            env={"MAGICK_CONFIGURE_PATH": str(POLICY.parent)})
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("security policy", result.stderr)

    def test_decoder_cannot_return_a_symlink_as_a_valid_image(self):
        source = self.home / "input.png"
        fixtures.png(source)
        victim = self.home / "victim.png"
        fixtures.png(victim)
        destination = self.home / "wallpaper.png"
        def malicious_decoder(argv, **_kwargs):
            work = Path(argv[argv.index("--chdir") + 1])
            (work / "wallpaper.png").symlink_to(victim)
            return subprocess.CompletedProcess(argv, 0, "320 256", "")
        with patch("security.run", side_effect=malicious_decoder), self.assertRaises(OSError):
            convert_image(source, destination)
        self.assertFalse(destination.exists())

    def test_aether_only_returns_literal_colors_to_native_templates(self):
        job, workspace = self.job_workspace()
        image = workspace / "wallpaper.png"
        fixtures.png(image)
        def malicious_aether(argv, *_args, **_kwargs):
            output = Path(argv[argv.index("--output") + 1])
            (output / "colors.toml").write_text(fixtures.COLORS + '\ncolor1 = "malicious command"\n')
        with patch.object(self.service, "run_process", side_effect=malicious_aether):
            rendered, _ = self.service.render(job, workspace, image)
        self.assertNotIn("malicious", (rendered / "colors.toml").read_text())

    def test_missing_sandbox_never_runs_harness(self):
        job, workspace = self.job_workspace()
        with patch("security.shutil.which", return_value=None), patch.object(self.service, "run_process") as run:
            with self.assertRaisesRegex(ValueError, "bubblewrap"):
                self.service.generate_image(job, workspace)
        run.assert_not_called()
