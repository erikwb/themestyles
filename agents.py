"""Discover authenticated harnesses and their eligible model catalogs.

OpenCode requires advertised image input and output. Other harnesses can supply
image tools independently of the model. Never run Omarchy's lazy installation
wrappers or return credentials in the public catalog.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import tomllib

from errors import ProcessTimedOut
from harnesses import HARNESS_NAMES, HARNESSES, launch_requirements
from processes import run as run_command
from processes import stop_process_group

DEFAULT_MODEL = "@default"



class AgentError(ValueError):
    pass


def signed_out(text):
    return bool(re.search(r"not (?:logged in|authenticated)|sign in|log in|login required", text, re.IGNORECASE))


def read(path, *, toml=False):
    try:
        text = Path(path).read_text()
        return tomllib.loads(text) if toml else json.loads(text)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise AgentError(f"Could not read harness settings: {path}") from exc


def safe_binary(name):
    """Resolve installed commands without executing an installation stub."""
    path = shutil.which(name)
    if not path:
        return None
    path = Path(path)
    try:
        with path.open("rb") as handle:
            prefix = handle.read(8192)
        stub = b"mise use " in prefix or b"omarchy-install-" in prefix
        if stub or "mise/shims/" in str(path):
            result = subprocess.run(["mise", "which", name], capture_output=True, text=True, timeout=5)
            if result.returncode:
                return None
            path = Path(result.stdout.strip())
            with path.open("rb") as handle:
                prefix = handle.read(8192)
            if b"mise use " in prefix or b"omarchy-install-" in prefix or "mise/shims/" in str(path):
                return None
        # Muse's launcher can download/update even for --help. Use its installed
        # native binary directly, as recorded by the launcher itself.
        if name == "muse" and prefix.startswith(b"#!") and b"muse-stable" in prefix:
            directory = path.resolve().parent
            version = (directory / ".muse-version").read_text().strip()
            if not re.fullmatch(r"[0-9.]+-R[0-9.]+", version):
                return None
            path = directory / ("muse-bin-" + version)
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
    except (OSError, subprocess.SubprocessError):
        return None


def run(binary, *args):
    result = run_command([binary, *args], timeout=8, check=False)
    return result.returncode, result.stdout + result.stderr


def model(value, label, efforts, default=""):
    levels = list(dict.fromkeys(x for x in efforts if isinstance(x, str) and re.fullmatch(r"[a-z]+", x)))
    return {"value": value, "label": label or value,
            "thinking": [{"value": "", "label": "Default"}] +
                        [{"value": x, "label": x.title()} for x in levels],
            "default_thinking": default if default in levels else ""}


def has_credential(value):
    """Recognize credential fields, never treat mere account metadata as login."""
    if not isinstance(value, dict):
        return False
    keys = {"access", "refresh", "access_token", "refresh_token", "accessToken", "refreshToken",
            "api_key", "apiKey", "key", "token", "oauth_token"}
    return any(isinstance(v, str) and bool(v.strip()) for k, v in value.items() if k in keys)


def json_objects(text):
    """OpenCode's verbose catalog emits a model ID followed by a JSON object."""
    decoder = json.JSONDecoder()
    offset = 0
    while (offset := text.find("{", offset)) >= 0:
        try:
            value, consumed = decoder.raw_decode(text[offset:])
            yield value
            offset += consumed
        except ValueError:
            offset += 1


def copilot_metadata(binary):
    """Use the CLI's SDK protocol for auth/models only; never create a session."""
    process = subprocess.Popen([binary, "--headless", "--no-auto-update", "--stdio",
                                "--log-level", "none"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    answers, buffer = {}, b""
    def request(number, method):
        payload = json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": {}}).encode()
        process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        process.stdin.flush()
    try:
        request(1, "auth.getStatus")
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 4 * 1024 * 1024:
                    break
                while b"\r\n\r\n" in buffer:
                    header, body = buffer.split(b"\r\n\r\n", 1)
                    match = re.search(rb"Content-Length:\s*(\d+)", header, re.I)
                    if not match:
                        raise ValueError("Invalid Copilot RPC header")
                    size = int(match[1])
                    if len(body) < size:
                        break
                    response, buffer = json.loads(body[:size]), body[size:]
                    number = response.get("id")
                    if number not in {1, 2}:
                        continue
                    answers[number] = response.get("result", {})
                    if number == 1:
                        if not answers[1].get("isAuthenticated"):
                            return answers
                        request(2, "models.list")
                    if number == 2:
                        return answers
        return answers
    finally:
        stop_process_group(process)
        process.stdin.close()
        process.stdout.close()


class Agents:
    def __init__(self, home=None):
        self.home = Path(home) if home else Path.home()
        self.config = Path(os.environ.get("XDG_CONFIG_HOME", self.home / ".config"))
        self.data = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        self.diagnostics = []

    def paths(self, harness):
        return HARNESSES[harness].paths(self.home, os.environ)

    def launch_requirements(self, harness, binary):
        return launch_requirements(harness, binary, self.home, safe_binary)

    def codex(self, binary):
        code, status = run(binary, "login", "status")
        if signed_out(status):
            return None
        if code or "Logged in" not in status:
            raise AgentError("Codex returned an unsuccessful or unrecognized account status.")
        directory = self.paths("codex")[0]
        config = read(directory / "config.toml", toml=True)
        models = []
        for entry in read(directory / "models_cache.json").get("models", []):
            if entry.get("visibility") != "list":
                continue
            slug = entry.get("slug", "")
            if slug:
                models.append(model(slug, entry.get("display_name"),
                                    [x.get("effort") for x in entry.get("supported_reasoning_levels", [])],
                                    entry.get("default_reasoning_level", "")))
        return self.entry("codex", "Codex", models, config.get("model", ""),
                          config.get("model_reasoning_effort", ""))

    def grok(self, binary):
        # The account-specific catalog is emitted without starting a conversation.
        code, status = run(binary, "models")
        if signed_out(status):
            return None
        if code or not re.search(r"(?:logged in|authenticated|using.*api.key)", status, re.I):
            raise AgentError("Grok returned an unsuccessful or unrecognized account status.")
        directory = self.paths("grok")[0]
        config = read(directory / "config.toml", toml=True)
        listed = set(re.findall(r"^\s*[-*]\s+(\S+)", status, re.M))
        catalog = read(directory / "models_cache.json")
        models = []
        for slug, entry in catalog.get("models", {}).items():
            info = entry.get("info", {})
            if slug not in listed or info.get("hidden"):
                continue
            models.append(model(slug, info.get("name"),
                                [x.get("value", x.get("id")) for x in info.get("reasoning_efforts", [])],
                                    info.get("reasoning_effort", "")))
        for slug in sorted(listed - {m["value"] for m in models}):
            if not catalog.get("models", {}).get(slug, {}).get("info", {}).get("hidden"):
                models.append(model(slug, slug, []))
        selected = re.search(r"^Default model:\s*(\S+)", status, re.M)
        return self.entry("grok", "Grok", models, selected.group(1) if selected else "",
                          config.get("models", {}).get("default_reasoning_effort", ""))

    def claude(self, binary):
        code, output = run(binary, "auth", "status")
        authenticated = json.loads(output).get("loggedIn")
        if authenticated is False:
            return None
        if code or authenticated is not True:
            raise AgentError("Claude Code returned an unsuccessful or unrecognized account status.")
        directory = self.paths("claude")[0]
        settings = read(directory / "settings.json")
        cache = read(self.paths("claude")[1])
        models = []
        for entry in cache.get("additionalModelOptionsCache", []):
            if entry.get("value"):
                models.append(model(entry["value"], entry.get("label"), ["low", "medium", "high"]))
        for alias in [settings.get("model"), "sonnet", "opus", "haiku"]:
            if alias and alias not in {m["value"] for m in models}:
                models.append(model(alias, alias, [] if "haiku" in alias else ["low", "medium", "high"]))
        return self.entry("claude", "Claude Code", models, settings.get("model", ""), settings.get("effortLevel", ""))

    def pi(self, binary):
        directory = self.paths("pi")[0]
        auth = read(directory / "auth.json")
        stored_providers = {k for k, v in auth.items() if has_credential(v)}
        code, output = run(binary, "--offline", "--list-models")
        if code:
            return self.entry("pi", "Pi", [], "", "") if stored_providers else None
        rows = [line.split() for line in output.splitlines()]
        rows = [r for r in rows if len(r) == 6 and r[4] in {"yes", "no"} and r[5] in {"yes", "no"}]
        # Check readiness through Pi so environment keys and custom providers
        # work too. Never request the --credentials option.
        def ready(provider):
            try:
                check_code, check = run(binary, "auth", "check", "--provider", provider, "--json")
                status = json.loads(check).get("status")
            except ValueError:
                status = None
            except (OSError, subprocess.SubprocessError):
                return None
            return provider if ((status == "ready" and not check_code)
                                or (status is None and provider in stored_providers)) else None
        with ThreadPoolExecutor(max_workers=4) as pool:
            authenticated = set(filter(None, pool.map(ready, dict.fromkeys(r[0] for r in rows))))
        stored = read(directory / "models-store.json")
        custom = read(directory / "models.json").get("providers", {})
        settings = read(directory / "settings.json")
        models = []
        for provider, slug, _context, _limit, reasoning, _images in rows:
            if provider not in authenticated:
                continue
            entries = stored.get(provider, {}).get("models", []) + custom.get(provider, {}).get("models", [])
            info = next((x for x in entries if x.get("id") == slug), {})
            mapping = info.get("thinkingLevelMap", {})
            levels = [x for x in ("off", "minimal", "low", "medium", "high", "xhigh", "max")
                      if mapping.get(x, x if x not in {"xhigh", "max"} else None) is not None] if reasoning == "yes" else []
            models.append(model(provider + "/" + slug, slug + " · " + provider, levels))
        if not models:
            return self.entry("pi", "Pi", [], "", "") if stored_providers else None
        preferred = settings.get("defaultProvider", "") + "/" + settings.get("defaultModel", "")
        return self.entry("pi", "Pi", models, preferred, settings.get("defaultThinkingLevel", ""))

    def opencode(self, binary):
        auth = read(self.paths("opencode")[1] / "auth.json")
        providers = {key for key, value in auth.items() if has_credential(value)}
        # `auth list` also reports provider credentials inherited from the env.
        code, summary = run(binary, "auth", "list")
        if not providers and (code or not re.search(r"[1-9]\d* (?:credentials?|environment variables?)", summary)):
            return None
        code, output = run(binary, "models", "--verbose")
        if code:
            raise AgentError("Could not read OpenCode's image-generation capabilities. Reopen the panel to retry.")
        models = []
        for info in json_objects(output):
            if not info.get("id") or not info.get("providerID"):
                continue
            provider = info["providerID"]
            if providers and provider not in providers:
                continue
            capabilities = info.get("capabilities")
            if not isinstance(capabilities, dict):
                continue
            image_input = capabilities.get("input")
            image_output = capabilities.get("output")
            if not isinstance(image_input, dict) or not isinstance(image_output, dict):
                continue
            # A router's aggregate capabilities do not guarantee its chosen model.
            if image_input.get("image") is not True or image_output.get("image") is not True or info["id"] == "openrouter/auto":
                continue
            models.append(model(provider + "/" + info["id"], info.get("name"), list(info.get("variants", {}))))
        entry = (self.entry("opencode", "OpenCode", models, "", "") if models else
                 {"value": "opencode", "label": "OpenCode", "models": [], "model": "", "thinking": ""})
        entry["notice"] = (
            "Only models advertising image input and output are shown. OpenCode still needs an image-generation tool."
            if models else "No models with image input and output are available from your signed-in OpenCode providers."
        )
        return entry

    def muse(self, binary):
        directory = self.paths("muse")[0]
        auth = read(directory / "auth.json")
        if not (os.environ.get("META_API_KEY") or has_credential(auth)
                or any(has_credential(x) for x in auth.get("providers", auth).values() if isinstance(x, dict))):
            return None
        settings = read(directory / "settings.json")
        selected = settings.get("model", "")
        return self.entry("muse", "Muse", [], selected if isinstance(selected, str) else "", "")

    def gemini(self, binary):
        directory = self.paths("gemini")[0]
        settings = read(directory / "settings.json")
        oauth = read(directory / "oauth_creds.json")
        adc = read(self.paths("gemini")[1] / "application_default_credentials.json")
        if not (has_credential(oauth) or has_credential(adc)
                or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            return None
        chosen = settings.get("model", {}).get("name", "")
        return self.entry("gemini", "Gemini", [], chosen, "")

    def copilot(self, binary):
        responses = copilot_metadata(binary)
        authenticated = responses.get(1, {}).get("isAuthenticated")
        if authenticated is False:
            return None
        if authenticated is not True:
            raise AgentError("Copilot did not return an account status.")
        models = [model(x["id"], x.get("name"), x.get("supportedReasoningEfforts", []),
                        x.get("defaultReasoningEffort", ""))
                  for x in responses.get(2, {}).get("models", []) if x.get("id")]
        directory = self.paths("copilot")[0]
        settings = read(directory / "settings.json") or read(directory / "config.json")
        return self.entry("copilot", "Copilot", models, settings.get("model", ""),
                          settings.get("reasoning_effort", ""))

    def cursor_agent(self, binary):
        code, status = run(binary, "status")
        if signed_out(status):
            return None
        if code or not re.search(r"logged in|authenticated", status, re.I):
            raise AgentError("Cursor returned an unsuccessful or unrecognized account status.")
        settings = read(self.paths("cursor-agent")[0] / "cli-config.json")
        selected = settings.get("model", "")
        if isinstance(selected, dict):
            selected = selected.get("id", "")
        return self.entry("cursor-agent", "Cursor", [], selected, "")

    def omp(self, binary):
        directory = self.paths("omp")[0]
        providers = set()
        database = directory / "agent.db"
        if database.is_file():
            # Read only, never migrate or create an agent database.
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
                    columns = {r[1] for r in connection.execute("PRAGMA table_info(auth_credentials)")}
                    active = " WHERE disabled_cause IS NULL" if "disabled_cause" in columns else ""
                    providers.update(r[0] for r in connection.execute(
                        "SELECT DISTINCT provider FROM auth_credentials" + active))
            except sqlite3.Error:
                pass
        legacy = read(directory / "auth.json")
        providers.update(k for k, v in legacy.items() if has_credential(v))
        if not providers:
            return None
        code, output = run(binary, "--list-models")
        models = []
        if not code:
            for row in (line.split() for line in output.splitlines()):
                if len(row) >= 2 and row[0] in providers:
                    models.append(model(row[0] + "/" + row[1], row[1] + " · " + row[0], []))
        return self.entry("omp", "Oh My Pi", models, "", "")

    def hermes(self, binary):
        directory = self.paths("hermes")[0]
        auth = read(directory / "auth.json")
        credentials = list(auth.get("providers", {}).values())
        for pool in auth.get("credential_pool", {}).values():
            if isinstance(pool, list):
                credentials.extend(pool)
        # Hermes also stores provider keys in its .env; inspect values without
        # executing shell expansion or returning them in the public catalog.
        keys = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
                "XAI_API_KEY", "NOUS_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY"}
        configured = any(os.environ.get(k) for k in keys)
        try:
            for line in (directory / ".env").read_text().splitlines():
                key, separator, value = line.removeprefix("export ").partition("=")
                if separator and key.strip() in keys and value.strip().strip("\"'"):
                    configured = True
        except OSError:
            pass
        if not configured and not any(has_credential(x) for x in credentials):
            return None
        return self.entry("hermes", "Hermes", [], "", "")

    def openclaw(self, binary):
        code, output = run(binary, "models", "status", "--json")
        if code:
            return None
        status = json.loads(output)
        auth = status.get("auth", {})
        providers = [p for p in auth.get("providers", [])
                     if p.get("profiles", {}).get("count", 0) or p.get("env") or p.get("modelsJson")]
        if not providers:
            return None
        code, output = run(binary, "models", "list", "--json")
        models = []
        if not code:
            models = [model(x["key"], x.get("name"), []) for x in json.loads(output).get("models", [])
                      if x.get("key") and x.get("available", True)]
        return self.entry("openclaw", "OpenClaw", models, status.get("resolvedDefault", ""), "")

    def crush(self, binary):
        settings = read(self.paths("crush")[0] / "crush.json")
        stored = read(self.paths("crush")[1] / "crush.json")
        providers = settings.get("providers", {}) | stored.get("providers", {})
        active = {}
        for key, info in providers.items():
            api_key = info.get("api_key", "")
            if api_key.startswith("$"):
                variable = re.fullmatch(r"\$\{?([A-Z_][A-Z_0-9]*)\}?", api_key)
                api_key = os.environ.get(variable[1], "") if variable else ""
            if not info.get("disable") and (api_key or has_credential(info.get("oauth"))):
                active[key] = info
        if not active:
            return None
        models = []
        for provider, info in active.items():
            for item in info.get("models", []) + info.get("chatgpt_models", []):
                if item.get("id"):
                    models.append(model(provider + "/" + item["id"], item.get("name"), []))
        chosen = (settings.get("models", {}) | stored.get("models", {})).get("large", {})
        preferred = (chosen.get("provider", "") + "/" + chosen["model"]) if chosen.get("model") else ""
        return self.entry("crush", "Crush", models, preferred, "")

    @staticmethod
    def entry(value, label, models, preferred, thinking):
        if not models:
            models = [model(preferred or DEFAULT_MODEL, preferred or "Harness default", [])]
        selected = next((m for m in models if m["value"] == preferred), models[0])
        levels = {x["value"] for x in selected["thinking"]}
        return {"value": value, "label": label, "models": models,
                "model": selected["value"], "thinking": thinking if thinking in levels else ""}

    def catalog(self, harness=None):
        def inspect(name):
            try:
                binary = safe_binary(name)
                if not binary:
                    return None, None
                return getattr(self, name.replace("-", "_"))(binary), None
            except Exception as exc:
                # Keep other accounts usable, but do not turn adapter bugs into a
                # silent signed-out result. Exception payloads may contain credentials.
                code = "discovery_timeout" if isinstance(exc, ProcessTimedOut) else "discovery_failed"
                message = ("Account discovery timed out." if code == "discovery_timeout" else str(exc)
                           if isinstance(exc, AgentError) else f"Could not read account/model information ({type(exc).__name__}).")
                return None, {"harness": name, "label": HARNESS_NAMES[name], "code": code, "message": message}
        with ThreadPoolExecutor(max_workers=4) as pool:
            names = [harness] if harness in HARNESS_NAMES else ([] if harness else HARNESS_NAMES)
            results = list(pool.map(inspect, names))
        self.diagnostics = [diagnostic for _, diagnostic in results if diagnostic]
        return [entry for entry, _ in results if entry]

    def selection(self, saved=None, catalog=None):
        entries = self.catalog() if catalog is None else catalog
        if not entries:
            return {}
        saved = saved or {}
        entry = next((x for x in entries if x["value"] == saved.get("harness")), entries[0])
        selected = next((m for m in entry["models"] if m["value"] == saved.get("model")), None)
        if selected is None:
            return {"harness": entry["value"], "model": entry["model"], "thinking": entry["thinking"]}
        effort = saved.get("thinking", "")
        return {"harness": entry["value"], "model": selected["value"],
                "thinking": effort if effort in {x["value"] for x in selected["thinking"]} else ""}

    def diagnostic_message(self):
        return " ".join(f"{item['label']}: {item['message']}" for item in self.diagnostics)

    def validate(self, selection):
        catalog = self.catalog(harness=selection.get("harness")) if selection else []
        if not catalog and self.diagnostics:
            raise AgentError(self.diagnostic_message())
        if catalog and not catalog[0]["models"]:
            raise AgentError(catalog[0].get("notice", "No eligible models are available for this harness."))
        if not selection or self.selection(selection, catalog) != selection:
            raise AgentError("That harness, model, or thinking level is no longer available. Reopen the panel to refresh.")
        return selection

    def command(self, selection, workspace, reference):
        name = selection["harness"]
        binary = safe_binary(name)
        if not binary:
            raise AgentError("The selected harness is no longer installed.")
        chosen, effort = selection["model"], selection["thinking"]
        choice = [] if chosen == DEFAULT_MODEL else ["--model", chosen]
        if name == "codex":
            argv = [binary, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "workspace-write",
                    "--color", "never", "--json", *choice,
                    "--image", str(reference), "--output-schema", str(workspace / "response-schema.json"),
                    "--output-last-message", str(workspace / "response.json")]
            if effort:
                argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
            return argv + ["-"]
        if name == "grok":
            argv = [binary, "--prompt-file", str(workspace / "prompt.txt"), "--cwd", str(workspace),
                    *choice, "--output-format", "json", "--permission-mode", "auto",
                    "--disable-web-search", "--no-subagents"]
            if effort:
                argv += ["--reasoning-effort", effort]
            return argv
        if name == "claude":
            argv = [binary, "--print", "--output-format", "json", "--no-session-persistence",
                    "--permission-mode", "auto", *choice]
            return argv + (["--effort", effort] if effort else [])
        if name in {"pi", "omp"}:
            argv = [binary, "--print", "--mode", "json", "--no-session", *choice]
            if effort:
                argv += ["--thinking", effort]
            return argv + ["@" + str(reference)]
        if name == "opencode":
            argv = [binary, "run", "--format", "json", "--dir", str(workspace), *choice]
            if effort:
                argv += ["--variant", effort]
            return argv
        if name == "muse":
            argv = [binary, "exec", "--json", "--prompt-file", str(workspace / "prompt.txt"),
                    "--image", str(reference), "--workspace", str(workspace), "--no-session-log",
                    "--approval-mode", "never", *choice]
            return argv + (["--reasoning-effort", effort] if effort else [])
        if name == "gemini":
            return [binary, "--output-format", "json", *choice]
        if name == "copilot":
            return [binary, "--no-auto-update", "--prompt", (workspace / "prompt.txt").read_text(),
                    *choice] + (["--effort", effort] if effort else [])
        if name == "cursor-agent":
            return [binary, "--print", "--output-format", "json", *choice,
                    (workspace / "prompt.txt").read_text()]
        if name == "hermes":
            return [binary, "chat", "--query-file", str(workspace / "prompt.txt"), *choice]
        if name == "openclaw":
            return [binary, "agent", "--local", "--message-file", str(workspace / "prompt.txt"),
                    "--session-id", "theme-styles-" + workspace.name, "--json", *choice]
        if name == "crush":
            return [binary, "run", "--quiet", *choice]
        raise AgentError("No image-generation adapter for the selected harness.")

    @staticmethod
    def uses_stdin(harness):
        return HARNESSES[harness].stdin
