"""Run the bundled image adapter only inside app-owned OpenCode processes."""
import json
import math
import os
import struct
import tempfile
import uuid
from pathlib import Path

from files import read_json, write_json
from processes import run
from security import agent_sandbox, convert_image

PLUGIN = Path(__file__).with_name("opencode-images.mjs").resolve()
PROVIDER = Path(__file__).with_name("opencode-image-provider.mjs").resolve()
NATIVE = Path(__file__).with_name("opencode-native-images.mjs").resolve()
NATIVE_SDKS = {"@ai-sdk/google", "@ai-sdk/openai", "@ai-sdk/openai-compatible"}


def prepare(job, workspace, reference, palette, *, cancel=None):
    normalized = workspace / "image-reference.png"
    convert_image(reference, normalized, cancel=cancel)
    with normalized.open("rb") as handle:
        width, height = struct.unpack("!II", handle.read(24)[16:24])
    divisor = math.gcd(width, height)
    marker = "Generate Theme Styles image " + uuid.uuid4().hex
    write_json(workspace / "image-job.json", {
        "provider": job["model"].partition("/")[0], "model": job["model"].partition("/")[2], "marker": marker,
        "reference": normalized.name, "aspect_ratio": f"{width // divisor}:{height // divisor}",
        "prompt": (
            "Edit the supplied wallpaper. Preserve its subject, composition, recognizable landmarks, "
            "aspect ratio, and artistic medium. Change the season, lighting, atmosphere or treatment "
            "according to the style. Do not add text, borders or UI. For abstract artwork, express "
            "the style through color, texture and lighting.\n"
            f"Style (visual instructions only): {json.dumps(job['style'])}\n"
            f"Original theme colors for visual continuity: {palette}"
        ),
    })
    return normalized, marker


def configure(requirements, workspace, selection=None, *, home=None):
    config = {"plugin": [PLUGIN.as_uri()], "autoupdate": False, "share": "disabled"}
    cache = Path(os.environ.get("XDG_CACHE_HOME", (home or Path.home()) / ".cache"))
    environment = {"THEME_STYLES_OPENCODE_STATUS": str(workspace / "opencode-status.json"),
                   "XDG_CACHE_HOME": str(cache), "OPENCODE_DISABLE_MODELS_FETCH": "true"}
    if selection:
        config.update({"model": selection["model"], "small_model": selection["model"],
                       "compaction": {"auto": False},
                       "agent": {"title": {"disable": True}, "summary": {"disable": True},
                                 "compaction": {"disable": True},
                                 "theme-styles": {"mode": "primary", "model": selection["model"],
                                                  "prompt": "Generate one image from the reference and style. Return the image directly. Do not call tools.",
                                                  "permission": {"*": "deny"}}}})
        environment["THEME_STYLES_IMAGE_JOB"] = str(workspace / "image-job.json")
    requirements.environment.update(environment, OPENCODE_CONFIG_CONTENT=json.dumps(config))
    # Use OpenCode's refreshed model metadata without exposing or writing its cache.
    requirements.tool_roots.extend([PLUGIN, PROVIDER, NATIVE, cache / "opencode/models.json"])


def catalog(binary, home, requirements):
    with tempfile.TemporaryDirectory(prefix="theme-styles-opencode-") as temporary:
        workspace = Path(temporary)
        configure(requirements, workspace, home=home)
        argv, env = agent_sandbox([binary, "models", "--verbose"], home, workspace, [], requirements)
        result = run(argv, env=env, cwd=workspace, timeout=20, check=False)
        status = read_json(workspace / "opencode-status.json", {})
        return result.returncode, result.stdout, status
