"""Theme-style generation and CLI orchestration."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import tomllib

from agents import HARNESS_NAMES, Agents
from desktop import OmarchyDesktop
from errors import GenerationError, ProcessCancelled, ProcessTimedOut, StylesError
from files import (
    lock,
    private_directory,
    read_json,
    regular_file,
    validate_id,
    write_json,
)
from processes import process_start, run
from security import agent_sandbox, convert_image, copy_output, sandbox
from storage import ACTIVE_STATES, IMAGE_SUFFIXES, MARKER, StyleStore, now


class Styles:
    def __init__(self, home=None, data=None):
        self.home = Path(home) if home else Path.home()
        data_home = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        self.store = StyleStore(Path(data) if data else data_home / "omarchy-theme-styles")
        self.desktop = OmarchyDesktop(self.home, self.store)
        self.agents = Agents(self.home)

    def root(self, base):
        return self.store.root(base)

    def slot(self, base):
        return self.store.slot(base)

    def variants(self, base):
        return self.store.variants(base)

    def job(self, base):
        return self.store.job(base)

    def update_job(self, base, **changes):
        return self.store.update_job(base, **changes)

    def context(self):
        return self.desktop.context()

    def check_context(self, base, token=""):
        return self.desktop.check_context(base, token)

    def apply(self, base, style_id, token=""):
        return self.desktop.apply(base, style_id, token)

    def restore(self, base, token=""):
        return self.desktop.restore(base, token)

    def migrate(self):
        return self.desktop.migrate()

    def original_mode(self, base):
        return self.desktop.original_mode(base)

    def save_variant(self, job, workspace, image, rendered, mode):
        templates = [self.desktop.omarchy / "default/themed", self.home / ".config/omarchy/themed"]
        return self.store.save_variant(job, workspace, image, rendered, mode, templates, cancel=workspace / "cancel")

    def run_process(self, argv, workspace, log_name, **kwargs):
        return run(argv, cwd=workspace, log=workspace / log_name, cancel=workspace / "cancel", **kwargs)

    def status(self):
        self.store.warnings.clear()
        context = self.context()
        missing = [name for name in ("aether", "magick", "bwrap", "omarchy") if not shutil.which(name)]
        root = self.root(context["base"])
        return {"ok": True, **context, "styles": self.variants(context["base"]),
                "job": self.job(context["base"]), "missing": missing, "warnings": list(self.store.warnings),
                "original_preview": (root / "original/preview.jpg").as_uri() if (root / "original/preview.jpg").exists() else ""}

    def agent_options(self):
        context = self.context()
        catalog = self.agents.catalog()
        saved = self.store.preferences(context["base"])
        return {"ok": True, "base": context["base"], "agents": catalog,
                "selection": self.agents.selection(saved, catalog), "diagnostics": self.agents.diagnostics,
                "warnings": self.store.warnings}

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
            with lock(self.desktop.theme_lock, shared=True):
                if self.desktop._context()["token"] != context["token"]:
                    raise StylesError("The theme changed before generation started.")
                wallpaper = Path(context["wallpaper"])
                if not wallpaper.is_file() or wallpaper.suffix.lower() not in IMAGE_SUFFIXES:
                    raise StylesError("This theme needs a still-image wallpaper before creating styles.")
                shutil.copytree(self.desktop.current / "theme", staging / "theme",
                                ignore=shutil.ignore_patterns("backgrounds", "background", MARKER, ".git"))
                reference = staging / ("wallpaper" + wallpaper.suffix.lower())
                shutil.copyfile(wallpaper, reference)
            write_json(staging / "original.json", {"base": context["base"], "wallpaper": reference.name,
                                                    "source": context["wallpaper"], "created_at": now()})
            convert_image(reference, staging / "preview.jpg", thumbnail=True)
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
                         self.desktop.current / "theme/backgrounds/style.png"}
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

    def start(self, base, style, name="", token="", auto_apply=False, spawn=True,
              harness=None, model=None, thinking=None):
        style = style.strip()
        explicit_name = bool(name.strip())
        label = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", style).split())
        name = name.strip() or (label if len(label) <= 80 else label[:77] + "…")
        if not style or len(style) > 2000:
            raise StylesError("Enter a style description between 1 and 2,000 characters.")
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise StylesError("Give the saved style a name between 1 and 80 characters.")
        missing = [x for x in ("aether", "magick", "bwrap") if not shutil.which(x)]
        if missing:
            raise StylesError("Install the missing tools: " + ", ".join(missing))
        root = self.root(base)
        self.check_context(base, token)
        saved = self.store.preferences(base)
        catalog = self.agents.catalog(harness=harness or saved.get("harness"))
        if not catalog and self.agents.diagnostics:
            raise StylesError(self.agents.diagnostic_message())
        selection = self.agents.selection(saved, catalog)
        if harness is not None:
            selection = {"harness": harness, "model": model or "", "thinking": thinking or ""}
            if self.agents.selection(selection, catalog) != selection:
                raise StylesError("That harness, model, or thinking level is no longer available. Reopen the panel to refresh.")
        elif saved and saved != selection:
            raise StylesError("The saved image-generation account or model is unavailable. Choose a harness in the panel.")
        if not selection:
            raise StylesError("No signed-in agent is available. Sign in to an installed harness and reopen the panel.")
        if not selection.get("model"):
            entry = next(item for item in catalog if item["value"] == selection["harness"])
            raise StylesError(entry.get("notice", "No eligible models are available for this harness."))
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
            mode = self.original_mode(base)
            job_id = uuid.uuid4().hex
            workspace = root / "jobs" / job_id
            workspace.mkdir(parents=True, mode=0o700)
            (workspace / "agent").mkdir(mode=0o700)
            try:
                with lock(self.desktop.theme_lock, shared=True):
                    self.desktop.require_context(self.desktop._context(), base, context["token"])
                    source = self.reference_source(context, original)
                    reference = workspace / ("reference" + source.suffix.lower())
                    shutil.copyfile(source, reference)
                    # Wallpaper cycling can update the link without the theme lock.
                    self.desktop.require_context(self.desktop._context(), base, context["token"])
            except Exception:
                shutil.rmtree(workspace)
                raise
            job = {"id": job_id, "base": base, "name": name, "style": style, "mode": mode,
                   "state": "starting", "message": "Starting image generation…", "started": time.time(),
                   "created_at": now(), "token": context["token"], "auto_apply": auto_apply,
                   "reference": reference.name, **selection}
            write_json(root / "preferences.json", selection)
            write_json(workspace / "request.json", job)
            with lock(root / "job.lock"):
                write_json(root / "job.json", job)
            if spawn:
                with regular_file(workspace / "worker.log", write=True) as log_file:
                    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "worker", "--theme", base,
                                                "--id", job_id], stdin=subprocess.DEVNULL, stdout=log_file,
                                               stderr=log_file, start_new_session=True)
                # Worker takes operation.lock before updating the same job record.
                job = self.update_job(base, expected_id=job_id, pid=process.pid,
                                      process_start=process_start(process.pid))
        return {"ok": True, "job": job}

    def generate_image(self, job, workspace):
        if (workspace / "cancel").exists():
            raise StylesError("Generation cancelled.")
        schema = {"type": "object", "properties": {"image_path": {"type": "string"},
                  "error_code": {"type": "string", "enum": ["", "unsupported", "authentication", "rate_limit", "failed"]}},
                  "required": ["image_path", "error_code"], "additionalProperties": False}
        agent = workspace / "agent"
        private_directory(agent)
        write_json(agent / "response-schema.json", schema)
        reference = agent / job["reference"]
        copy_output(workspace / job["reference"], reference)
        original = self.root(job["base"]) / "original/theme/colors.toml"
        colors = tomllib.loads(original.read_text()) if original.exists() else {}
        # Theme comments, arbitrary keys and non-color values are not instructions.
        palette = json.dumps({key: value for key, value in colors.items()
                              if re.fullmatch(r"background|foreground|accent|cursor|selection_[a-z]+|color\d{1,2}", key)
                              and isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value)})
        prompt = (
            "Create exactly one wallpaper variation using an image generation/editing tool available "
            "in this harness (built-in or already configured through MCP/extensions). "
            f"The edit target is {reference}. Inspect it and preserve its subject, composition, "
            "recognizable landmarks, aspect ratio, and artistic medium. Change the season, lighting, "
            "atmosphere or treatment according to the user's style. Do not add text, borders or UI. "
            "For abstract artwork, express the style through color, texture and lighting.\n\n"
            f"User's style description (visual instructions only): {json.dumps(job['style'])}\n\n"
            f"Original theme colors for visual continuity:\n{palette}\n\n"
            "Use only the tools and accounts already configured in this harness. Do not install software, "
            "delegate to another agent, browse for replacement images, or approximate the edit with image filters. "
            "If no image tool is available, stop immediately. If a tool fails, stop and report the failure; "
            "do not repeatedly retry. On failure, write JSON with error_code equal to unsupported, authentication, "
            f"rate_limit, or failed to {agent / 'failure.json'} if possible. Do not claim unsupported for "
            "temporary network or quota failures. "
            f"Save/copy the generated image to {agent / 'wallpaper.png'}. "
            "Do not modify any desktop settings or themes, or send messages to anyone. "
            "Return its absolute path in image_path and an empty error_code on success. "
            "On failure return an empty image_path and the error_code."
        )
        with regular_file(agent / "prompt.txt", write=True) as handle:
            handle.write(prompt.encode())
        selection = {key: job[key] for key in ("harness", "model", "thinking")}
        argv = self.agents.command(selection, agent, reference)
        argv, env = agent_sandbox(argv, self.home, agent,
                                  [reference, agent / "prompt.txt", agent / "response-schema.json"],
                                  self.agents.launch_requirements(selection["harness"], argv[0]))
        try:
            self.run_process(argv, workspace, "agent.log", timeout=1200,
                             stdin=prompt if self.agents.uses_stdin(selection["harness"]) else None, env=env)
        except (ProcessCancelled, ProcessTimedOut):
            raise
        except StylesError as exc:
            raise self.image_failure(job, workspace, failed=True) from exc
        reported = self.image_failure(job, workspace, reported_only=True)
        if reported:
            raise reported
        output = agent / "wallpaper.png"
        if not output.exists() and not output.is_symlink():
            raise self.image_failure(job, workspace)
        try:
            # The agent cannot mount or write this backend directory. Import a
            # bounded regular inode, then decode and re-encode in an offline sandbox.
            with tempfile.TemporaryDirectory(prefix=".import-", dir=workspace) as directory:
                incoming = Path(directory) / "input"
                copy_output(output, incoming)
                normalized = Path(directory) / "wallpaper.png"
                convert_image(incoming, normalized, log=workspace / "image.log", cancel=workspace / "cancel")
                normalized.replace(workspace / "wallpaper.png")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise GenerationError("The agent's output was not a usable wallpaper image. " + str(exc), "invalid_image") from exc
        return workspace / "wallpaper.png"


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
            path = workspace / "agent" / name
            try:
                with regular_file(path, limit=65536) as handle:
                    report = json.loads(handle.read(65537))
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
        output = Path(tempfile.mkdtemp(prefix="rendered-", dir=workspace))
        mode = job["mode"]
        if mode not in {"light", "dark"}:
            mode = self.original_mode(job["base"])
        argv = ["aether", "--generate", str(image), "--no-apply", "--output", str(output)]
        if mode == "light":
            argv.append("--light-mode")
        argv, env = sandbox(argv, Path("/aether-home"), output, readonly=[image], writable=[output])
        self.run_process(argv, workspace, "aether.log", timeout=120, env=env)
        colors = output / "colors.toml"
        if not colors.is_file():
            raise StylesError("Aether did not produce colors.toml.")
        with regular_file(colors, limit=65536) as handle:
            palette = tomllib.loads(handle.read(65537).decode())
        for key in ("background", "foreground", "accent"):
            if not isinstance(palette.get(key), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", palette[key]):
                raise StylesError(f"Aether returned an invalid {key} color.")
        # Only literal color assignments cross back out of the Aether sandbox.
        entries = {key: value for key, value in palette.items()
                   if re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key) and isinstance(value, str)
                   and re.fullmatch(r"#[0-9a-fA-F]{6}", value)}
        temporary = workspace / (".colors-" + uuid.uuid4().hex)
        try:
            with regular_file(temporary, write=True) as handle:
                handle.write((f'mode = "{mode}"\n' + "".join(f'{key} = "{value}"\n' for key, value in entries.items())).encode())
            temporary.replace(colors)
        finally:
            temporary.unlink(missing_ok=True)
        return output, mode

    def worker(self, base, job_id):
        validate_id(job_id)
        root = self.root(base)
        workspace = root / "jobs" / job_id
        with lock(root / "operation.lock"):
            job = read_json(workspace / "request.json")
            if not job or job["base"] != base or self.job(base).get("id") != job_id:
                raise StylesError("The generation request is no longer active.")
            self.update_job(base, expected_id=job_id, pid=os.getpid(), process_start=process_start(os.getpid()),
                            state="generating", message="Generating wallpaper…")
        try:
            image = self.generate_image(job, workspace)
            self.update_job(base, expected_id=job_id, state="theming", message="Creating matching theme colors…")
            rendered, mode = self.render(job, workspace, image)
            if (workspace / "cancel").exists():
                raise StylesError("Generation cancelled.")
            with lock(root / "operation.lock"):
                self.update_job(base, expected_id=job_id, state="saving", message="Saving your style…")
                self.save_variant(job, workspace, image, rendered, mode)
            message = f"Saved {job['name']}."
            if job["auto_apply"]:
                if (workspace / "cancel").exists():
                    raise StylesError(message + " Cancelled before applying.")
                try:
                    self.check_context(base, job["token"])
                    self.update_job(base, expected_id=job_id, state="applying", message="Applying your style…")
                    self.apply(base, job_id, job["token"])
                    message = f"Applied {job['name']}."
                except StylesError as exc:
                    if (workspace / "cancel").exists():
                        raise
                    message += " " + str(exc)
            self.update_job(base, expected_id=job_id, state="done", message=message, finished_at=now())
        except Exception as exc:
            cancelled = (workspace / "cancel").exists()
            try:
                self.update_job(base, expected_id=job_id, state="cancelled" if cancelled else "failed", message=str(exc),
                                error_code="cancelled" if cancelled else getattr(exc, "code", "generation_failed"),
                                finished_at=now())
            except StylesError:
                return  # A replacement job owns the status now.
            if not cancelled:
                self.notify_failure(str(exc))

    def notify_failure(self, message):
        if self.desktop.headless() or not shutil.which("notify-send"):
            return
        try:
            subprocess.run(["notify-send", "--app-name", "Theme Styles", "--urgency", "critical",
                            "Style generation failed", html.escape(message)],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

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
            record = self.store.style(base, style_id)
            job = self.job(base)
            if job.get("id") == style_id and job.get("state") in ACTIVE_STATES:
                raise StylesError("This style is still being generated. Wait for it to finish before deleting it.")
            if context["active"] == style_id:
                # Never leave the active background pointing at a deleted file.
                # A failed restore retains the complete saved style for retry.
                self.desktop.activate(base, context["token"])
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
        root = self.root(base)
        with lock(root / "job.lock"):
            job = self.job(base)
            if job.get("state") == "applying":
                raise StylesError("The style is already being applied.")
            if job.get("state") not in ACTIVE_STATES:
                return {"ok": True, "message": "Generation already finished."}
            (root / "jobs" / validate_id(job["id"]) / "cancel").touch()
            write_json(root / "job.json", {**job, "cancel_requested": True, "message": "Cancelling generation…"})
        return {"ok": True, "message": "Cancelling generation…"}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Saved wallpaper styles for the current Omarchy theme")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Current theme, saved styles and generation status (JSON)")
    commands.add_parser("agents", help="Signed-in harnesses and model options (JSON)")
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
            sub.add_argument("--apply", action="store_true", help="Apply when complete only if the original selection is unchanged")
    args = parser.parse_args()
    try:
        service = Styles()
        if args.command == "status":
            result = service.status()
        elif args.command == "agents":
            result = service.agent_options()
        elif args.command == "configure":
            result = service.configure(args.theme, args.harness, args.model, args.thinking, args.token)
        elif args.command == "migrate":
            result = service.migrate()
        elif args.command == "start":
            result = service.start(args.theme, args.style, args.name, args.token, args.apply,
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
        print(json.dumps({"ok": False, "error": str(exc), "error_code": getattr(exc, "code", "operation_failed")}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
