"""Theme-scoped saved styles and job records; no desktop mutation."""
import hashlib
import math
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from errors import ProcessCancelled, RecordError, StylesError
from files import (
    lock,
    private_directory,
    read_json,
    validate_id,
    validate_slug,
    write_json,
)
from processes import process_start
from security import convert_image

PLUGIN_ID = "io.weirdware.themestyles"
# Recognize retained styles created before the plugin received its final ID.
STYLE_OWNERS = {PLUGIN_ID, "io.github.erikwb.theme-styles"}
MARKER = "theme-styles.json"
ACTIVE_STATES = {"starting", "generating", "theming", "saving", "applying"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StyleStore:
    def __init__(self, data):
        self.data = Path(data)
        private_directory(self.data)
        self.warnings = []

    def root(self, base):
        validate_slug(base)
        key = hashlib.sha256(base.encode()).hexdigest()[:24]
        return self.data / "themes" / key

    def slot(self, base):
        prefix = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:48] or "theme"
        return f"{prefix}-styles-{hashlib.sha256(base.encode()).hexdigest()[:8]}"

    def variants(self, base):
        result = []
        for path in (self.root(base) / "variants").glob("*/style.json"):
            try:
                record = self.style(base, path.parent.name)
                record["preview"] = (path.parent / "preview.jpg").as_uri()
                result.append(record)
            except (StylesError, OSError):
                self.warn(path, "A damaged saved style was skipped.")
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def warn(self, path, message):
        warning = {"code": "invalid_record", "message": message, "path": str(path)}
        if warning not in self.warnings:
            self.warnings.append(warning)

    def style(self, base, style_id):
        validate_id(style_id)
        path = self.root(base) / "variants" / style_id / "style.json"
        record = read_json(path, {})
        if record.get("base") != base or record.get("id") != style_id:
            raise RecordError("This style does not belong to the current theme.")
        if any(not isinstance(record.get(key), str) or not record[key] for key in ("name", "created_at")):
            raise RecordError(f"Saved style metadata is incomplete: {path}")
        try:
            datetime.fromisoformat(record["created_at"])
        except ValueError as exc:
            raise RecordError(f"Saved style has an invalid creation date: {path}") from exc
        return record

    def preferences(self, base):
        path = self.root(base) / "preferences.json"
        try:
            value = read_json(path, {})
            if any(not isinstance(v, str) for v in value.values()):
                raise RecordError("Invalid agent preferences")
            return value
        except (RecordError, OSError):
            self.warn(path, "Saved agent preferences could not be loaded. Choose an agent again.")
            return {}

    def job(self, base):
        path = self.root(base) / "job.json"
        try:
            job = read_json(path, {})
            if job:
                validate_id(job.get("id"))
                if (job.get("base") != base or not isinstance(job.get("state"), str)
                        or job["state"] not in ACTIVE_STATES | {"done", "failed", "cancelled"}):
                    raise RecordError("Invalid generation status")
                if (not isinstance(job.get("started", 0), (int, float))
                        or not math.isfinite(job.get("started", 0))):
                    raise RecordError("Invalid generation start time")
        except (StylesError, OSError):
            self.warn(path, "The last generation record could not be loaded.")
            return {"state": "failed", "error_code": "invalid_record", "message": "The last generation record is damaged. You can generate again."}
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

    def update_job(self, base, *, expected_id=None, **changes):
        root = self.root(base)
        with lock(root / "job.lock"):
            path = root / "job.json"
            value = read_json(path, {})
            if expected_id is not None and value.get("id") != expected_id:
                raise StylesError("The generation request is no longer active.")
            if changes.get("state") == "applying" and value.get("cancel_requested"):
                raise ProcessCancelled("Generation cancelled.")
            value.update(changes)
            write_json(path, value)
            return value

    def save_variant(self, job, workspace, image, rendered, mode, template_roots, cancel=None):
        root = self.root(job["base"])
        variants = root / "variants"
        variants.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".saving-", dir=variants))
        try:
            theme = staging / "theme"
            # Inherit assets and non-palette extras; Omarchy regenerates supported
            # app configurations from the new palette instead of keeping old colors.
            shutil.copytree(root / "original/theme", theme)
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
            convert_image(image, staging / "preview.jpg", thumbnail=True, cancel=cancel)
            record = {"id": job["id"], "base": job["base"], "name": job["name"], "style": job["style"],
                      "mode": mode, "created_at": now(), "provider": job.get("harness", "codex"),
                      "harness": job.get("harness", "codex"), "model": job.get("model", ""),
                      "thinking": job.get("thinking", ""), "reference": job["reference"]}
            # Keep the source with the style so it survives cleanup of job logs.
            shutil.copyfile(workspace / job["reference"], staging / job["reference"])
            write_json(staging / "style.json", record)
            write_json(theme / MARKER, {"plugin": PLUGIN_ID, "base": job["base"], "style_id": job["id"]})
            if cancel is not None and cancel.exists():
                raise ProcessCancelled("Generation cancelled.")
            staging.rename(variants / job["id"])
            return record
        finally:
            if staging.exists():
                shutil.rmtree(staging)
