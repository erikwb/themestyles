"""Exercise the real OpenCode adapter against a local service, never a paid model."""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from support import png

import opencode_images
from agents import Agents, json_objects, safe_binary
from processes import run
from security import agent_sandbox
from theme_styles import Styles

OPENCODE = safe_binary("opencode")


@unittest.skipUnless(OPENCODE and shutil.which("bwrap"), "OpenCode and Bubblewrap required")
class OpenCodeImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.work = self.home / "job"
        self.work.mkdir()
        self.config = self.home / ".config/opencode"
        self.config.mkdir(parents=True)
        self.auth = self.home / ".local/share/opencode/auth.json"
        self.auth.parent.mkdir(parents=True)
        self.auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "test-key-not-a-credential"}}))
        self.env = patch.dict(os.environ, {"PATH": os.environ["PATH"], "HOME": str(self.home),
                                          "XDG_CONFIG_HOME": str(self.config.parent),
                                          "XDG_DATA_HOME": str(self.auth.parent.parent)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.request_headers = []
        self.error = 0
        self.bad_image = False
        self.protocol = "router"
        self.catalog_error = 0
        self.text_only = False
        self.truncated = False
        image = self.work / "reference.png"
        png(image)
        self.image = image.read_bytes()
        model = {"id": "test/image", "name": "Test image", "architecture": {
            "input_modalities": ["image", "text"], "output_modalities": ["image"]},
            "supported_parameters": {"input_references": {"min": 0, "max": 1},
                                     "output_format": {"values": ["png"]}}}
        self.models = [model, dict(model, id="test/svg", supported_parameters={
            "input_references": {"max": 1}, "output_format": {"values": ["svg"]}}),
            dict(model, id="test/no-reference", supported_parameters={}),
            dict(model, id="openrouter/auto")]
        fixture = self

        class API(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.requests.append(("GET", self.path))
                self.send_response(fixture.catalog_error or 200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": fixture.models}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                fixture.requests.append(("POST", self.path, body,
                                         self.headers.get("Authorization") or self.headers.get("x-goog-api-key")))
                self.send_response(fixture.error or 200)
                if fixture.protocol != "router":
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(fixture.native_response())
                    return
                self.end_headers()
                encoded = "invalid" if fixture.bad_image else base64.b64encode(fixture.image).decode()
                self.wfile.write(json.dumps({"data": [{"b64_json": encoded}]}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), API)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (self.config / "opencode.json").write_text(json.dumps({"provider": {"openrouter": {
            "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/api/v1"}}}}))
        self.agents = Agents(self.home)
        self.before = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.home)): hashlib.sha256(path.read_bytes()).hexdigest()
                for root in (self.config, self.auth.parent) for path in root.rglob("*") if path.is_file()}

    def requirements(self):
        return self.agents.launch_requirements("opencode", OPENCODE)

    def generate(self, selected="openrouter/test/image"):
        selection = {"harness": "opencode", "model": selected, "thinking": ""}
        reference, prompt = opencode_images.prepare(dict(selection, style="Winter night"), self.work,
                                                    self.work / "reference.png", "{}")
        (self.work / "prompt.txt").write_text(prompt)
        requirements = self.requirements()
        opencode_images.configure(requirements, self.work, selection)
        argv = self.agents.command(selection, self.work, reference)
        argv, env = agent_sandbox(argv, self.home, self.work,
                                  [reference, self.work / "image-job.json"], requirements)
        return run(argv, env=env, cwd=self.work, stdin=prompt, timeout=40, check=False)

    def test_catalog_and_generation_leave_config_and_auth_unchanged(self):
        code, output, status = opencode_images.catalog(OPENCODE, self.home, self.requirements())
        self.assertEqual(code, 0)
        self.assertTrue(status["connected"])
        self.assertEqual(status["providers"], ["openrouter"])
        added = [item["id"] for item in json_objects(output)
                 if item.get("api", {}).get("npm") == opencode_images.PROVIDER.as_uri()]
        self.assertEqual(added, ["test/image"])
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.work / "wallpaper.png").exists(), result.stdout + result.stderr)
        self.assertEqual((self.work / "wallpaper.png").read_bytes(), self.image)
        posts = [request for request in self.requests if request[0] == "POST"]
        self.assertEqual(len(posts), 1)
        _, path, body, auth = posts[0]
        self.assertEqual(path, "/api/v1/images")
        self.assertEqual(body["model"], "test/image")
        self.assertIn("Winter night", body["prompt"])
        self.assertEqual(auth, "Bearer test-key-not-a-credential")
        self.assertEqual(body["n"], 1)
        self.assertTrue(body["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("test-key-not-a-credential", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), self.before)

    def test_rate_limit_stops_without_retry_or_output(self):
        self.error = 429
        result = self.generate()
        self.assertEqual(json.loads((self.work / "failure.json").read_text()), {"error_code": "rate_limit"})
        self.assertFalse((self.work / "wallpaper.png").exists())
        self.assertEqual(len([r for r in self.requests if r[0] == "POST"]), 1)
        self.assertNotIn("test-key-not-a-credential", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), self.before)

    def test_invalid_image_is_rejected(self):
        self.bad_image = True
        self.generate()
        self.assertEqual(json.loads((self.work / "failure.json").read_text()), {"error_code": "failed"})
        self.assertFalse((self.work / "wallpaper.png").exists())

    def test_backend_imports_the_generated_image(self):
        service = Styles(home=self.home, data=self.home / "styles")
        job = {"base": "alpha", "harness": "opencode", "model": "openrouter/test/image",
               "thinking": "", "style": "Summer", "reference": "reference.png"}
        output = service.generate_image(job, self.work)
        self.assertEqual(output, self.work / "wallpaper.png")
        self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(len([r for r in self.requests if r[0] == "POST"]), 1)
        self.assertEqual(self.snapshot(), self.before)

    def test_previous_attempt_cannot_send_another_image_request(self):
        (self.work / ".image-request-started").touch()
        self.generate()
        self.assertFalse((self.work / "wallpaper.png").exists())
        self.assertEqual(len([r for r in self.requests if r[0] == "POST"]), 0)

    def test_signed_out_does_not_offer_image_models(self):
        self.auth.unlink()
        self.assertIsNone(self.agents.opencode(OPENCODE))
        self.assertEqual(self.requests, [])

    def configure_native(self, provider="opencode", sdk="@ai-sdk/google", *, output=True):
        original = json.loads((self.config / "opencode.json").read_text())
        base = original["provider"]["openrouter"]["options"]["baseURL"]
        self.auth.write_text(json.dumps({provider: {"type": "api", "key": "native-test-key"}}))
        settings = {"provider": {provider: {"options": {"baseURL": base}, "models": {
            "future-image": {"name": "Future image", "attachment": True,
                "modalities": {"input": ["text", "image"], "output": ["text", "image"] if output else ["text"]},
                "limit": {"context": 32000, "output": 4096}, "provider": {"npm": sdk}},
            "vision-only": {"attachment": True, "modalities": {"input": ["text", "image"], "output": ["text"]},
                            "limit": {"context": 32000, "output": 4096}, "provider": {"npm": sdk}},
        }}}}
        (self.config / "opencode.json").write_text(json.dumps(settings))
        self.protocol = {"@ai-sdk/google": "google", "@ai-sdk/openai-compatible": "chat", "@ai-sdk/openai": "responses"}[sdk]
        self.before = self.snapshot()
        return provider + "/future-image"

    def native_response(self):
        encoded = "invalid" if self.bad_image else base64.b64encode(self.image).decode()
        if self.protocol == "google":
            events = [{"candidates": [{"index": 0, "content": {"role": "model", "parts": [
                {"inlineData": {"mimeType": "image/png", "data": encoded}}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}]
        elif self.protocol == "chat":
            events = [{"id": "chat-test", "object": "chat.completion.chunk", "created": 1, "model": "future-image",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Image generated.",
                    "images": [{"image_url": {"url": "data:image/png;base64," + encoded}}]}, "finish_reason": "stop"}]}]
        else:
            response = {"id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
                "model": "future-image", "output": [{"id": "ig_test", "type": "image_generation_call",
                                                      "status": "completed", "result": encoded}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            item = response["output"][0]
            events = [
                {"type": "response.output_item.added", "sequence_number": 0, "output_index": 0,
                 "item": dict(item, status="in_progress", result=None)},
                {"type": "response.output_item.done", "sequence_number": 1, "output_index": 0, "item": item},
                {"type": "response.completed", "sequence_number": 2, "response": response}]
        if self.text_only:
            events = [{"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": "No image"}]},
                                       "finishReason": "STOP"}]}]
        if self.truncated:
            events = [{"candidates": [{"index": 0, "content": {"role": "model", "parts": [
                {"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]}]
        return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()

    def test_zen_google_native_images_use_existing_sdk_and_login(self):
        selected = self.configure_native()
        entry = self.agents.opencode(OPENCODE)
        self.assertEqual([m["value"] for m in entry["models"]], [selected])
        result = self.generate(selected)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.work / "wallpaper.png").read_bytes(), self.image)
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn(":streamGenerateContent", posts[0][1])
        self.assertEqual(posts[0][3], "native-test-key")
        self.assertEqual(posts[0][2]["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])
        self.assertIn("Winter night", json.dumps(posts[0][2]["contents"]))
        self.assertIn("inlineData", json.dumps(posts[0][2]["contents"]))
        self.assertEqual(self.snapshot(), self.before)
        self.assertNotIn("native-test-key", result.stdout)

    def test_go_chat_images_use_native_auth_and_endpoint(self):
        selected = self.configure_native("opencode-go", "@ai-sdk/openai-compatible")
        entry = self.agents.opencode(OPENCODE)
        self.assertEqual([m["value"] for m in entry["models"]], [selected])
        result = self.generate(selected)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.work / "wallpaper.png").read_bytes(), self.image)
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/api/v1/chat/completions")
        self.assertEqual(posts[0][3], "Bearer native-test-key")
        self.assertEqual(posts[0][2]["modalities"], ["text", "image"])
        self.assertTrue(self.request_headers[0].get("x-opencode-session"))
        self.assertIn("opencode", self.request_headers[0]["user-agent"].lower())
        self.assertIn("data:image/png;base64,", json.dumps(posts[0][2]["messages"]))
        self.assertEqual(self.snapshot(), self.before)

    def test_zen_responses_images_are_saved(self):
        selected = self.configure_native("opencode", "@ai-sdk/openai")
        result = self.generate(selected)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.work / "wallpaper.png").read_bytes(), self.image)
        self.assertEqual([r[1] for r in self.requests if r[0] == "POST"], ["/api/v1/responses"])

    def test_native_authentication_failure_does_not_retry(self):
        selected = self.configure_native()
        self.error = 401
        result = self.generate(selected)
        self.assertEqual(json.loads((self.work / "failure.json").read_text()), {"error_code": "authentication"})
        self.assertEqual(len([r for r in self.requests if r[0] == "POST"]), 1)
        self.assertFalse((self.work / "wallpaper.png").exists())
        self.assertNotIn("native-test-key", result.stdout)
        self.assertEqual(self.snapshot(), self.before)

    def test_native_text_without_image_fails(self):
        selected = self.configure_native()
        self.text_only = True
        self.generate(selected)
        self.assertEqual(json.loads((self.work / "failure.json").read_text()), {"error_code": "failed"})
        self.assertFalse((self.work / "wallpaper.png").exists())

    def test_native_truncated_image_stream_fails(self):
        selected = self.configure_native()
        self.truncated = True
        self.generate(selected)
        self.assertFalse((self.work / "wallpaper.png").exists())
        self.assertTrue((self.work / "failure.json").exists())

    def test_native_vision_models_are_hidden_and_rechecked_before_generation(self):
        selected = self.configure_native(output=False)
        self.assertEqual(self.agents.opencode(OPENCODE)["models"], [])
        self.generate(selected)
        self.assertEqual(json.loads((self.work / "failure.json").read_text()), {"error_code": "unsupported"})
        self.assertEqual(self.requests, [])

    def test_configured_native_provider_without_credentials_is_hidden(self):
        self.configure_native()
        self.auth.unlink()
        self.assertIsNone(self.agents.opencode(OPENCODE))
        self.assertEqual(self.requests, [])

    def test_broken_openrouter_catalog_does_not_hide_zen(self):
        selected = self.configure_native()
        auth = json.loads(self.auth.read_text())
        auth["openrouter"] = {"type": "api", "key": "router-test-key"}
        self.auth.write_text(json.dumps(auth))
        settings = json.loads((self.config / "opencode.json").read_text())
        settings["provider"]["openrouter"] = {"options": settings["provider"]["opencode"]["options"]}
        (self.config / "opencode.json").write_text(json.dumps(settings))
        self.catalog_error = 503
        entry = self.agents.opencode(OPENCODE)
        self.assertEqual([m["value"] for m in entry["models"]], [selected])
        self.assertIn("Could not load OpenRouter", entry["notice"])

    def test_zen_environment_key_is_available_to_discovery_and_generation(self):
        selected = self.configure_native()
        self.auth.unlink()
        with patch.dict(os.environ, {"OPENCODE_API_KEY": "native-test-key"}):
            self.assertEqual([m["value"] for m in self.agents.opencode(OPENCODE)["models"]], [selected])
            self.assertEqual(self.generate(selected).returncode, 0)
        self.assertTrue((self.work / "wallpaper.png").exists())

    def test_new_native_model_in_opencode_cache_is_discovered_without_config_override(self):
        selected = self.configure_native()
        settings = json.loads((self.config / "opencode.json").read_text())
        future = settings["provider"]["opencode"].pop("models")["future-image"]
        future.update(id="future-image", release_date="2026-09-19", reasoning=False,
                      temperature=False, tool_call=False, cost={"input": 1, "output": 1})
        catalog = {"opencode": {"id": "opencode", "name": "OpenCode Zen", "env": ["OPENCODE_API_KEY"],
                   "api": "https://opencode.ai/zen/v1", "npm": "@ai-sdk/openai-compatible",
                   "models": {"future-image": future}}}
        cache = self.home / "custom-cache/opencode/models.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(catalog))
        before = cache.read_bytes()
        (self.config / "opencode.json").write_text(json.dumps(settings))
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache.parent.parent)}):
            entry = self.agents.opencode(OPENCODE)
            self.assertEqual([m["value"] for m in entry["models"]], [selected])
            result = self.generate(selected)
            self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(cache.read_bytes(), before)
        self.assertTrue((self.work / "wallpaper.png").exists())
