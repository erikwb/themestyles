"""Harness configuration shared by account discovery and sandbox launches."""
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import tomllib

PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
                 "META_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "NOUS_API_KEY")
CONFIG_FILES = ("config.toml", "config.json", "settings.json", "models.json", "opencode.json", "crush.json", "mcp.json")
# Config-declared API/MCP variables are permitted; process and desktop overrides are not.
BLOCKED_ENV = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TMPDIR", "BASH_ENV", "ENV",
               "SSH_AUTH_SOCK", "SSH_AGENT_PID", "DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
               "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS"}
ENV_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z_0-9]*)\}|\$([A-Z_][A-Z_0-9]*)|\{env:([A-Z_][A-Z_0-9]*)\}")


@dataclass(frozen=True)
class Harness:
    label: str
    locations: tuple[str, ...]
    override: str = ""
    keys: tuple[str, ...] = PROVIDER_KEYS
    stdin: bool = False

    def paths(self, home, env):
        variables = {"home": str(home), "config": env.get("XDG_CONFIG_HOME", str(home / ".config")),
                     "data": env.get("XDG_DATA_HOME", str(home / ".local/share"))}
        paths = [Path(value.format(**variables)) for value in self.locations]
        if self.override and env.get(self.override):
            paths[0] = Path(env[self.override])
        return paths


HARNESSES = {
    "codex": Harness("Codex", ("{home}/.codex",), "CODEX_HOME", ("OPENAI_API_KEY",), True),
    "grok": Harness("Grok", ("{home}/.grok",), keys=("XAI_API_KEY",)),
    "claude": Harness("Claude Code", ("{home}/.claude", "{home}/.claude.json"), "CLAUDE_CONFIG_DIR", ("ANTHROPIC_API_KEY",), True),
    "pi": Harness("Pi", ("{home}/.pi/agent",), "PI_CODING_AGENT_DIR", stdin=True),
    "opencode": Harness("OpenCode", ("{config}/opencode", "{data}/opencode"), stdin=True),
    "muse": Harness("Muse", ("{config}/muse",), keys=("META_API_KEY",)),
    "gemini": Harness("Gemini", ("{home}/.gemini", "{config}/gcloud"), keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"), stdin=True),
    "copilot": Harness("Copilot", ("{home}/.copilot",), "COPILOT_HOME", ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")),
    "cursor-agent": Harness("Cursor", ("{home}/.cursor", "{config}/cursor"), keys=("CURSOR_API_KEY",)),
    "omp": Harness("Oh My Pi", ("{home}/.omp/agent",), "PI_CODING_AGENT_DIR", stdin=True),
    "hermes": Harness("Hermes", ("{home}/.hermes",), "HERMES_HOME"),
    "openclaw": Harness("OpenClaw", ("{home}/.openclaw",)),
    "crush": Harness("Crush", ("{config}/crush", "{data}/crush"), stdin=True),
}
HARNESS_NAMES = {key: spec.label for key, spec in HARNESSES.items()}


@dataclass
class LaunchRequirements:
    config_roots: list[Path]
    tool_roots: list[Path]
    environment: dict[str, str]


def environment_references(value):
    if isinstance(value, str):
        return {next(group for group in match.groups() if group) for match in ENV_REFERENCE.finditer(value)}
    values = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return set().union(*(environment_references(item) for item in values)) if values else set()


def configured_environment(paths):
    names = set()
    for root in paths:
        for path in ([root] if root.is_file() else [root / name for name in CONFIG_FILES]):
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            try:
                content = path.read_text()
                value = tomllib.loads(content) if path.suffix == ".toml" else json.loads(content)
                names.update(environment_references(value))
            except (OSError, ValueError):
                continue  # Discovery reports malformed harness configuration separately.
    return {key for key in names if key not in BLOCKED_ENV and not key.startswith(("LD_", "DYLD_", "XDG_", "MAGICK_"))}


def installation_root(binary, home, env):
    """Include package siblings without exposing the user's home/config root."""
    path = Path(binary).resolve()
    boundaries = {Path("/"), Path("/home"), home, home.parent,
                  home / ".local", home / ".local/share", home / ".config",
                  Path(env.get("XDG_CONFIG_HOME", home / ".config")),
                  Path(env.get("XDG_DATA_HOME", home / ".local/share"))}
    for parent in path.parents:
        if parent in boundaries or str(parent) in {"/usr", "/opt", "/usr/local"}:
            break
        if (parent / "package.json").is_file() or (parent / "pyvenv.cfg").is_file():
            return parent
    return path if path.parent in boundaries else path.parent


def launch_requirements(name, binary, home, resolve_binary, env=None):
    env = os.environ if env is None else env
    spec = HARNESSES[name]
    roots = spec.paths(home, env)
    keys = {*spec.keys, *configured_environment(roots), spec.override,
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "all_proxy", "no_proxy"}
    environment = {key: env[key] for key in keys if key and key in env}
    mise = Path(env.get("MISE_DATA_DIR", home / ".local/share/mise"))
    tools = [mise / "installs", mise / "shims", installation_root(binary, home, env)]
    tools += [home / path for path in (".local/bin", ".local/share/claude", ".local/share/cursor-agent",
                                      ".bun/bin", ".bun/install/global", ".npm-global", ".opencode/bin")]
    runtime_bins = []
    try:
        with Path(binary).open("rb") as handle:
            first_line = handle.read(4096).split(b"\n", 1)[0]
        if first_line.startswith(b"#!"):
            words = shlex.split(first_line[2:].decode())
            interpreter = words[0]
            if Path(interpreter).name == "env":
                interpreter = next((word for word in words[1:] if not word.startswith("-") and "=" not in word), "")
            resolved = resolve_binary(interpreter) if interpreter else None
            if resolved:
                tools.append(installation_root(resolved, home, env))
                runtime_bins.append(str(Path(resolved).parent))
                # A venv's interpreter may resolve to /usr/bin; retain its packages too.
                if interpreter.startswith("/") and (Path(interpreter).parent.parent / "pyvenv.cfg").is_file():
                    tools.append(Path(interpreter).parent.parent)
    except (OSError, ValueError, IndexError):
        pass
    environment["PATH"] = ":".join([*runtime_bins, env.get("PATH", "/usr/bin:/bin")])
    return LaunchRequirements(roots, list(dict.fromkeys(tools)), environment)
