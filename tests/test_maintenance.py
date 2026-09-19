"""Regression cases for portability, recoverable records and process errors."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import StyleFixture, png

from agents import Agents
from errors import ProcessCancelled, ProcessOutputLimit, ProcessTimedOut
from security import agent_sandbox, convert_image


class StoreRecoveryTests(StyleFixture, unittest.TestCase):
    def test_damaged_styles_are_isolated_and_reported_without_mutating_them(self):
        job = self.request()
        self.complete(job)
        bad = self.service.root("alpha") / "variants" / ("a" * 32) / "style.json"
        bad.parent.mkdir()
        invalid = ['{"base":', '[]', '{"base":"alpha"}',
                   json.dumps({"id": "a" * 32, "base": "alpha", "name": 3, "created_at": "yesterday"})]
        for content in invalid:
            with self.subTest(content=content):
                bad.write_text(content)
                status = self.service.status()
                self.assertTrue(status["ok"])
                self.assertEqual([style["id"] for style in status["styles"]], [job["id"]])
                self.assertEqual(status["warnings"][0]["path"], str(bad))
                self.assertEqual(bad.read_text(), content)
        bad.unlink()
        self.assertEqual(self.service.status()["warnings"], [])

    def test_damaged_job_does_not_hide_valid_styles(self):
        job = self.request()
        self.complete(job)
        path = self.service.root("alpha") / "job.json"
        for content in ('[1]', '{"id":"bad"}', json.dumps(job | {"state": []})):
            with self.subTest(content=content):
                path.write_text(content)
                status = self.service.status()
                self.assertTrue(status["ok"])
                self.assertEqual(len(status["styles"]), 1)
                self.assertEqual(status["job"]["error_code"], "invalid_record")

    def test_timeout_category_does_not_depend_on_message_wording(self):
        job = self.request()
        with patch.object(self.service, "run_process", side_effect=ProcessTimedOut("Deadline expired")):
            self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["error_code"], "timeout")
        self.assertEqual(self.service.job("alpha")["message"], "Deadline expired")

    def test_log_limit_reaches_the_ui_without_becoming_an_image_support_error(self):
        job = self.request()
        with patch.object(self.service, "run_process", side_effect=ProcessOutputLimit("Log limit reached")):
            self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["error_code"], "output_limit")
        self.assertEqual(self.service.job("alpha")["message"], "Log limit reached")

    def test_cancel_during_preview_does_not_publish_a_style(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        def cancelled_preview(*_args, **_kwargs):
            (workspace / "cancel").touch()
            raise ProcessCancelled("Generation cancelled.")
        with patch("storage.convert_image", side_effect=cancelled_preview), self.assertRaises(ProcessCancelled):
            self.complete(job)
        self.assertEqual(self.service.variants("alpha"), [])
        self.assertEqual(list((self.service.root("alpha") / "variants").glob(".saving-*")), [])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.workspace = self.home / "job"
        self.workspace.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.environ["PATH"]}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.agents = Agents(self.home)

    def launch(self, argv, harness="crush"):
        requirements = self.agents.launch_requirements(harness, argv[0])
        command, env = agent_sandbox(argv, self.home, self.workspace, [], requirements)
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)

    def test_configured_custom_provider_key_reaches_sandbox(self):
        config = self.home / ".config/crush"
        config.mkdir(parents=True)
        (config / "crush.json").write_text(json.dumps({"providers": {"custom": {"api_key": "$CUSTOM_LLM_KEY"}}}))
        with patch.dict(os.environ, {"CUSTOM_LLM_KEY": "fixture-only", "UNRELATED_SECRET": "must-not-export"}):
            self.assertIsNotNone(self.agents.crush("/unused"))
            result = self.launch([sys.executable, "-c",
                                 "import os; assert os.environ['CUSTOM_LLM_KEY'] == 'fixture-only'; assert 'UNRELATED_SECRET' not in os.environ"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_cannot_export_loader_or_desktop_environment(self):
        config = self.home / ".config/crush"
        config.mkdir(parents=True)
        (config / "crush.json").write_text(json.dumps({"providers": {"custom": {"api_key": "$CUSTOM_LLM_KEY"}},
                                                       "env": ["$SSH_AUTH_SOCK", "$LD_PRELOAD", "$DBUS_SESSION_BUS_ADDRESS"]}))
        with patch.dict(os.environ, {"CUSTOM_LLM_KEY": "fixture", "SSH_AUTH_SOCK": "/private/socket",
                                    "LD_PRELOAD": "/private/library", "DBUS_SESSION_BUS_ADDRESS": "private"}):
            requirements = self.agents.launch_requirements("crush", sys.executable)
        self.assertNotIn("SSH_AUTH_SOCK", requirements.environment)
        self.assertNotIn("LD_PRELOAD", requirements.environment)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", requirements.environment)

    def test_opencode_jsonc_can_reference_a_custom_key(self):
        config = self.home / ".config/opencode"
        config.mkdir(parents=True)
        (config / "opencode.jsonc").write_text('''{
            // Credentials remain in the environment.
            "provider": {"openrouter": {"options": {"apiKey": "{env:CUSTOM_IMAGE_KEY}"}}},
        }''')
        with patch.dict(os.environ, {"CUSTOM_IMAGE_KEY": "fixture", "UNRELATED_SECRET": "private"}):
            requirements = self.agents.launch_requirements("opencode", sys.executable)
        self.assertEqual(requirements.environment["CUSTOM_IMAGE_KEY"], "fixture")
        self.assertNotIn("UNRELATED_SECRET", requirements.environment)

    def test_jsonc_comments_do_not_expose_secrets_to_the_sandbox(self):
        config = self.home / ".config/opencode"
        config.mkdir(parents=True)
        (config / "opencode.jsonc").write_text('''{
            // Disabled integration: {env:COMMENT_SECRET}
            /* "apiKey": "${BLOCK_SECRET}", */
            "provider": {"custom": {"options": {
                "apiKey": "{env:ACTIVE_KEY}",
                "baseURL": "https://example.invalid/a/*literal*/",
            }}},
            "instructions": ["quote: \\\" // still a string",],
        }''')
        with patch.dict(os.environ, {"ACTIVE_KEY": "synthetic", "COMMENT_SECRET": "private", "BLOCK_SECRET": "private"}):
            result = self.launch([sys.executable, "-c", "import os; assert os.environ['ACTIVE_KEY']=='synthetic'; "
                                 "assert 'COMMENT_SECRET' not in os.environ; assert 'BLOCK_SECRET' not in os.environ"], "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_jsonc_does_not_export_environment_references(self):
        config = self.home / ".config/opencode"
        config.mkdir(parents=True)
        for content in ('{"key":"{env:PRIVATE_KEY}", /* unfinished',
                        '{"key":"{env:PRIVATE_KEY}" trailing}',
                        '{invalid:"{env:PRIVATE_KEY}"}'):
            with self.subTest(content=content), patch.dict(os.environ, {"PRIVATE_KEY": "synthetic"}):
                (config / "opencode.jsonc").write_text(content)
                requirements = self.agents.launch_requirements("opencode", sys.executable)
                self.assertNotIn("PRIVATE_KEY", requirements.environment)

    def test_custom_install_can_import_its_package_siblings(self):
        package = self.home / "custom-install"
        package.mkdir()
        executable = package / "agent"
        executable.write_text('#!/usr/bin/env python3\nimport sibling\nassert sibling.value == 42\n')
        executable.chmod(0o700)
        (package / "sibling.py").write_text('value = 42\n')
        result = self.launch([str(executable)], "pi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((package / "__pycache__").exists())

    def test_custom_config_directory_is_shared_by_discovery_and_launch(self):
        config = self.home / "custom-pi"
        config.mkdir()
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(config)}):
            requirements = self.agents.launch_requirements("pi", sys.executable)
            self.assertEqual(self.agents.paths("pi")[0], config)
            self.assertEqual(requirements.config_roots[0], config)
            self.assertEqual(requirements.environment["PI_CODING_AGENT_DIR"], str(config))

    def test_discovery_reports_errors_without_exposing_exception_payloads(self):
        with patch("agents.safe_binary", return_value="/fake"), \
                patch.object(self.agents, "codex", side_effect=AttributeError("sensitive fixture")):
            self.assertEqual(self.agents.catalog(harness="codex"), [])
        self.assertEqual(self.agents.diagnostics[0]["code"], "discovery_failed")
        self.assertIn("AttributeError", self.agents.diagnostics[0]["message"])
        self.assertNotIn("sensitive fixture", json.dumps(self.agents.diagnostics))
        with patch("agents.safe_binary", return_value="/fake"), patch.object(self.agents, "codex", return_value=None):
            self.assertEqual(self.agents.catalog(harness="codex"), [])
        self.assertEqual(self.agents.diagnostics, [])

    def test_discovery_timeout_has_its_own_diagnostic(self):
        with patch("agents.safe_binary", return_value="/fake"), \
                patch.object(self.agents, "codex", side_effect=ProcessTimedOut("arbitrary text")):
            self.agents.catalog(harness="codex")
        self.assertEqual(self.agents.diagnostics[0]["code"], "discovery_timeout")

    def test_failed_auth_command_is_distinct_from_signed_out(self):
        with patch("agents.safe_binary", return_value="/fake"), patch("agents.run", return_value=(2, "unknown subcommand")):
            self.assertEqual(self.agents.catalog(harness="codex"), [])
        self.assertEqual(self.agents.diagnostics[0]["code"], "discovery_failed")
        with patch("agents.safe_binary", return_value="/fake"), patch("agents.run", return_value=(1, "Not logged in")):
            self.assertEqual(self.agents.catalog(harness="codex"), [])
        self.assertEqual(self.agents.diagnostics, [])

    def test_image_conversion_can_be_cancelled_while_decoder_runs(self):
        source = self.workspace / "source.png"
        png(source)
        marker = self.workspace / "cancel"
        timer = threading.Timer(0.15, marker.touch)
        timer.start()
        started = time.monotonic()
        try:
            with patch("security.sandbox", return_value=([sys.executable, "-c", "import time; time.sleep(10)"], dict(os.environ))):
                with self.assertRaises(ProcessCancelled):
                    convert_image(source, self.workspace / "result.png", cancel=marker)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            timer.join()
        self.assertFalse((self.workspace / "result.png").exists())
