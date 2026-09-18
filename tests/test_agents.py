"""Discovery uses synthetic auth responses: never calls a live model."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agents import (
    DEFAULT_MODEL,
    HARNESS_NAMES,
    AgentError,
    Agents,
    model,
    safe_binary,
)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict("os.environ", {"PATH": os.environ["PATH"],
                              "CODEX_HOME": str(self.home / ".codex")}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.agents = Agents(self.home)

    def write(self, path, value):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))

    def codex_catalog(self):
        self.write(".codex/models_cache.json", {"models": [
            {"slug": "image-agent", "visibility": "list", "input_modalities": ["text", "image"],
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
            {"slug": "text-only", "visibility": "list", "input_modalities": ["text"]},
            {"slug": "hidden", "visibility": "hide", "input_modalities": ["image"]}]})

    def test_codex_accepts_api_login_and_text_models_without_image_feature_probe(self):
        self.codex_catalog()
        with patch("agents.run", return_value=(1, "Not logged in")):
            self.assertIsNone(self.agents.codex("/codex"))
        for login in ("ChatGPT", "an API key"):
            with patch("agents.run", return_value=(0, "Logged in using " + login)) as run:
                result = self.agents.codex("/codex")
                run.assert_called_once_with("/codex", "login", "status")
            self.assertEqual([m["value"] for m in result["models"]], ["image-agent", "text-only"])
            self.assertEqual([x["value"] for x in result["models"][0]["thinking"]], ["", "low", "high"])

    def test_signed_in_harness_without_model_cache_uses_its_default(self):
        with patch("agents.run", return_value=(0, "Logged in using ChatGPT")):
            result = self.agents.codex("/codex")
        self.assertEqual(result["model"], DEFAULT_MODEL)
        self.assertEqual(result["models"][0]["label"], "Harness default")

    def test_discovery_skips_uninstalled_and_isolates_broken_catalogs(self):
        with patch("agents.safe_binary", side_effect=lambda n: "/" + n if n in {"claude", "codex"} else None), \
             patch.object(self.agents, "codex", side_effect=ValueError("bad cache")), \
             patch("agents.run", return_value=(0, '{"loggedIn": true}')):
            self.assertEqual([a["value"] for a in self.agents.catalog()], ["claude"])

    def test_validating_selection_does_not_probe_unrelated_harnesses(self):
        with patch("agents.safe_binary", return_value="/codex") as binary, \
             patch("agents.run", return_value=(0, "Logged in using ChatGPT")):
            selection = {"harness": "codex", "model": DEFAULT_MODEL, "thinking": ""}
            self.assertEqual(self.agents.validate(selection), selection)
            binary.assert_called_once_with("codex")
        with patch("agents.safe_binary") as binary:
            with self.assertRaises(AgentError):
                self.agents.validate({"harness": "unknown", "model": DEFAULT_MODEL, "thinking": ""})
            binary.assert_not_called()

    def test_grok_keeps_custom_models_but_excludes_hidden_and_stale_models(self):
        self.write(".grok/models_cache.json", {"auth_method": "session", "models": {
            "grok-image": {"info": {"name": "Grok", "model_family": "xai", "reasoning_efforts": [{"value": "low"}]}},
            "private": {"info": {"model_family": "xai", "hidden": True}},
            "custom": {"info": {"model_family": "custom"}},
            "stale": {"info": {"model_family": "xai"}}}})
        with patch("agents.run", return_value=(0, "You are logged in with grok.com.\nDefault model: grok-image\n"
                                               " * grok-image\n - private\n - custom\n")):
            result = self.agents.grok("/grok")
        self.assertEqual([m["value"] for m in result["models"]], ["grok-image", "custom"])
        for status in ("Sign in to Grok", "You are not logged in"):
            with patch("agents.run", return_value=(0, status)):
                self.assertIsNone(self.agents.grok("/grok"))

    def test_claude_reads_account_status_and_configured_model(self):
        self.write(".claude/settings.json", {"model": "account-specific", "effortLevel": "high"})
        with patch("agents.run", return_value=(0, '{"loggedIn": true}')):
            result = self.agents.claude("/claude")
        self.assertEqual(result["model"], "account-specific")
        self.assertEqual(result["thinking"], "high")
        with patch("agents.run", return_value=(0, '{"loggedIn": false}')):
            self.assertIsNone(self.agents.claude("/claude"))

    def test_pi_filters_authentication_but_keeps_text_only_models(self):
        listing = "first text-model 32K 8K yes no\nsecond image-model 32K 8K yes yes\n"
        def command(binary, *args):
            if args[-1] == "--list-models":
                return 0, listing
            return 0, json.dumps({"status": "ready" if args[3] == "first" else "missing"})
        with patch("agents.run", side_effect=command):
            result = self.agents.pi("/pi")
        self.assertEqual([m["value"] for m in result["models"]], ["first/text-model"])

    def test_pi_older_cli_can_use_stored_login_and_harness_default(self):
        self.write(".pi/agent/auth.json", {"custom": {"access": "secret"}})
        with patch("agents.run", return_value=(1, "unknown option")):
            self.assertEqual(self.agents.pi("/pi")["model"], DEFAULT_MODEL)

    def test_opencode_reads_only_authenticated_provider_models(self):
        self.write(".local/share/opencode/auth.json", {"mine": {"key": "secret"}, "other": {"type": "oauth"}})
        catalog = '\n'.join(json.dumps({"id": "model", "providerID": p, "variants": {"high": {}},
                                       "capabilities": {"input": {"image": True}, "output": {"image": True}}})
                            for p in ("mine", "other"))
        with patch("agents.run", side_effect=[(0, "1 credential"), (0, catalog)]):
            result = self.agents.opencode("/opencode")
        self.assertEqual([m["value"] for m in result["models"]], ["mine/model"])
        self.assertNotIn("secret", json.dumps(result))

    def test_opencode_requires_image_input_and_output_and_excludes_router(self):
        self.write(".local/share/opencode/auth.json", {"mine": {"key": "secret"}})
        capabilities = {"input": {"image": True}, "output": {"image": True}}
        entries = [
            {"id": "new-image-model", "capabilities": capabilities, "variants": {"high": {}}},
            {"id": "vision", "capabilities": {"input": {"image": True}, "output": {"image": False}}},
            {"id": "text-to-image", "capabilities": {"input": {"image": False}, "output": {"image": True}}},
            {"id": "unknown"},
            {"id": "malformed", "capabilities": {"input": True, "output": "image"}},
            {"id": "string-boolean", "capabilities": {"input": {"image": True}, "output": {"image": "true"}}},
            {"id": "openrouter/auto", "capabilities": capabilities},
        ]
        output = '\n'.join(json.dumps(dict(entry, providerID="mine")) for entry in entries)
        with patch("agents.run", side_effect=[(0, "1 credential"), (0, output)]):
            result = self.agents.opencode("/opencode")
        self.assertEqual([m["value"] for m in result["models"]], ["mine/new-image-model"])
        self.assertEqual([t["value"] for t in result["models"][0]["thinking"]], ["", "high"])
        saved = {"harness": "opencode", "model": "mine/vision", "thinking": "high"}
        self.assertEqual(self.agents.selection(saved, [result]),
                         {"harness": "opencode", "model": "mine/new-image-model", "thinking": ""})
        with patch.object(self.agents, "catalog", return_value=[result]):
            with self.assertRaises(AgentError):
                self.agents.validate(saved)

    def test_opencode_empty_image_catalog_never_uses_harness_default(self):
        self.write(".local/share/opencode/auth.json", {"mine": {"key": "secret"}})
        with patch("agents.run", side_effect=[(0, "1 credential"), (0, '{"id":"text","providerID":"mine"}')]):
            result = self.agents.opencode("/opencode")
        self.assertEqual(result["models"], [])
        self.assertEqual(result["model"], "")
        self.assertIn("No models with image input and output", result["notice"])
        with patch.object(self.agents, "catalog", return_value=[result]):
            for chosen in ("", DEFAULT_MODEL, "mine/text"):
                with self.subTest(model=chosen), self.assertRaisesRegex(AgentError, "No models with image"):
                    self.agents.validate({"harness": "opencode", "model": chosen, "thinking": ""})

    def test_opencode_catalog_failure_does_not_fall_back_to_unchecked_model(self):
        self.write(".local/share/opencode/auth.json", {"mine": {"key": "secret"}})
        with patch("agents.run", side_effect=[(0, "1 credential"), (1, "failure")]):
            with self.assertRaisesRegex(AgentError, "Could not read OpenCode"):
                self.agents.opencode("/opencode")

    def test_file_based_adapters_require_credentials_not_just_account_metadata(self):
        cases = [("gemini", ".gemini/oauth_creds.json", {"access_token": "secret"}),
                 ("muse", ".config/muse/auth.json", {"access_token": "secret"}),
                 ("hermes", ".hermes/auth.json", {"providers": {"custom": {"access_token": "secret"}}}),
                 ("crush", ".config/crush/crush.json", {"providers": {"custom": {"api_key": "secret"}}})]
        for name, path, credentials in cases:
            with self.subTest(harness=name):
                self.write(path, {"email": "person@example.com"})
                self.assertIsNone(getattr(self.agents, name)("/" + name))
                self.write(path, credentials)
                result = getattr(self.agents, name)("/" + name)
                self.assertEqual(result["value"], name)
                self.assertNotIn("secret", json.dumps(result))

    def test_cursor_uses_status_and_configured_model(self):
        self.write(".cursor/cli-config.json", {"model": {"id": "custom-model"}})
        with patch("agents.run", return_value=(0, "Logged in as person@example.com")):
            self.assertEqual(self.agents.cursor_agent("/cursor")["model"], "custom-model")
        with patch("agents.run", return_value=(0, "Not authenticated")):
            self.assertIsNone(self.agents.cursor_agent("/cursor"))

    def test_omp_excludes_disabled_credentials_without_changing_database(self):
        path = self.home / ".omp/agent/agent.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE auth_credentials(provider TEXT, disabled_cause TEXT)")
            db.executemany("INSERT INTO auth_credentials VALUES (?, ?)", [("mine", None), ("old", "logout")])
            db.commit()
        before = path.read_bytes()
        with patch("agents.run", return_value=(0, "mine model 32K 8K yes no\nold hidden 32K 8K yes yes")):
            self.assertEqual([m["value"] for m in self.agents.omp("/omp")["models"]], ["mine/model"])
        self.assertEqual(before, path.read_bytes())

    def test_openclaw_status_uses_credentials_without_a_generation_probe(self):
        status = {"resolvedDefault": "provider/model", "auth": {"providers": [
            {"provider": "provider", "profiles": {"count": 1}}]}}
        with patch("agents.run", side_effect=[(0, json.dumps(status)), (0, '{"models": []}')]) as run:
            result = self.agents.openclaw("/openclaw")
        self.assertEqual(result["model"], "provider/model")
        self.assertNotIn("--probe", str(run.call_args_list))

    def test_copilot_uses_auth_and_model_rpc_without_a_conversation(self):
        binary = self.home / "copilot"
        binary.write_text('''#!/usr/bin/env python3
import json, sys
for expected, result in [("auth.getStatus", {"isAuthenticated": True}),
                          ("models.list", {"models": [{"id": "account-model", "name": "Model"}]})]:
    size = int(sys.stdin.buffer.readline().split(b":")[1])
    sys.stdin.buffer.readline()
    request = json.loads(sys.stdin.buffer.read(size))
    assert request["method"] == expected
    data = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\\r\\n\\r\\n".encode() + data)
    sys.stdout.buffer.flush()
''')
        binary.chmod(0o755)
        result = self.agents.copilot(str(binary))
        self.assertEqual(result["model"], "account-model")
        with patch("agents.copilot_metadata", return_value={1: {"isAuthenticated": False}}):
            self.assertIsNone(self.agents.copilot(str(binary)))

    def test_cold_omarchy_stub_is_never_executed(self):
        stub = self.home / "grok"
        stub.write_text("#!/bin/sh\nmise use -g grok\n")
        stub.chmod(0o755)
        with patch("agents.shutil.which", return_value=str(stub)), \
             patch("agents.subprocess.run") as run:
            run.return_value.returncode = 1
            self.assertIsNone(safe_binary("grok"))
            self.assertEqual(run.call_args.args[0], ["mise", "which", "grok"])

    def test_switching_harness_cannot_reuse_foreign_model_or_effort(self):
        catalog = [Agents.entry("grok", "Grok", [model("grok-image", "Grok", ["low"], "low")], "grok-image", "")]
        stale = {"harness": "codex", "model": "gpt", "thinking": "ultra"}
        with patch.object(self.agents, "catalog", return_value=catalog):
            with self.assertRaises(AgentError):
                self.agents.validate(stale)
        self.assertEqual(self.agents.selection(stale, catalog),
                         {"harness": "grok", "model": "grok-image", "thinking": ""})

    def test_each_harness_receives_explicit_model_and_supported_effort(self):
        (self.home / "prompt.txt").write_text("Edit the wallpaper")
        with patch("agents.safe_binary", side_effect=lambda name: "/tools/" + name):
            for harness in HARNESS_NAMES:
                command = self.agents.command({"harness": harness, "model": "chosen", "thinking": "low"},
                                              self.home, self.home / "reference.png")
                self.assertEqual(command[0], "/tools/" + harness)
                self.assertEqual(command[command.index("--model") + 1], "chosen")
                self.assertNotIn("--yolo", command)
                self.assertNotIn("bypassPermissions", command)
                self.assertNotIn("--deliver", command)
                self.assertNotIn("--to", command)

    def test_harness_default_does_not_send_fake_model_name(self):
        (self.home / "prompt.txt").write_text("Edit the wallpaper")
        with patch("agents.safe_binary", return_value="/agent"):
            for harness in HARNESS_NAMES:
                command = self.agents.command({"harness": harness, "model": DEFAULT_MODEL, "thinking": ""},
                                              self.home, self.home / "reference.png")
                self.assertNotIn(DEFAULT_MODEL, command)
                self.assertNotIn("--model", command)


if __name__ == "__main__":
    unittest.main()
