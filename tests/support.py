"""Temporary theme fixtures shared by integration and security tests."""
import os
import shutil
import struct
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

from agents import Agents, model
from theme_styles import Styles

CATALOG = [Agents.entry("codex", "Codex", [model("image-agent", "Image Agent", ["low", "high"], "low")],
                       "image-agent", "high"),
           Agents.entry("grok", "Grok", [model("grok-image", "Grok Image", ["low", "high"], "high")],
                        "grok-image", "")]


COLORS = 'mode = "dark"\nbackground = "#121822"\nforeground = "#eeeeff"\naccent = "#88aaff"\n'


def png(path, color=(30, 60, 90)):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    pixels = b"".join(b"\x00" + bytes((c + x // 4 + y // 4) % 256
                      for x in range(320) for c in color) for y in range(256))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 320, 256, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class StyleFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environment = patch.dict(os.environ, {"HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_DATA_HOME": str(self.home / ".local/share"),
            "CODEX_HOME": str(self.home / ".codex"), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "PI_CODING_AGENT_DIR": str(self.home / ".pi/agent"), "COPILOT_HOME": str(self.home / ".copilot"),
            "HERMES_HOME": str(self.home / ".hermes")})
        environment.start()
        self.addCleanup(environment.stop)
        self.service = Styles(home=self.home, data=self.home / "data")
        self.service.desktop.theme_lock = self.home / "run/theme.lock"
        self.service.desktop.omarchy = self.home / "omarchy"
        self.service.desktop.current.mkdir(parents=True)
        self.make_theme("alpha")
        self.make_theme("beta")
        self.select("alpha")
        self.calls = []
        self.service.desktop.activate = self.select
        self.service.desktop.render_templates = lambda: None
        self.service.desktop.headless = lambda: True
        # Unit tests don't require Codex to be installed.
        self.which = patch("theme_styles.shutil.which", wraps=shutil.which)
        self.which.start()
        self.addCleanup(self.which.stop)
        catalog = patch.object(self.service.agents, "catalog", return_value=CATALOG)
        catalog.start()
        self.addCleanup(catalog.stop)
        binary = patch("agents.safe_binary", return_value="/tool")
        binary.start()
        self.addCleanup(binary.stop)

    def make_theme(self, name):
        root = self.service.desktop.themes / name
        (root / "backgrounds").mkdir(parents=True)
        png(root / "backgrounds/original.png")
        (root / "colors.toml").write_text(COLORS)
        (root / "kitty.conf").write_text("old colors\n")
        (root / "extra.asset").write_text("keep this\n")

    def select(self, slug, token=""):
        if hasattr(self, "calls"):
            self.calls.append(slug)
        current = self.service.desktop.current
        if (current / "theme").exists():
            shutil.rmtree(current / "theme")
        shutil.copytree(self.service.desktop.themes / slug, current / "theme")
        (current / "theme.name").write_text(slug)
        (current / "background").unlink(missing_ok=True)
        (current / "background").symlink_to(next((current / "theme/backgrounds").iterdir()))

    def select_wallpaper(self, path):
        link = self.service.desktop.current / "background"
        link.unlink(missing_ok=True)
        link.symlink_to(path)

    def request(self, name="Winter", auto_apply=False):
        return self.service.start("alpha", "Snow and twilight", name, auto_apply=auto_apply, spawn=False)["job"]

    def complete(self, job):
        workspace = self.service.root(job["base"]) / "jobs" / job["id"]
        image = workspace / "wallpaper.png"
        png(image, (10, 20, 220))
        rendered = workspace / "rendered"
        rendered.mkdir()
        (rendered / "colors.toml").write_text(COLORS.replace("#88aaff", "#55bbcc"))
        return self.service.save_variant(job, workspace, image, rendered, "dark")

    def fake_pipeline(self):
        def image(job, workspace):
            target = workspace / "wallpaper.png"
            png(target)
            return target
        def render(job, workspace, image):
            target = workspace / "rendered"
            target.mkdir()
            (target / "colors.toml").write_text(COLORS)
            return target, "dark"
        self.service.generate_image = image
        self.service.render = render
