"""Theme-scoped wallpaper generation and native Omarchy theme activation.

Only the worker invokes an agent. Listing and switching saved styles are local.
Agent output is an image; Aether and Omarchy own all theme configuration.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from agents import Agents, HARNESS_NAMES

PLUGIN_ID = "io.weirdware.themestyles"
# Recognize retained styles created before the plugin received its final ID.
STYLE_OWNERS = {PLUGIN_ID, "io.github.erikwb.theme-styles"}
MARKER = "theme-styles.json"
ACTIVE_STATES = {"starting", "generating", "theming", "saving", "applying"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# The same app refresh helpers used by Omarchy's theme setter. Missing helpers
# on older releases are skipped; theme source directories are never changed.
REFRESH_COMMANDS = (
    "omarchy-restart-terminal", "omarchy-restart-hyprctl", "omarchy-restart-btop",
    "omarchy-restart-opencode", "omarchy-restart-helix",
    *(f"omarchy-theme-set-{app}" for app in (
        "foot", "tmux", "gnome", "pi", "claude", "hermes", "t3code", "browser",
        "vscode", "obsidian", "keyboard")),
)


class StylesError(Exception):
    pass


class GenerationError(StylesError):
    def __init__(self, message, code="generation_failed"):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_slug(value):
    if not value or value.startswith(".") or "/" in value or "\\" in value or any(ord(c) < 32 for c in value):
        raise StylesError("Invalid theme identifier.")
    return value


def validate_id(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise StylesError("Invalid saved style identifier.")
    return value


@contextmanager
def lock(path, *, shared=False, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(handle, flags | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise StylesError("A style operation is already running. Try again shortly.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def process_start(pid):
    try:
        # Everything after the final ')' starts at proc stat field 3.
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return ""


class Styles:
    def __init__(self, home=None, data=None):
        self.home = Path(home) if home else Path.home()
        self.current = self.home / ".local/state/omarchy/current"
        self.themes = self.home / ".config/omarchy/themes"
        self.omarchy = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"))
        data_home = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        self.data = Path(data) if data else data_home / "omarchy-theme-styles"
        self.theme_lock = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-theme-set.lock"
        self.agents = Agents(self.home)

    def root(self, base):
        validate_slug(base)
        key = hashlib.sha256(base.encode()).hexdigest()[:24]
        return self.data / "themes" / key

    def slot(self, base):
        prefix = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:48] or "theme"
        return f"{prefix}-styles-{hashlib.sha256(base.encode()).hexdigest()[:8]}"

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
            if name in {candidate, self.slot(candidate)}:
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

    def variants(self, base):
        result = []
        for path in (self.root(base) / "variants").glob("*/style.json"):
            record = read_json(path)
            if record and record.get("base") == base:
                record["preview"] = (path.parent / "preview.jpg").as_uri()
                result.append(record)
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def job(self, base):
        job = read_json(self.root(base) / "job.json", {})
        if job.get("state") in ACTIVE_STATES:
            alive = job.get("pid") and process_start(job["pid"]) == job.get("process_start")
            if not alive and time.time() - job.get("started", 0) > 10:
                job = {**job, "state": "failed", "message": "Generation was interrupted. You can generate again."}
        job_id = job.get("id", "")
        if isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{32}", job_id):
            workspace = self.root(base) / "jobs" / job_id
            logs = [workspace / name for name in ("agent.log", "image.log", "aether.log")]
            try:
                logs = [path for path in logs if path.is_file() and not path.is_symlink()]
                job["log_path"] = str(max(logs, key=lambda path: path.stat().st_mtime_ns)) if logs else ""
            except OSError:
                job["log_path"] = ""
        return job

    def status(self):
        context = self.context()
        missing = [name for name in ("aether", "magick", "omarchy") if not shutil.which(name)]
        root = self.root(context["base"])
        return {"ok": True, **context, "styles": self.variants(context["base"]),
                "job": self.job(context["base"]), "missing": missing,
                "original_preview": (root / "original/preview.jpg").as_uri() if (root / "original/preview.jpg").exists() else ""}

    def agent_options(self):
        context = self.context()
        catalog = self.agents.catalog()
        saved = read_json(self.root(context["base"]) / "preferences.json", {})
        return {"ok": True, "base": context["base"], "agents": catalog,
                "selection": self.agents.selection(saved, catalog)}

    def configure(self, base, harness, model, thinking, token=""):
        self.check_context(base, token)
        selection = self.agents.validate({"harness": harness, "model": model, "thinking": thinking})
        with lock(self.root(base) / "operation.lock"):
            self.check_context(base, token)
            write_json(self.root(base) / "preferences.json", selection)
        return {"ok": True}

    def snapshot(self, context):
        root = self.root(context["base"])
        original = root / "original"
        if original.is_dir():
            return original
        if context["active"]:
            raise StylesError("The original theme snapshot is missing. Select the original theme first.")
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".original-", dir=root))
        try:
            with lock(self.theme_lock, shared=True):
                if self._context()["token"] != context["token"]:
                    raise StylesError("The theme changed before generation started.")
                wallpaper = Path(context["wallpaper"])
                if not wallpaper.is_file() or wallpaper.suffix.lower() not in IMAGE_SUFFIXES:
                    raise StylesError("This theme needs a still-image wallpaper before creating styles.")
                shutil.copytree(self.current / "theme", staging / "theme",
                                ignore=shutil.ignore_patterns("backgrounds", "background", MARKER, ".git"))
                reference = staging / ("wallpaper" + wallpaper.suffix.lower())
                shutil.copyfile(wallpaper, reference)
            write_json(staging / "original.json", {"base": context["base"], "wallpaper": reference.name,
                                                    "source": context["wallpaper"], "created_at": now()})
            subprocess.run(["magick", str(reference), "-thumbnail", "640x360>", str(staging / "preview.jpg")],
                           check=True, capture_output=True, timeout=30)
            staging.rename(original)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return original

    def reference_source(self, context, original):
        """Use the selected wallpaper, or the original behind an applied style."""
        wallpaper = Path(context["wallpaper"])
        if context["active"]:
            style_id = validate_id(context["active"])
            root = self.root(context["base"])
            variant = root / "variants" / style_id
            generated = {variant / "theme/backgrounds/style.png",
                         self.current / "theme/backgrounds/style.png"}
            if wallpaper in {path.resolve() for path in generated}:
                record = read_json(variant / "style.json", {})
                if record.get("reference"):
                    name = record["reference"]
                    if name not in {"reference" + suffix for suffix in IMAGE_SUFFIXES}:
                        raise StylesError("This saved style has an invalid source wallpaper.")
                    wallpaper = variant / name
                else:
                    # Earlier versions retained the reference only in the job.
                    workspace = root / "jobs" / style_id
                    request = read_json(workspace / "request.json", {})
                    name = request.get("reference", "")
                    if name in {"reference" + suffix for suffix in IMAGE_SUFFIXES} and (workspace / name).is_file():
                        wallpaper = workspace / name
                    else:
                        source = read_json(original / "original.json", {})
                        name = source.get("wallpaper", "")
                        if name not in {"wallpaper" + suffix for suffix in IMAGE_SUFFIXES}:
                            raise StylesError("The original wallpaper for this saved style is missing.")
                        wallpaper = original / name
        if not wallpaper.is_file() or wallpaper.suffix.lower() not in IMAGE_SUFFIXES:
            raise StylesError("The source wallpaper is unavailable. Select a still-image wallpaper and try again.")
        return wallpaper

    def start(self, base, style, name="", mode="auto", token="", auto_apply=False, spawn=True,
              harness=None, model=None, thinking=None):
        style = style.strip()
        explicit_name = bool(name.strip())
        label = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", style).split())
        name = name.strip() or (label if len(label) <= 80 else label[:77] + "…")
        if not style or len(style) > 2000:
            raise StylesError("Enter a style description between 1 and 2,000 characters.")
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise StylesError("Give the saved style a name between 1 and 80 characters.")
        if mode not in {"auto", "light", "dark"}:
            raise StylesError("Choose Original, Light, or Dark application colors.")
        missing = [x for x in ("aether", "magick") if not shutil.which(x)]
        if missing:
            raise StylesError("Install the missing tools: " + ", ".join(missing))
        root = self.root(base)
        self.check_context(base, token)
        catalog = self.agents.catalog()
        saved = read_json(root / "preferences.json", {})
        selection = self.agents.selection(saved, catalog)
        if harness is not None:
            selection = {"harness": harness, "model": model or "", "thinking": thinking or ""}
            if self.agents.selection(selection, catalog) != selection:
                raise StylesError("That harness, model, or thinking level is no longer available. Reopen the panel to refresh.")
        elif saved and saved != selection:
            raise StylesError("The saved image-generation account or model is unavailable. Choose a harness in the panel.")
        if not selection:
            raise StylesError("No signed-in agent is available. Sign in to an installed harness and reopen the panel.")
        with lock(root / "operation.lock", blocking=False):
            context = self.check_context(base, token)
            if self.job(base).get("state") in ACTIVE_STATES:
                raise StylesError("A style is already being generated for this theme.")
            used_names = {v["name"].casefold() for v in self.variants(base)}
            if explicit_name and name.casefold() in used_names:
                raise StylesError("That name is already saved. Choose a new name to keep both versions.")
            stem, number = name, 2
            while name.casefold() in used_names:
                suffix = f" {number}"
                name = stem[:80 - len(suffix)].rstrip() + suffix
                number += 1
            original = self.snapshot(context)
            job_id = uuid.uuid4().hex
            workspace = root / "jobs" / job_id
            workspace.mkdir(parents=True)
            try:
                with lock(self.theme_lock, shared=True):
                    self.require_context(self._context(), base, context["token"])
                    source = self.reference_source(context, original)
                    reference = workspace / ("reference" + source.suffix.lower())
                    shutil.copyfile(source, reference)
                    # Wallpaper cycling can update the link without the theme lock.
                    self.require_context(self._context(), base, context["token"])
            except Exception:
                shutil.rmtree(workspace)
                raise
            job = {"id": job_id, "base": base, "name": name, "style": style, "mode": mode,
                   "state": "starting", "message": "Starting image generation…", "started": time.time(),
                   "created_at": now(), "token": context["token"], "auto_apply": auto_apply,
                   "reference": reference.name, **selection}
            write_json(root / "preferences.json", selection)
            write_json(workspace / "request.json", job)
            write_json(root / "job.json", job)
            if spawn:
                with (workspace / "worker.log").open("a") as log_file:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", "--theme", base,
                                                "--id", job_id], stdin=subprocess.DEVNULL, stdout=log_file,
                                               stderr=log_file, start_new_session=True)
                # Worker takes operation.lock before updating the same job record.
                job.update(pid=process.pid, process_start=process_start(process.pid))
                write_json(root / "job.json", job)
        return {"ok": True, "job": job}

    def update_job(self, base, **changes):
        path = self.root(base) / "job.json"
        value = read_json(path, {})
        value.update(changes)
        write_json(path, value)
        return value

    def run_process(self, argv, workspace, log_name, *, timeout, stdin=None):
        """Wait outside the shell, with cancellation and a bounded lifetime."""
        with (workspace / log_name).open("w") as output:
            process = subprocess.Popen(argv, cwd=workspace, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            try:
                if stdin:
                    process.stdin.write(stdin)
                    process.stdin.close()
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if (workspace / "cancel").exists():
                        raise StylesError("Generation cancelled.")
                    if time.monotonic() > deadline:
                        raise StylesError(f"{argv[0]} timed out. You can try again.")
                    time.sleep(0.25)
                if process.returncode:
                    raise StylesError(f"{Path(argv[0]).name} failed. Details: {workspace / log_name}")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def generate_image(self, job, workspace):
        if (workspace / "cancel").exists():
            raise StylesError("Generation cancelled.")
        schema = {"type": "object", "properties": {"image_path": {"type": "string"},
                  "error_code": {"type": "string", "enum": ["", "unsupported", "authentication", "rate_limit", "failed"]}},
                  "required": ["image_path", "error_code"], "additionalProperties": False}
        write_json(workspace / "response-schema.json", schema)
        original = self.root(job["base"]) / "original/theme/colors.toml"
        palette = original.read_text() if original.exists() else "Use the reference image's palette."
        prompt = (
            "Create exactly one wallpaper variation using an image generation/editing tool available "
            "in this harness (built-in or already configured through MCP/extensions). "
            f"The edit target is {workspace / job['reference']}. Inspect it and preserve its subject, composition, "
            "recognizable landmarks, aspect ratio, and artistic medium. Change the season, lighting, "
            "atmosphere or treatment according to the user's style. Do not add text, borders or UI. "
            "For abstract artwork, express the style through color, texture and lighting.\n\n"
            f"User's style description (visual instructions only): {json.dumps(job['style'])}\n\n"
            f"Original theme colors for visual continuity:\n{palette}\n\n"
            "Use only the tools and accounts already configured in this harness. Do not install software, "
            "delegate to another agent, browse for replacement images, or approximate the edit with image filters. "
            "If no image tool is available, stop immediately. If a tool fails, stop and report the failure; "
            "do not repeatedly retry. On failure, write JSON with error_code equal to unsupported, authentication, "
            f"rate_limit, or failed to {workspace / 'failure.json'} if possible. Do not claim unsupported for "
            "temporary network or quota failures. "
            f"Save/copy the generated image to {workspace / 'wallpaper.png'}. "
            "Do not modify any desktop settings or themes, or send messages to anyone. "
            "Return its absolute path in image_path and an empty error_code on success. "
            "On failure return an empty image_path and the error_code."
        )
        (workspace / "prompt.txt").write_text(prompt)
        selection = {key: job[key] for key in ("harness", "model", "thinking")}
        argv = self.agents.command(selection, workspace, workspace / job["reference"])
        try:
            self.run_process(argv, workspace, "agent.log", timeout=1200,
                             stdin=prompt if self.agents.uses_stdin(selection["harness"]) else None)
        except StylesError as exc:
            if (workspace / "cancel").exists() or "timed out" in str(exc):
                raise
            raise self.image_failure(job, workspace, failed=True) from exc
        reported = self.image_failure(job, workspace, reported_only=True)
        if reported:
            raise reported
        output = workspace / "wallpaper.png"
        if not output.is_file() or output.is_symlink():
            raise self.image_failure(job, workspace)
        result = subprocess.run(["magick", "identify", "-format", "%m %w %h", str(output)],
                                capture_output=True, text=True, timeout=30)
        parts = result.stdout.split()
        if (result.returncode or len(parts) != 3 or parts[0] not in {"PNG", "JPEG", "WEBP", "BMP"}
                or min(int(parts[1]), int(parts[2])) < 256):
            raise GenerationError("The agent's output was not a usable wallpaper image.", "invalid_image")
        if parts[0] != "PNG":
            # Tools sometimes copy a JPEG/WebP to the requested .png filename.
            # Validate the actual image and normalize it before Aether sees it.
            normalized = workspace / "normalized.png"
            self.run_process(["magick", str(output), str(normalized)], workspace, "image.log", timeout=30)
            normalized.replace(output)
        return output

    @staticmethod
    def image_failure(job, workspace, failed=False, reported_only=False):
        harness = HARNESS_NAMES.get(job["harness"], "The selected agent")
        reasons = {
            "unsupported": f"{harness} reported that image generation is unavailable with this setup. Try another harness or model.",
            "authentication": f"{harness} reported an authentication error. Check its login and image-tool connection, then try again.",
            "rate_limit": f"{harness} reported a quota or rate limit. Try again later or choose another harness.",
            "failed": f"{harness} could not generate the wallpaper. Check its image tool or try another harness.",
        }
        for name in ("failure.json", "response.json"):
            path = workspace / name
            try:
                if path.is_symlink() or path.stat().st_size > 65536:
                    continue
                report = read_json(path)
                code = report.get("error_code") if isinstance(report, dict) else None
                if code in reasons:
                    return GenerationError(reasons[code] + f" Details: {workspace / 'agent.log'}", code)
            except (OSError, ValueError):
                pass
        if reported_only:
            return None
        message = (f"{harness} failed to generate the wallpaper. Check its login, model, and image tools."
                   if failed else f"{harness} did not save a wallpaper. Image generation may be unavailable "
                   "with this setup. Try another harness or model.")
        return GenerationError(message + f" Details: {workspace / 'agent.log'}")

    def render(self, job, workspace, image):
        output = workspace / "rendered"
        mode = job["mode"]
        if mode == "auto":
            source = self.root(job["base"]) / "original/theme/colors.toml"
            colors = tomllib.loads(source.read_text()) if source.exists() else {}
            mode = colors.get("mode", colors.get("theme_type", "dark"))
            if mode not in {"light", "dark"}:
                mode = "dark"
        argv = ["aether", "--generate", str(image), "--no-apply", "--output", str(output)]
        if mode == "light":
            argv.append("--light-mode")
        self.run_process(argv, workspace, "aether.log", timeout=120)
        colors = output / "colors.toml"
        if not colors.is_file():
            raise StylesError("Aether did not produce colors.toml.")
        palette = tomllib.loads(colors.read_text())
        for key in ("background", "foreground", "accent"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", palette.get(key, "")):
                raise StylesError(f"Aether returned an invalid {key} color.")
        return output, mode

    def save_variant(self, job, workspace, image, rendered, mode):
        root = self.root(job["base"])
        variants = root / "variants"
        variants.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".saving-", dir=variants))
        try:
            theme = staging / "theme"
            # Inherit assets and non-palette extras; Omarchy regenerates supported
            # app configurations from the new palette instead of keeping old colors.
            shutil.copytree(root / "original/theme", theme)
            template_roots = [self.omarchy / "default/themed", self.home / ".config/omarchy/themed"]
            for template_root in template_roots:
                for template in template_root.glob("*.tpl"):
                    (theme / template.name.removesuffix(".tpl")).unlink(missing_ok=True)
            shutil.copyfile(rendered / "colors.toml", theme / "colors.toml")
            # Markers controlling dark/light mode may come from older themes.
            (theme / "light.mode").unlink(missing_ok=True)
            if mode == "light":
                (theme / "light.mode").touch()
            backgrounds = theme / "backgrounds"
            backgrounds.mkdir(exist_ok=True)
            shutil.copyfile(image, backgrounds / "style.png")
            subprocess.run(["magick", str(image), "-thumbnail", "640x360>", str(staging / "preview.jpg")],
                           capture_output=True, timeout=30, check=True)
            record = {"id": job["id"], "base": job["base"], "name": job["name"], "style": job["style"],
                      "mode": mode, "created_at": now(), "provider": job.get("harness", "codex"),
                      "harness": job.get("harness", "codex"), "model": job.get("model", ""),
                      "thinking": job.get("thinking", ""), "reference": job["reference"]}
            # Keep the source with the style so it survives cleanup of job logs.
            shutil.copyfile(workspace / job["reference"], staging / job["reference"])
            write_json(staging / "style.json", record)
            write_json(theme / MARKER, {"plugin": PLUGIN_ID, "base": job["base"], "style_id": job["id"]})
            staging.rename(variants / job["id"])
            return record
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def worker(self, base, job_id):
        validate_id(job_id)
        root = self.root(base)
        workspace = root / "jobs" / job_id
        with lock(root / "operation.lock"):
            job = read_json(workspace / "request.json")
            if not job or job["base"] != base or self.job(base).get("id") != job_id:
                raise StylesError("The generation request is no longer active.")
            self.update_job(base, pid=os.getpid(), process_start=process_start(os.getpid()),
                            state="generating", message="Generating wallpaper…")
        try:
            image = self.generate_image(job, workspace)
            self.update_job(base, state="theming", message="Creating matching theme colors…")
            rendered, mode = self.render(job, workspace, image)
            if (workspace / "cancel").exists():
                raise StylesError("Generation cancelled.")
            with lock(root / "operation.lock"):
                self.update_job(base, state="saving", message="Saving your style…")
                self.save_variant(job, workspace, image, rendered, mode)
            message = f"Saved {job['name']}."
            if job["auto_apply"]:
                if (workspace / "cancel").exists():
                    raise StylesError(message + " Cancelled before applying.")
                try:
                    self.check_context(base, job["token"])
                    self.update_job(base, state="applying", message="Applying your style…")
                    self.apply(base, job_id, job["token"])
                    message = f"Applied {job['name']}."
                except StylesError as exc:
                    message += " " + str(exc)
            self.update_job(base, state="done", message=message, finished_at=now())
        except Exception as exc:
            cancelled = (workspace / "cancel").exists()
            self.update_job(base, state="cancelled" if cancelled else "failed", message=str(exc),
                            error_code="cancelled" if cancelled else getattr(exc, "code", "generation_failed"),
                            finished_at=now())
            if not cancelled:
                self.notify_failure(str(exc))

    def notify_failure(self, message):
        if self.headless() or not shutil.which("notify-send"):
            return
        try:
            subprocess.run(["notify-send", "--app-name", "Theme Styles", "--urgency", "critical",
                            "Style generation failed", html.escape(message)],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def apply(self, base, style_id, token=""):
        validate_id(style_id)
        root = self.root(base)
        with lock(root / "operation.lock"):
            self.check_context(base, token)
            variant = root / "variants" / style_id
            record = read_json(variant / "style.json", {})
            if record.get("base") != base or record.get("id") != style_id:
                raise StylesError("This style does not belong to the current theme.")
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
        archived = []
        for destination in self.themes.glob("*-styles-*"):
            if destination.is_symlink() or not destination.is_dir():
                continue
            marker = read_json(destination / MARKER, {})
            if marker.get("plugin") not in STYLE_OWNERS:
                continue
            base = validate_slug(marker.get("base", ""))
            if destination.name != self.slot(base):
                continue
            root = self.root(base)
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

    def activate(self, slug):
        result = subprocess.run(["omarchy", "theme", "set", slug], capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise StylesError("Omarchy could not apply this style: " + result.stderr.strip()[-600:])

    def restore(self, base, token=""):
        with lock(self.root(base) / "operation.lock"):
            self.check_context(base, token)
            self.activate(base)
        return {"ok": True, "message": "Original theme restored."}

    def delete(self, base, style_id, token="", confirmed=False):
        if not confirmed:
            raise StylesError("Confirm deletion of this saved style first.")
        validate_id(style_id)
        root = self.root(base)
        with lock(root / "operation.lock"):
            context = self.check_context(base, token)
            variant = root / "variants" / style_id
            workspace = root / "jobs" / style_id
            if variant.is_symlink() or workspace.is_symlink():
                raise StylesError("Refusing to delete a saved style through a symbolic link.")
            record = read_json(variant / "style.json", {})
            if record.get("base") != base or record.get("id") != style_id:
                raise StylesError("This style does not belong to the current theme.")
            job = self.job(base)
            if job.get("id") == style_id and job.get("state") in ACTIVE_STATES:
                raise StylesError("This style is still being generated. Wait for it to finish before deleting it.")
            if context["active"] == style_id:
                # Never leave the active background pointing at a deleted file.
                # A failed restore retains the complete saved style for retry.
                self.activate(base)
                restored = self.check_context(base)
                if restored["active"]:
                    raise StylesError("Restore the original appearance before deleting the active style.")
            shutil.rmtree(variant)
            if workspace.exists():
                shutil.rmtree(workspace)
            if job.get("id") == style_id:
                (root / "job.json").unlink(missing_ok=True)
        return {"ok": True, "message": f"Deleted {record['name']}."}

    def cancel(self, base):
        self.check_context(base)
        job = self.job(base)
        if job.get("state") == "applying":
            raise StylesError("The style is already being applied.")
        if job.get("state") in ACTIVE_STATES:
            (self.root(base) / "jobs" / validate_id(job["id"]) / "cancel").touch()
            self.update_job(base, cancel_requested=True, message="Cancelling generation…")
        return {"ok": True, "message": "Cancelling generation…"}


def main():
    parser = argparse.ArgumentParser(description="Saved wallpaper styles for the current Omarchy theme")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Current theme, saved styles and generation status (JSON)")
    commands.add_parser("agents", help="Signed-in image-capable harnesses and model options (JSON)")
    commands.add_parser("migrate", help="Archive old companion themes without removing saved styles")
    for action in ("start", "configure", "apply", "restore", "delete", "cancel", "worker"):
        sub = commands.add_parser(action)
        sub.add_argument("--theme", required=True, help="Expected source theme; operations refuse a different current theme")
        if action in {"start", "configure", "apply", "restore", "delete"}:
            sub.add_argument("--token", default="", help="Expected theme selection token from status")
        if action in {"apply", "delete", "worker"}:
            sub.add_argument("--id", required=True)
        if action == "delete":
            sub.add_argument("--yes", action="store_true", help="Confirm permanent deletion of this saved style")
        if action in {"start", "configure"}:
            sub.add_argument("--harness", required=action == "configure")
            sub.add_argument("--model", required=action == "configure")
            sub.add_argument("--thinking", default="")
        if action == "start":
            sub.add_argument("--style", required=True)
            sub.add_argument("--name", default="")
            sub.add_argument("--mode", choices=["auto", "dark", "light"], default="auto")
            sub.add_argument("--apply", action="store_true", help="Apply when complete only if the original selection is unchanged")
    args = parser.parse_args()
    service = Styles()
    try:
        if args.command == "status":
            result = service.status()
        elif args.command == "agents":
            result = service.agent_options()
        elif args.command == "configure":
            result = service.configure(args.theme, args.harness, args.model, args.thinking, args.token)
        elif args.command == "migrate":
            result = service.migrate()
        elif args.command == "start":
            result = service.start(args.theme, args.style, args.name, args.mode, args.token, args.apply,
                                   harness=args.harness, model=args.model, thinking=args.thinking)
        elif args.command == "worker":
            service.worker(args.theme, args.id)
            return
        elif args.command == "apply":
            result = service.apply(args.theme, args.id, args.token)
        elif args.command == "restore":
            result = service.restore(args.theme, args.token)
        elif args.command == "delete":
            result = service.delete(args.theme, args.id, args.token, args.yes)
        else:
            result = service.cancel(args.theme)
        print(json.dumps(result, ensure_ascii=False))
    except (StylesError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
