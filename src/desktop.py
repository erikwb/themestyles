"""Omarchy runtime activation, rollback and desktop synchronization."""
import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from errors import StylesError
from files import harden_tree, lock, read_json, validate_id, validate_slug
from processes import run
from storage import MARKER, STYLE_OWNERS

# The same app refresh helpers used by Omarchy's theme setter. Missing helpers
# on older releases are skipped; theme source directories are never changed.
REFRESH_COMMANDS = (
    "omarchy-restart-terminal", "omarchy-restart-hyprctl", "omarchy-restart-btop",
    "omarchy-restart-opencode", "omarchy-restart-helix",
    *(f"omarchy-theme-set-{app}" for app in (
        "foot", "tmux", "gnome", "pi", "claude", "hermes", "t3code", "browser",
        "vscode", "obsidian", "keyboard")),
)


class OmarchyDesktop:
    def __init__(self, home, store):
        self.home = Path(home)
        self.store = store
        self.current = self.home / ".local/state/omarchy/current"
        self.themes = self.home / ".config/omarchy/themes"
        self.omarchy = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"))
        self.theme_lock = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-theme-set.lock"

    def run_process(self, argv, workspace, log_name, **kwargs):
        return run(argv, cwd=workspace, log=workspace / log_name, **kwargs)

    def context(self):
        """Read the name, marker and wallpaper from one Omarchy transition."""
        with lock(self.theme_lock, shared=True):
            return self._context()

    def _context(self):
        try:
            name = validate_slug((self.current / "theme.name").read_text().strip())
        except FileNotFoundError as exc:
            raise StylesError("No current Omarchy theme was found.") from exc
        marker = read_json(self.current / "theme" / MARKER, {})
        base, active = name, ""
        if marker.get("plugin") in STYLE_OWNERS:
            candidate = validate_slug(marker.get("base", ""))
            if name in {candidate, self.store.slot(candidate)}:
                base, active = candidate, marker.get("style_id", "")
        try:
            wallpaper = (self.current / "background").resolve(strict=True)
        except OSError:
            wallpaper = None
        # A switch away and back also invalidates a pending auto-apply.
        parts = [name]
        for path in [self.current / "theme.name", self.current / "background", self.current / "theme/colors.toml"]:
            try:
                st = path.lstat()
                parts.extend([str(st.st_ino), str(st.st_mtime_ns)])
            except OSError:
                parts.append("missing")
        return {"base": base, "name": name, "display_name": base.replace("-", " ").title(),
                "active": active, "wallpaper": str(wallpaper) if wallpaper else "",
                "token": hashlib.sha256("|".join(parts).encode()).hexdigest()}

    def check_context(self, base, token=""):
        context = self.context()
        return self.require_context(context, base, token)

    def require_context(self, context, base, token=""):
        if context["base"] != base or (token and context["token"] != token):
            raise StylesError("The selected theme changed. Reopen Theme Styles and try again.")
        return context

    def original_mode(self, base):
        source = self.store.root(base) / "original/theme/colors.toml"
        result = subprocess.run(["omarchy-theme-color", "--file", str(source), "mode"],
                                capture_output=True, text=True, timeout=10)
        mode = result.stdout.strip()
        if result.returncode or mode not in {"light", "dark"}:
            raise StylesError("Omarchy could not determine the original theme's light/dark mode.")
        return mode

    def apply(self, base, style_id, token=""):
        validate_id(style_id)
        root = self.store.root(base)
        with lock(root / "operation.lock"):
            self.check_context(base, token)
            variant = root / "variants" / style_id
            record = self.store.style(base, style_id)
            wallpaper = variant / "theme/backgrounds/style.png"
            if not wallpaper.is_file():
                raise StylesError("This saved style's wallpaper is missing.")
            # Only the runtime copy changes. Omarchy's next normal theme set
            # recreates it from the original source, naturally clearing a style.
            with lock(self.theme_lock):
                self.require_context(self._context(), base, token)
                next_theme = self.current / "next-theme"
                if next_theme.exists() or next_theme.is_symlink():
                    raise StylesError("Omarchy has an unfinished theme change. Reapply the original theme first.")
                with tempfile.TemporaryDirectory(prefix=".theme-styles-", dir=self.current) as temporary:
                    backup = Path(temporary)
                    try:
                        shutil.copytree(variant / "theme", next_theme)
                        self.render_templates()
                        # Keep the old runtime and selection for rollback, even
                        # if writing the new selection fails halfway through.
                        for name in ("theme.name", "background"):
                            path = self.current / name
                            if path.exists() or path.is_symlink():
                                shutil.copy2(path, backup / name, follow_symlinks=False)
                        (self.current / "theme").rename(backup / "theme")
                        try:
                            next_theme.rename(self.current / "theme")
                            self.update_selection(base, wallpaper)
                        except Exception:
                            if (self.current / "theme").exists():
                                shutil.rmtree(self.current / "theme")
                            (backup / "theme").rename(self.current / "theme")
                            for name in ("theme.name", "background"):
                                if (backup / name).exists() or (backup / name).is_symlink():
                                    os.replace(backup / name, self.current / name)
                                else:
                                    (self.current / name).unlink(missing_ok=True)
                            raise
                        self.sync_shell(wallpaper)
                    finally:
                        if next_theme.exists():
                            shutil.rmtree(next_theme)
            self.refresh_apps(base)
        return {"ok": True, "message": f"Applied {record['name']}."}

    def render_templates(self):
        result = subprocess.run(["omarchy-theme-set-templates"], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise StylesError("Omarchy could not render this style: " + result.stderr.strip()[-600:])

    def update_selection(self, base, wallpaper):
        name = self.current / f".theme-name-{uuid.uuid4().hex}"
        background = self.current / f".background-{uuid.uuid4().hex}"
        try:
            name.write_text(base + "\n")
            background.symlink_to(wallpaper)
            os.replace(name, self.current / "theme.name")
            os.replace(background, self.current / "background")
        finally:
            name.unlink(missing_ok=True)
            background.unlink(missing_ok=True)

    def headless(self):
        return os.environ.get("OMARCHY_THEME_HEADLESS") == "1" or os.environ.get("OMARCHY_THEME_OFFLINE") == "1"

    def best_effort(self, argv, timeout=30):
        # Like native theme switching, an app which isn't running must not
        # prevent the remaining applications from receiving their colors.
        try:
            subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def sync_shell(self, wallpaper):
        if self.headless():
            return
        payloads = []
        for filename in ("colors.toml", "shell.toml"):
            path = self.current / "theme" / filename
            payloads.append(base64.b64encode(path.read_bytes()).decode() if path.is_file() else "")
        self.best_effort(["omarchy-shell", "background", "setInstant", str(wallpaper)], timeout=2)
        self.best_effort(["omarchy-shell", "shell", "applyTheme", *payloads], timeout=2)

    def refresh_apps(self, base):
        if self.headless():
            return
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda command: self.best_effort([command]),
                              (command for command in REFRESH_COMMANDS if shutil.which(command))))
        self.best_effort(["omarchy", "hook", "theme-set", base])
        self.best_effort(["omarchy", "theme", "bg", "cache"])

    def migrate(self):
        """Archive only our old companion themes; retain every saved variant."""
        harden_tree(self.store.data)
        archived = []
        for destination in self.themes.glob("*-styles-*"):
            if destination.is_symlink() or not destination.is_dir():
                continue
            marker = read_json(destination / MARKER, {})
            if marker.get("plugin") not in STYLE_OWNERS:
                continue
            base = validate_slug(marker.get("base", ""))
            if destination.name != self.store.slot(base):
                continue
            root = self.store.root(base)
            with lock(root / "operation.lock"), lock(self.theme_lock):
                context = self._context()
                archive = root / "legacy-themes" / uuid.uuid4().hex
                archive.parent.mkdir(parents=True, exist_ok=True)
                # The runtime is already a copy. Preserve its look while
                # restoring the original name before retiring the old entry.
                if context["name"] == destination.name:
                    wallpaper = self.current / "theme/backgrounds/style.png"
                    if not wallpaper.is_file():
                        raise StylesError("Restore the original theme before migrating this legacy style.")
                    self.update_selection(base, wallpaper)
                shutil.move(str(destination), str(archive))
                archived.append(destination.name)
        if archived and not self.headless():
            self.best_effort(["omarchy", "theme", "switcher", "--preload"])
        return {"ok": True, "archived": archived}

    def activate(self, slug, token=""):
        # Hold the desktop lock from validation through mutation. The native
        # headless setter uses a private runtime lock to avoid reacquiring ours;
        # shell IPC and app refreshes use the real session afterwards.
        with lock(self.theme_lock):
            self.require_context(self._context(), slug, token)
            if (self.current / "next-theme").exists() or (self.current / "next-theme").is_symlink():
                raise StylesError("Omarchy has an unfinished theme change. Reapply the original theme first.")
            with tempfile.TemporaryDirectory(prefix=".theme-styles-restore-", dir=self.current) as temporary:
                backup = Path(temporary)
                shutil.copytree(self.current / "theme", backup / "theme", symlinks=True)
                for name in ("theme.name", "background"):
                    path = self.current / name
                    if path.exists() or path.is_symlink():
                        shutil.copy2(path, backup / name, follow_symlinks=False)
                runtime = backup / "runtime"
                runtime.mkdir(mode=0o700)
                env = {**os.environ, "HOME": str(self.home), "OMARCHY_PATH": str(self.omarchy),
                       "OMARCHY_THEME_HEADLESS": "1", "OMARCHY_THEME_SKIP_BACKGROUND": "0",
                       "XDG_RUNTIME_DIR": str(runtime)}
                try:
                    self.run_process(["omarchy", "theme", "set", slug], backup, "restore.log", timeout=90, env=env)
                    restored = self.require_context(self._context(), slug)
                    wallpaper = Path(restored["wallpaper"])
                    if (restored["active"] or not wallpaper.is_file()
                            or wallpaper.is_relative_to(self.store.root(slug) / "variants")):
                        raise StylesError("The original theme has no usable wallpaper. Its saved styles have been kept.")
                except Exception as exc:
                    if (self.current / "theme").exists():
                        shutil.rmtree(self.current / "theme")
                    (backup / "theme").rename(self.current / "theme")
                    for name in ("theme.name", "background"):
                        if (backup / name).exists() or (backup / name).is_symlink():
                            os.replace(backup / name, self.current / name)
                        else:
                            (self.current / name).unlink(missing_ok=True)
                    detail = (backup / "restore.log").read_text(errors="replace")[-600:] if (backup / "restore.log").exists() else ""
                    raise StylesError("Omarchy could not restore the original theme. " + (detail.strip() or str(exc))) from exc
                finally:
                    if (self.current / "next-theme").exists():
                        shutil.rmtree(self.current / "next-theme")
                self.sync_shell(wallpaper)
        self.refresh_apps(slug)

    def restore(self, base, token=""):
        with lock(self.store.root(base) / "operation.lock"):
            self.check_context(base, token)
            self.activate(base, token)
        return {"ok": True, "message": "Original theme restored."}
