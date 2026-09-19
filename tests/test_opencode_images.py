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
        self.error = 0
        self.bad_image = False
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
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": fixture.models}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(("POST", self.path, body, self.headers.get("Authorization")))
                self.send_response(fixture.error or 200)
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

    def generate(self):
        selection = {"harness": "opencode", "model": "openrouter/test/image", "thinking": ""}
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
        self.assertEqual(status, {"connected": True})
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
