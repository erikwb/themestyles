"""Exercise the actual plugin and native dropdowns in an invisible test shell."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "shell"


@unittest.skipUnless(shutil.which("quickshell") and os.environ.get("WAYLAND_DISPLAY")
                     and (SHELL / "Ui/Dropdown.qml").is_file(), "Native picker test requires Omarchy on Wayland")
class PanelTests(unittest.TestCase):
    def test_picker_selection_survives_native_control_assignments(self):
        with tempfile.TemporaryDirectory(prefix="theme-styles-picker-") as temporary:
            root = Path(temporary)
            for name in ("Ui", "Commons"):
                (root / name).symlink_to(SHELL / name, target_is_directory=True)
            (root / "plugin").mkdir()
            shutil.copyfile(SOURCE / "Panel.qml", root / "plugin/Panel.qml")
            shutil.copyfile(SOURCE / "tests/picker_regression.qml", root / "shell.qml")
            # All panel subprocesses go to this fixture. No account discovery,
            # preferences, desktop changes, or image generation can run.
            helper = root / "plugin/theme-styles"
            helper.write_text('#!/usr/bin/env python3\nprint(\'{"ok": false, "error": "Test fixture"}\')\n')
            helper.chmod(0o755)
            env = os.environ.copy()
            env["QT_QPA_PLATFORM"] = "wayland"
            result = subprocess.run(["quickshell", "--path", str(root / "shell.qml"), "--no-color"],
                                    env=env, capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PICKER REGRESSION PASSED", output)
            self.assertNotIn("PICKER REGRESSION FAILED", output)
