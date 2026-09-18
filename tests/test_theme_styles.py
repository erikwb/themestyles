"""Behavior tests use an isolated home; no desktop or paid generation is touched."""
import fcntl
import os
import shutil
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from unittest.mock import patch

from support import CATALOG, COLORS, StyleFixture, png

from agents import DEFAULT_MODEL, HARNESS_NAMES
from desktop import OmarchyDesktop
from theme_styles import (
    MARKER,
    GenerationError,
    StylesError,
    read_json,
    write_json,
)


class StylesTests(StyleFixture, unittest.TestCase):
    def test_saved_styles_are_visible_only_for_current_parent(self):
        job = self.request()
        self.complete(job)
        self.assertEqual([v["name"] for v in self.service.status()["styles"]], ["Winter"])
        self.select("beta")
        self.assertEqual(self.service.status()["styles"], [])
        self.select("alpha")
        self.assertEqual(self.service.status()["styles"][0]["id"], job["id"])

    def test_agent_preferences_are_scoped_to_the_current_theme(self):
        self.service.configure("alpha", "grok", "grok-image", "low")
        self.assertEqual(self.service.agent_options()["selection"]["harness"], "grok")
        self.select("beta")
        self.assertEqual(self.service.agent_options()["selection"]["harness"], "codex")
        self.select("alpha")
        self.assertEqual(self.service.agent_options()["selection"]["thinking"], "low")

    def test_generation_freezes_agent_selection_and_retains_it_with_style(self):
        self.service.configure("alpha", "grok", "grok-image", "low")
        job = self.request()
        self.service.configure("alpha", "codex", "image-agent", "high")
        record = self.complete(job)
        self.assertEqual((record["harness"], record["model"], record["thinking"]),
                         ("grok", "grok-image", "low"))
        request = read_json(self.service.root("alpha") / "jobs" / job["id"] / "request.json")
        self.assertEqual(request["harness"], "grok")

    def test_invalid_agent_selection_cannot_create_a_job(self):
        for fields in [("claude", "image-agent", "low"), ("codex", "missing", "low"),
                       ("codex", "image-agent", "ultra")]:
            with self.assertRaisesRegex(StylesError, "no longer available"):
                self.service.start("alpha", "winter", spawn=False,
                                   harness=fields[0], model=fields[1], thinking=fields[2])
        self.assertFalse((self.service.root("alpha") / "job.json").exists())

    def test_signed_out_harness_cannot_start_generation(self):
        with patch.object(self.service.agents, "catalog", return_value=[]):
            with self.assertRaisesRegex(StylesError, "No signed-in"):
                self.request()

    def test_empty_filtered_catalog_cannot_start_or_create_a_job(self):
        catalog = [{"value": "opencode", "label": "OpenCode", "models": [], "model": "", "thinking": "",
                    "notice": "No models with image input and output are available."}]
        with patch.object(self.service.agents, "catalog", return_value=catalog):
            for selection in ({}, {"harness": "opencode", "model": "", "thinking": ""}):
                with self.subTest(selection=selection), self.assertRaisesRegex(StylesError, "No models with image"):
                    self.service.start("alpha", "winter", spawn=False, **selection)
        self.assertFalse((self.service.root("alpha") / "job.json").exists())
        self.assertFalse((self.service.root("alpha") / "jobs").exists())

    def test_signed_out_saved_harness_does_not_fall_back_to_another_account(self):
        self.service.configure("alpha", "grok", "grok-image", "low")
        with patch.object(self.service.agents, "catalog", return_value=CATALOG[:1]):
            with self.assertRaisesRegex(StylesError, "saved image-generation account"):
                self.request()
        self.assertFalse((self.service.root("alpha") / "job.json").exists())

    def test_stale_theme_cannot_change_agent_preferences(self):
        token = self.service.context()["token"]
        self.select("beta")
        with self.assertRaises(StylesError):
            self.service.configure("alpha", "grok", "grok-image", "low", token)

    def test_same_name_can_exist_in_different_themes(self):
        self.complete(self.request())
        self.select("beta")
        job = self.service.start("beta", "Snow", "Winter", spawn=False)["job"]
        self.complete(job)
        self.assertEqual(len(self.service.variants("alpha")), 1)
        self.assertEqual(len(self.service.variants("beta")), 1)

    def test_duplicate_name_cannot_replace_saved_image(self):
        job = self.request()
        self.complete(job)
        self.service.update_job("alpha", state="done")
        with self.assertRaisesRegex(StylesError, "already saved"):
            self.request("winter")
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_new_name_retains_both_versions(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        second = self.request("Winter 2")
        self.complete(second)
        self.assertEqual({v["name"] for v in self.service.variants("alpha")}, {"Winter", "Winter 2"})

    def test_long_description_gets_a_short_default_name(self):
        job = self.service.start("alpha", "Winter with snow and twilight " * 8, spawn=False)["job"]
        self.assertLessEqual(len(job["name"]), 80)
        self.assertGreater(len(job["style"]), 80)

    def test_automatic_names_keep_repeated_descriptions_as_separate_styles(self):
        jobs = []
        for description in ("Winter", "winter", "Winter"):
            job = self.service.start("alpha", description, spawn=False)["job"]
            self.complete(job)
            self.service.update_job("alpha", state="done")
            jobs.append(job)
        self.assertEqual([j["name"] for j in jobs], ["Winter", "winter 2", "Winter 3"])
        self.assertEqual(len(self.service.variants("alpha")), 3)

    def test_numbered_automatic_names_fit_long_descriptions(self):
        description = "Moonlit rainy forest " * 10
        first = self.service.start("alpha", description, spawn=False)["job"]
        self.complete(first)
        self.service.update_job("alpha", state="done")
        second = self.service.start("alpha", description, spawn=False)["job"]
        self.assertLessEqual(len(second["name"]), 80)
        self.assertTrue(second["name"].endswith(" 2"))
        self.assertEqual(second["style"], description.strip())

    def test_automatic_name_normalizes_multiline_descriptions(self):
        job = self.service.start("alpha", "Winter\nwith\tsnow", spawn=False)["job"]
        self.assertEqual(job["name"], "Winter with snow")
        self.assertEqual(job["style"], "Winter\nwith\tsnow")

    def test_applying_style_retains_parent_and_original_theme(self):
        original = (self.service.desktop.themes / "alpha/colors.toml").read_bytes()
        job = self.request()
        self.complete(job)
        self.service.apply("alpha", job["id"])
        state = self.service.status()
        self.assertEqual(state["base"], "alpha")
        self.assertEqual(state["name"], "alpha")
        self.assertEqual(state["active"], job["id"])
        self.assertEqual(len(state["styles"]), 1)
        self.assertEqual((self.service.desktop.themes / "alpha/colors.toml").read_bytes(), original)
        self.assertEqual({p.name for p in self.service.desktop.themes.iterdir()}, {"alpha", "beta"})
        self.service.restore("alpha")
        self.assertEqual(self.service.status()["active"], "")

    def test_regeneration_uses_original_not_previous_output(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        root = self.service.root("alpha")
        self.service.apply("alpha", first["id"])
        second = self.request("Winter 2")
        reference = root / "jobs" / second["id"] / second["reference"]
        original = root / "original/wallpaper.png"
        self.assertEqual(reference.read_bytes(), original.read_bytes())
        self.assertNotEqual(reference.read_bytes(), (self.service.desktop.current / "background").read_bytes())

    def test_first_generation_uses_selected_wallpaper_not_first_in_theme(self):
        default = (self.service.desktop.current / "background").read_bytes()
        selected = self.service.desktop.current / "theme/backgrounds/second.png"
        png(selected, (100, 10, 30))
        self.select_wallpaper(selected)
        job = self.request()
        reference = self.service.root("alpha") / "jobs" / job["id"] / job["reference"]
        self.assertEqual(reference.read_bytes(), selected.read_bytes())
        self.assertNotEqual(reference.read_bytes(), default)

    def test_switching_wallpaper_after_first_generation_changes_next_reference(self):
        self.request()
        self.service.update_job("alpha", state="done")
        root = self.service.root("alpha")
        original = (root / "original/wallpaper.png").read_bytes()
        selected = self.service.desktop.current / "theme/backgrounds/second.png"
        png(selected, (100, 10, 30))
        self.select_wallpaper(selected)
        job = self.request("Summer")
        reference = root / "jobs" / job["id"] / job["reference"]
        self.assertEqual(reference.read_bytes(), selected.read_bytes())
        self.assertNotEqual(reference.read_bytes(), original)
        self.assertEqual((root / "original/wallpaper.png").read_bytes(), original)

    def test_saved_style_keeps_its_own_source_after_job_cleanup(self):
        self.request()
        self.service.update_job("alpha", state="done")
        selected = self.service.desktop.current / "theme/backgrounds/second.png"
        png(selected, (100, 10, 30))
        source = selected.read_bytes()
        self.select_wallpaper(selected)
        second = self.request("Summer")
        self.complete(second)
        self.service.update_job("alpha", state="done")
        self.service.apply("alpha", second["id"])
        root = self.service.root("alpha")
        shutil.rmtree(root / "jobs" / second["id"])
        third = self.request("Summer dusk")
        reference = root / "jobs" / third["id"] / third["reference"]
        self.assertEqual(reference.read_bytes(), source)
        self.assertNotEqual(reference.read_bytes(), (root / "original/wallpaper.png").read_bytes())
        self.assertNotEqual(reference.read_bytes(), (self.service.desktop.current / "background").read_bytes())

    def test_native_wallpaper_change_over_active_style_takes_precedence(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        self.service.apply("alpha", first["id"])
        selected = self.home / "other wallpaper.png"
        png(selected, (100, 10, 30))
        self.select_wallpaper(selected)
        second = self.request("Summer")
        reference = self.service.root("alpha") / "jobs" / second["id"] / second["reference"]
        self.assertEqual(reference.read_bytes(), selected.read_bytes())

    def test_legacy_style_can_use_its_job_reference(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        root = self.service.root("alpha")
        variant = root / "variants" / first["id"]
        record = read_json(variant / "style.json")
        (variant / record.pop("reference")).unlink()
        write_json(variant / "style.json", record)
        source = root / "jobs" / first["id"] / first["reference"]
        png(source, (100, 10, 30))
        self.service.apply("alpha", first["id"])
        second = self.request("Summer")
        self.assertEqual((root / "jobs" / second["id"] / second["reference"]).read_bytes(), source.read_bytes())

    def test_missing_saved_reference_does_not_silently_use_another_wallpaper(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        self.service.apply("alpha", first["id"])
        root = self.service.root("alpha")
        (root / "variants" / first["id"] / first["reference"]).unlink()
        with self.assertRaisesRegex(StylesError, "source wallpaper is unavailable"):
            self.request("Summer")
        self.assertEqual([p.name for p in (root / "jobs").iterdir()], [first["id"]])

    def test_switching_wallpaper_during_generation_keeps_reference_and_skips_apply(self):
        job = self.request(auto_apply=True)
        root = self.service.root("alpha")
        reference = root / "jobs" / job["id"] / job["reference"]
        frozen = reference.read_bytes()
        selected = self.service.desktop.current / "theme/backgrounds/second.png"
        png(selected, (100, 10, 30))
        self.select_wallpaper(selected)
        self.fake_pipeline()
        self.service.worker("alpha", job["id"])
        self.assertEqual(reference.read_bytes(), frozen)
        self.assertEqual(self.service.context()["wallpaper"], str(selected))
        self.assertEqual(self.service.context()["active"], "")
        self.assertEqual(self.service.job("alpha")["state"], "done")
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_wallpaper_change_while_copying_reference_rejects_request(self):
        self.request()
        self.service.update_job("alpha", state="done")
        selected = self.service.desktop.current / "theme/backgrounds/second.png"
        png(selected, (100, 10, 30))
        original_copy = shutil.copyfile
        def copy_and_switch(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if Path(destination).name.startswith("reference."):
                self.select_wallpaper(selected)
            return result
        with patch("theme_styles.shutil.copyfile", side_effect=copy_and_switch):
            with self.assertRaisesRegex(StylesError, "selected theme changed"):
                self.request("Summer")
        self.assertEqual(self.service.job("alpha")["state"], "done")
        self.assertEqual(len(list((self.service.root("alpha") / "jobs").iterdir())), 1)

    def test_cross_theme_apply_is_rejected_before_mutation(self):
        job = self.request()
        self.complete(job)
        self.select("beta")
        with self.assertRaisesRegex(StylesError, "selected theme changed"):
            self.service.apply("alpha", job["id"])
        self.assertFalse((self.service.desktop.themes / self.service.slot("alpha")).exists())
        with self.assertRaisesRegex(StylesError, "does not belong"):
            self.service.apply("beta", job["id"])
        self.assertEqual(self.service.context()["name"], "beta")

    def test_stale_selection_rejects_apply_and_restore(self):
        job = self.request()
        self.complete(job)
        token = self.service.context()["token"]
        self.select("beta")
        self.select("alpha")
        with self.assertRaises(StylesError):
            self.service.apply("alpha", job["id"], token)
        with self.assertRaises(StylesError):
            self.service.restore("alpha", token)

    def test_generation_finishing_on_other_theme_only_saves(self):
        job = self.request(auto_apply=True)
        self.fake_pipeline()
        self.select("beta")
        self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.context()["name"], "beta")
        self.assertEqual(len(self.service.variants("alpha")), 1)
        self.assertEqual(self.service.job("alpha")["state"], "done")
        self.assertIn("Saved", self.service.job("alpha")["message"])

    def test_generation_finishing_after_switch_away_and_back_only_saves(self):
        job = self.request(auto_apply=True)
        self.fake_pipeline()
        self.select("beta")
        self.select("alpha")
        self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.context()["name"], "alpha")
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_generation_applies_if_selection_is_unchanged(self):
        job = self.request(auto_apply=True)
        self.fake_pipeline()
        self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.context()["active"], job["id"])
        self.assertIn("Applied", self.service.job("alpha")["message"])

    def test_worker_reports_actual_generation_stages(self):
        job = self.request(auto_apply=True)
        self.fake_pipeline()
        with patch.object(self.service, "update_job", wraps=self.service.update_job) as update:
            self.service.worker("alpha", job["id"])
        stages = [call.kwargs["state"] for call in update.call_args_list if "state" in call.kwargs]
        self.assertEqual(stages, ["generating", "theming", "saving", "applying", "done"])

    def test_worker_does_not_report_applying_if_theme_changed(self):
        job = self.request(auto_apply=True)
        self.fake_pipeline()
        self.select("beta")
        with patch.object(self.service, "update_job", wraps=self.service.update_job) as update:
            self.service.worker("alpha", job["id"])
        stages = [call.kwargs["state"] for call in update.call_args_list if "state" in call.kwargs]
        self.assertEqual(stages, ["generating", "theming", "saving", "done"])

    def test_failed_generation_retains_original_and_no_partial_style(self):
        job = self.request(auto_apply=True)
        with patch.object(self.service, "generate_image", side_effect=StylesError("No image access")):
            self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["state"], "failed")
        self.assertEqual(self.service.variants("alpha"), [])
        self.assertEqual(self.service.context()["name"], "alpha")

    def test_successful_agent_exit_without_an_image_is_an_error(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        with patch.object(self.service, "run_process"):
            with self.assertRaisesRegex(StylesError, "did not save a wallpaper"):
                self.service.generate_image(job, workspace)

    def test_log_path_is_available_even_when_error_message_omits_it(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        log = workspace / "agent.log"
        log.write_text("agent output")
        self.service.update_job("alpha", state="failed", message="No usable image")
        self.assertEqual(self.service.job("alpha")["log_path"], str(log))
        newer = workspace / "aether.log"
        newer.write_text("palette generation failed")
        os.utime(newer, ns=(log.stat().st_mtime_ns + 1000000,) * 2)
        self.assertEqual(self.service.job("alpha")["log_path"], str(newer))
        newer.unlink()
        log.unlink()
        self.assertEqual(self.service.job("alpha")["log_path"], "")

    def test_reported_tool_failure_is_not_mistaken_for_success(self):
        job = self.request(auto_apply=True)
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        png(workspace / "agent/wallpaper.png")
        write_json(workspace / "agent/failure.json", {"error_code": "unsupported", "message": "untrusted secret"})
        before = self.service.context()
        with patch.object(self.service, "run_process"), patch.object(self.service, "notify_failure") as notify:
            self.service.worker("alpha", job["id"])
        result = self.service.job("alpha")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "unsupported")
        self.assertIn("image generation is unavailable", result["message"])
        self.assertNotIn("untrusted secret", result["message"])
        notify.assert_called_once_with(result["message"])
        self.assertEqual(self.service.context(), before)
        self.assertEqual(self.service.variants("alpha"), [])
        self.assertEqual(self.service.agents.catalog(), CATALOG)

    def test_real_child_failure_keeps_applied_style_and_notifies_once(self):
        first = self.request()
        self.complete(first)
        self.service.update_job("alpha", state="done")
        self.service.apply("alpha", first["id"])
        before = self.service.context()
        job = self.request("Summer", auto_apply=True)
        agent = self.home / "fake-agent"
        agent.write_text('''#!/usr/bin/env python3
from pathlib import Path
Path("failure.json").write_text('{"error_code": "unsupported"}')
print("No image tool is configured")
''')
        agent.chmod(0o755)
        with patch("agents.safe_binary", return_value=str(agent)), \
             patch.object(self.service, "notify_failure") as notify:
            self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["error_code"], "unsupported")
        self.assertEqual(self.service.context(), before)
        self.assertEqual([s["id"] for s in self.service.variants("alpha")], [first["id"]])
        notify.assert_called_once()

    def test_failed_process_reports_quota_without_claiming_unsupported(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        write_json(workspace / "agent/response.json", {"image_path": "", "error_code": "rate_limit"})
        with patch.object(self.service, "run_process", side_effect=StylesError("agent failed")):
            with self.assertRaises(GenerationError) as error:
                self.service.generate_image(job, workspace)
        self.assertEqual(error.exception.code, "rate_limit")
        self.assertIn("quota", str(error.exception))
        self.assertNotIn("unsupported", str(error.exception))

    def test_unknown_failure_preserves_log_and_does_not_claim_unsupported(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        (workspace / "agent/failure.json").write_text("malformed")
        with patch.object(self.service, "run_process", side_effect=StylesError("agent failed")):
            with self.assertRaises(GenerationError) as error:
                self.service.generate_image(job, workspace)
        self.assertEqual(error.exception.code, "generation_failed")
        self.assertIn(str(workspace / "agent.log"), str(error.exception))

    def test_every_adapter_receives_prompt_and_missing_output_fails(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        for harness in HARNESS_NAMES:
            with self.subTest(harness=harness), patch.object(self.service, "run_process") as process:
                attempt = job | {"harness": harness, "model": DEFAULT_MODEL, "thinking": ""}
                with self.assertRaises(GenerationError):
                    self.service.generate_image(attempt, workspace)
                args, kwargs = process.call_args
                if self.service.agents.uses_stdin(harness):
                    self.assertIn("Create exactly one wallpaper", kwargs["stdin"])
                else:
                    self.assertTrue(any("prompt.txt" in a or "Create exactly one wallpaper" in a for a in args[0]))

    def test_invalid_image_is_a_failure(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        (workspace / "agent/wallpaper.png").write_text("This is not an image")
        with patch.object(self.service, "run_process"):
            with self.assertRaises(GenerationError) as error:
                self.service.generate_image(job, workspace)
        self.assertEqual(error.exception.code, "invalid_image")

    def test_valid_jpeg_named_png_is_normalized(self):
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        target = workspace / "agent/wallpaper.png"
        png(target)
        subprocess.run(["magick", str(target), "JPEG:" + str(target)], check=True)
        run_process = self.service.run_process
        def fake_agent(argv, *args, **kwargs):
            if argv[0] == "magick":
                return run_process(argv, *args, **kwargs)
        with patch.object(self.service, "run_process", side_effect=fake_agent):
            output = self.service.generate_image(job, workspace)
        self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_notification_escapes_markup_and_does_not_use_shell(self):
        self.service.desktop.headless = lambda: False
        with patch("theme_styles.subprocess.run") as run:
            self.service.notify_failure("<b>Image failed</b> $(command)")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "&lt;b&gt;Image failed&lt;/b&gt; $(command)")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_cancel_does_not_pop_up_failure(self):
        job = self.request()
        self.service.cancel("alpha")
        with patch.object(self.service, "notify_failure") as notify:
            self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["state"], "cancelled")
        notify.assert_not_called()

    def test_second_generation_cannot_replace_running_job(self):
        first = self.request()
        with self.assertRaisesRegex(StylesError, "already being generated"):
            self.request("Winter 2")
        self.assertEqual(self.service.job("alpha")["id"], first["id"])

    def test_cancellation_stops_child_and_saves_nothing(self):
        job = self.request()
        self.fake_pipeline()
        self.service.cancel("alpha")
        self.assertTrue(self.service.job("alpha")["cancel_requested"])
        self.assertEqual(self.service.job("alpha")["message"], "Cancelling generation…")
        self.service.worker("alpha", job["id"])
        self.assertEqual(self.service.job("alpha")["state"], "cancelled")
        self.assertEqual(self.service.variants("alpha"), [])
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        with self.assertRaisesRegex(StylesError, "cancelled"):
            self.service.run_process(["sleep", "30"], workspace, "child.log", timeout=40)

    def test_cancellation_does_not_interrupt_application(self):
        job = self.request()
        self.service.update_job("alpha", state="applying")
        with self.assertRaisesRegex(StylesError, "already being applied"):
            self.service.cancel("alpha")
        self.assertFalse((self.service.root("alpha") / "jobs" / job["id"] / "cancel").exists())

    def test_interrupted_job_no_longer_blocks_generation(self):
        self.request()
        self.service.update_job("alpha", started=1, pid=999999999, process_start="missing")
        self.assertEqual(self.service.job("alpha")["state"], "failed")
        self.assertEqual(self.request()["state"], "starting")

    def test_existing_unmanaged_companion_is_ignored(self):
        job = self.request()
        self.complete(job)
        destination = self.service.desktop.themes / self.service.slot("alpha")
        destination.mkdir()
        (destination / "precious").write_text("untouched")
        self.service.apply("alpha", job["id"])
        self.service.migrate()
        self.assertEqual((destination / "precious").read_text(), "untouched")

    def test_failed_template_render_keeps_previous_style_and_background(self):
        first = self.request()
        self.complete(first)
        self.service.apply("alpha", first["id"])
        self.service.update_job("alpha", state="done")
        second = self.request("Winter 2")
        self.complete(second)
        background = (self.service.desktop.current / "background").readlink()
        with patch.object(self.service.desktop, "render_templates", side_effect=StylesError("apply failed")):
            with self.assertRaisesRegex(StylesError, "apply failed"):
                self.service.apply("alpha", second["id"])
        marker = read_json(self.service.desktop.current / "theme" / MARKER)
        self.assertEqual(marker["style_id"], first["id"])
        self.assertEqual((self.service.desktop.current / "background").readlink(), background)
        self.assertFalse((self.service.desktop.current / "next-theme").exists())

    def test_partial_activation_failure_recovers_runtime(self):
        job = self.request()
        self.complete(job)
        previous = self.service.context()
        def fail_after_switch(base, wallpaper):
            OmarchyDesktop.update_selection(self.service.desktop, base, wallpaper)
            raise StylesError("refresh failed")
        self.service.desktop.update_selection = fail_after_switch
        with self.assertRaisesRegex(StylesError, "refresh failed"):
            self.service.apply("alpha", job["id"])
        self.assertEqual(self.service.context()["name"], "alpha")
        self.assertEqual(self.service.context()["active"], "")
        self.assertEqual(self.service.context()["wallpaper"], previous["wallpaper"])
        self.assertEqual((self.service.desktop.current / "theme/colors.toml").read_text(), COLORS)
        self.assertFalse((self.service.desktop.themes / self.service.slot("alpha")).exists())

    def test_reselecting_original_clears_style_but_retains_saved_versions(self):
        job = self.request()
        self.complete(job)
        self.service.apply("alpha", job["id"])
        self.select("beta")
        self.assertEqual(self.service.status()["styles"], [])
        self.select("alpha")
        state = self.service.status()
        self.assertEqual(state["name"], "alpha")
        self.assertEqual(state["active"], "")
        self.assertEqual(state["styles"][0]["id"], job["id"])
        self.assertEqual((self.service.desktop.current / "theme/colors.toml").read_text(), COLORS)

    def test_migration_archives_legacy_theme_and_preserves_active_style(self):
        job = self.request()
        self.complete(job)
        legacy = self.service.desktop.themes / self.service.slot("alpha")
        shutil.copytree(self.service.root("alpha") / "variants" / job["id"] / "theme", legacy)
        self.select(legacy.name)
        before = (self.service.desktop.current / "background").read_bytes()
        result = self.service.migrate()
        self.assertEqual(result["archived"], [legacy.name])
        self.assertFalse(legacy.exists())
        self.assertEqual(self.service.context()["name"], "alpha")
        self.assertEqual(self.service.context()["active"], job["id"])
        self.assertEqual((self.service.desktop.current / "background").read_bytes(), before)
        self.assertEqual(len(self.service.variants("alpha")), 1)
        archives = list((self.service.root("alpha") / "legacy-themes").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / MARKER).is_file())
        self.assertEqual(self.service.migrate()["archived"], [])

    def test_migration_of_inactive_legacy_theme_does_not_change_selection(self):
        job = self.request()
        self.complete(job)
        legacy = self.service.desktop.themes / self.service.slot("alpha")
        shutil.copytree(self.service.root("alpha") / "variants" / job["id"] / "theme", legacy)
        self.select("beta")
        before = self.service.context()
        self.service.migrate()
        self.assertEqual(self.service.context(), before)
        self.assertFalse(legacy.exists())

    def test_existing_native_staging_is_not_overwritten(self):
        job = self.request()
        self.complete(job)
        staging = self.service.desktop.current / "next-theme"
        staging.mkdir()
        (staging / "precious").write_text("pending")
        with self.assertRaisesRegex(StylesError, "unfinished theme change"):
            self.service.apply("alpha", job["id"])
        self.assertEqual((staging / "precious").read_text(), "pending")

    def saved_style(self, name="Winter"):
        job = self.request(name)
        self.complete(job)
        self.service.update_job("alpha", state="done")
        return job

    def test_deletion_requires_confirmation(self):
        job = self.saved_style()
        with self.assertRaisesRegex(StylesError, "Confirm deletion"):
            self.service.delete("alpha", job["id"])
        self.assertEqual(len(self.service.variants("alpha")), 1)
        self.assertEqual(self.calls, [])

    def test_deletion_removes_only_selected_style_and_its_generation_files(self):
        first = self.saved_style()
        second = self.saved_style("Summer")
        original = (self.service.desktop.themes / "alpha/colors.toml").read_bytes()
        root = self.service.root("alpha")
        self.service.delete("alpha", first["id"], confirmed=True)
        self.assertEqual([v["id"] for v in self.service.variants("alpha")], [second["id"]])
        self.assertFalse((root / "jobs" / first["id"]).exists())
        self.assertTrue((root / "jobs" / second["id"]).exists())
        self.assertTrue((root / "original/wallpaper.png").is_file())
        self.assertEqual(self.service.job("alpha")["id"], second["id"])
        self.assertEqual((self.service.desktop.themes / "alpha/colors.toml").read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_deleting_active_style_restores_original_and_clears_job(self):
        job = self.saved_style()
        self.service.apply("alpha", job["id"])
        self.service.delete("alpha", job["id"], confirmed=True)
        state = self.service.status()
        self.assertEqual(state["name"], "alpha")
        self.assertEqual(state["active"], "")
        self.assertEqual(state["styles"], [])
        self.assertEqual(state["job"], {})
        self.assertTrue((self.service.desktop.current / "background").is_file())
        self.assertEqual((self.service.desktop.current / "theme/colors.toml").read_text(), COLORS)

    def test_failed_restore_prevents_active_style_deletion(self):
        job = self.saved_style()
        self.service.apply("alpha", job["id"])
        with patch.object(self.service.desktop, "activate", side_effect=StylesError("restore failed")):
            with self.assertRaisesRegex(StylesError, "restore failed"):
                self.service.delete("alpha", job["id"], confirmed=True)
        self.assertEqual(self.service.context()["active"], job["id"])
        self.assertTrue((self.service.desktop.current / "background").is_file())
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_stale_confirmation_and_cross_theme_deletion_are_rejected(self):
        job = self.saved_style()
        token = self.service.context()["token"]
        self.select("beta")
        with self.assertRaisesRegex(StylesError, "selected theme changed"):
            self.service.delete("alpha", job["id"], token, confirmed=True)
        with self.assertRaisesRegex(StylesError, "does not belong"):
            self.service.delete("beta", job["id"], confirmed=True)
        self.select("alpha")
        with self.assertRaisesRegex(StylesError, "selected theme changed"):
            self.service.delete("alpha", job["id"], token, confirmed=True)
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_inflight_style_cannot_be_deleted_before_worker_finishes(self):
        job = self.request()
        self.complete(job)
        with self.assertRaisesRegex(StylesError, "still being generated"):
            self.service.delete("alpha", job["id"], confirmed=True)
        self.assertEqual(len(self.service.variants("alpha")), 1)

    def test_deletion_rejects_paths_and_symlinked_styles(self):
        job = self.saved_style()
        with self.assertRaises(StylesError):
            self.service.delete("alpha", "../../escape", confirmed=True)
        variant = self.service.root("alpha") / "variants" / job["id"]
        moved = self.home / "saved-style"
        variant.rename(moved)
        variant.symlink_to(moved)
        with self.assertRaisesRegex(StylesError, "symbolic link"):
            self.service.delete("alpha", job["id"], confirmed=True)
        self.assertTrue((moved / "style.json").is_file())

    def test_native_templates_replace_old_colors_and_extras_survive(self):
        templates = self.service.desktop.omarchy / "default/themed"
        templates.mkdir(parents=True)
        (templates / "kitty.conf.tpl").write_text("foreground {{ foreground }}")
        job = self.request()
        self.complete(job)
        theme = self.service.root("alpha") / "variants" / job["id"] / "theme"
        self.assertFalse((theme / "kitty.conf").exists())
        self.assertEqual((theme / "extra.asset").read_text(), "keep this\n")
        self.assertIn("#55bbcc", (theme / "colors.toml").read_text())

    def test_real_aether_renders_without_changing_current_theme(self):
        if not Path("/usr/bin/aether").exists():
            self.skipTest("Aether is not installed")
        job = self.request()
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        image = workspace / "wallpaper.png"
        png(image)
        before = self.service.context()
        rendered, mode = self.service.render(job, workspace, image)
        self.assertTrue((rendered / "colors.toml").exists())
        self.assertEqual(mode, "dark")
        self.assertEqual(self.service.context(), before)

    def test_native_mode_resolution_preserves_legacy_light_themes(self):
        job = self.request()
        theme = self.service.root("alpha") / "original/theme"
        cases = [
            ('mode = "light"\nbackground = "#101010"\n', False, "light"),
            ('mode = "dark"\ntheme_type = "light"\n', True, "dark"),
            ('theme_type = "light"\nbackground = "#101010"\n', False, "light"),
            ('background = "#101010"\n', True, "light"),
            ('background = "#ffffff"\n', False, "light"),
            ('background = "#101010"\n', False, "dark"),
        ]
        for colors, marker, expected in cases:
            with self.subTest(colors=colors, marker=marker):
                (theme / "colors.toml").write_text(colors)
                (theme / "light.mode").unlink(missing_ok=True)
                if marker:
                    (theme / "light.mode").touch()
                self.assertEqual(self.service.original_mode("alpha"), expected)
        self.service.update_job("alpha", state="done")
        (theme / "colors.toml").write_text('background = "#ffffff"\n')
        job = self.request("Another style")
        self.assertEqual(job["mode"], "light")
        workspace = self.service.root("alpha") / "jobs" / job["id"]
        def render_output(argv, *_args, **_kwargs):
            output = Path(argv[argv.index("--output") + 1])
            (output / "colors.toml").write_text(COLORS)
        with patch.object(self.service, "run_process", side_effect=render_output) as run:
            _, mode = self.service.render(job, workspace, workspace / job["reference"])
        self.assertEqual(mode, "light")
        self.assertIn("--light-mode", run.call_args.args[0])

    def test_restore_and_delete_recheck_selection_inside_native_lock(self):
        job = self.saved_style()
        self.service.apply("alpha", job["id"])
        def switch_before_activate(base, token):
            self.select("beta")
            OmarchyDesktop.activate(self.service.desktop, base, token)
        for deleting in (False, True):
            with self.subTest(deleting=deleting):
                self.select("alpha")
                self.service.apply("alpha", job["id"])
                token = self.service.context()["token"]
                with patch.object(self.service.desktop, "activate", side_effect=switch_before_activate):
                    with self.assertRaisesRegex(StylesError, "selected theme changed"):
                        if deleting:
                            self.service.delete("alpha", job["id"], token, confirmed=True)
                        else:
                            self.service.restore("alpha", token)
                self.assertEqual(self.service.context()["base"], "beta")
                self.assertTrue((self.service.root("alpha") / "variants" / job["id"]).is_dir())

    def test_native_restore_holds_desktop_lock_and_rolls_back_failure(self):
        job = self.saved_style()
        self.service.apply("alpha", job["id"])
        previous = self.service.context()
        def failed_setter(argv, workspace, log, **kwargs):
            with self.service.desktop.theme_lock.open("a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(kwargs["env"]["OMARCHY_THEME_HEADLESS"], "1")
            self.assertNotEqual(Path(kwargs["env"]["XDG_RUNTIME_DIR"]), self.service.desktop.theme_lock.parent)
            self.select("alpha")
            raise StylesError("Native setter failed after changing files")
        with patch.object(self.service.desktop, "run_process", side_effect=failed_setter):
            with self.assertRaisesRegex(StylesError, "Native setter failed"):
                OmarchyDesktop.activate(self.service.desktop, "alpha", previous["token"])
        restored = self.service.context()
        self.assertEqual((restored["base"], restored["active"], restored["wallpaper"]),
                         (previous["base"], previous["active"], previous["wallpaper"]))

    def test_cancel_and_worker_status_updates_do_not_overwrite_each_other(self):
        job = self.request()
        captured = threading.Event()
        release = threading.Event()
        job_path = self.service.root("alpha") / "job.json"
        def paused_read(path, default=None):
            value = read_json(path, default)
            if Path(path) == job_path and threading.current_thread().name.startswith("worker"):
                captured.set()
                if not release.wait(3):
                    raise RuntimeError("Test worker was not released")
            return value
        with patch("storage.read_json", side_effect=paused_read), \
             ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker") as workers, \
             ThreadPoolExecutor(max_workers=1, thread_name_prefix="cancel") as cancellers:
            worker = workers.submit(self.service.update_job, "alpha", expected_id=job["id"], state="theming")
            try:
                self.assertTrue(captured.wait(2))
                cancellation = cancellers.submit(self.service.cancel, "alpha")
                try:
                    cancellation.result(timeout=0.1)
                except FutureTimeout:
                    pass
            finally:
                release.set()
            worker.result(timeout=2)
            cancellation.result(timeout=2)
        status = self.service.job("alpha")
        self.assertEqual(status["state"], "theming")
        self.assertTrue(status["cancel_requested"])

    def test_cancel_wins_before_applying_and_stale_workers_cannot_update_new_jobs(self):
        job = self.request()
        self.service.cancel("alpha")
        with self.assertRaisesRegex(StylesError, "cancelled"):
            self.service.update_job("alpha", expected_id=job["id"], state="applying")
        with self.assertRaisesRegex(StylesError, "no longer active"):
            self.service.update_job("alpha", expected_id="0" * 32, state="failed")
        self.assertEqual(self.service.job("alpha")["id"], job["id"])
        self.assertEqual(self.service.job("alpha")["state"], "starting")

    def test_spawned_worker_does_not_write_into_plugin_directory(self):
        plugin = self.home / "plugin"
        plugin.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ("theme_styles.py", "agents.py", "harnesses.py", "security.py", "policy.xml", "files.py", "errors.py", "processes.py", "storage.py", "desktop.py", "theme-styles"):
            shutil.copyfile(source / name, plugin / name)
        binaries = self.home / "bin"
        binaries.mkdir()
        agent = binaries / "codex"
        agent.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
sys.stdin.read()
Path("failure.json").write_text('{"error_code": "unsupported"}')
''')
        agent.chmod(0o755)
        self.service.store.data = self.home / "omarchy-theme-styles"
        children = []
        popen = subprocess.Popen
        def spawn(argv, **kwargs):
            child = popen(argv, **kwargs)
            if "worker" in argv:
                children.append(child)
            return child
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home),
               "XDG_RUNTIME_DIR": str(self.home / "run"), "OMARCHY_THEME_HEADLESS": "1",
               "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
               "PYTHONDONTWRITEBYTECODE": "", "PYTHONPYCACHEPREFIX": ""}
        with patch.dict(os.environ, env), patch("theme_styles.__file__", str(plugin / "theme_styles.py")), \
             patch("theme_styles.subprocess.Popen", side_effect=spawn):
            try:
                self.service.start("alpha", "Winter")
                self.assertEqual(len(children), 1)
                self.assertEqual(children[0].wait(timeout=10), 0)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
        self.assertEqual(self.service.job("alpha")["error_code"], "unsupported")
        self.assertEqual(list(plugin.rglob("*.pyc")), [])

    def test_native_restore_without_original_wallpaper_retains_active_style(self):
        job = self.saved_style()
        self.service.apply("alpha", job["id"])
        shutil.rmtree(self.service.desktop.themes / "alpha/backgrounds")
        self.service.desktop.activate = lambda base, token="": OmarchyDesktop.activate(self.service.desktop, base, token)
        with self.assertRaisesRegex(StylesError, "no usable wallpaper"):
            self.service.delete("alpha", job["id"], confirmed=True)
        self.assertEqual(self.service.context()["active"], job["id"])
        self.assertTrue((self.service.desktop.current / "background").is_file())
        self.assertTrue((self.service.root("alpha") / "variants" / job["id"]).is_dir())

    def test_native_omarchy_can_apply_and_restore_in_isolated_home(self):
        omarchy_path = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"))
        if not (omarchy_path / "bin/omarchy-theme-set").exists():
            self.skipTest("Omarchy is not installed")
        self.service.desktop.omarchy = omarchy_path
        job = self.request()
        self.complete(job)
        self.service.desktop.activate = lambda slug, token="": OmarchyDesktop.activate(self.service.desktop, slug, token)
        self.service.desktop.render_templates = lambda: OmarchyDesktop.render_templates(self.service.desktop)
        runtime = self.home / "run"
        runtime.mkdir(exist_ok=True)
        with patch.dict(os.environ, {"HOME": str(self.home), "OMARCHY_THEME_HEADLESS": "1",
                                     "OMARCHY_PATH": str(omarchy_path), "XDG_RUNTIME_DIR": str(runtime)}):
            before_list = subprocess.check_output(["omarchy", "theme", "list"])
            self.service.apply("alpha", job["id"])
            self.assertEqual(self.service.context()["name"], "alpha")
            self.assertEqual(self.service.context()["active"], job["id"])
            self.assertEqual((self.service.desktop.current / "background").read_bytes(),
                             (self.service.root("alpha") / "variants" / job["id"] / "theme/backgrounds/style.png").read_bytes())
            self.assertIn("#eeeeff", (self.service.desktop.current / "theme/kitty.conf").read_text())
            self.assertEqual(subprocess.check_output(["omarchy", "theme", "list"]), before_list)
            subprocess.run(["omarchy", "theme", "set", "beta"], check=True, capture_output=True)
            self.assertEqual(self.service.status()["styles"], [])
            subprocess.run(["omarchy", "theme", "set", "alpha"], check=True, capture_output=True)
            self.assertEqual(self.service.context()["active"], "")
            self.assertEqual((self.service.desktop.current / "theme/colors.toml").read_text(), COLORS)
            self.assertEqual(len(self.service.status()["styles"]), 1)
            self.service.apply("alpha", job["id"])
            self.service.restore("alpha")
            self.assertEqual(self.service.context()["name"], "alpha")
            self.service.update_job("alpha", state="done")
            self.service.apply("alpha", job["id"])
            self.service.delete("alpha", job["id"], confirmed=True)
            self.assertEqual(self.service.status()["styles"], [])
            self.assertEqual(self.service.context()["active"], "")
            self.assertTrue((self.service.desktop.current / "background").is_file())

    def test_pathlike_style_names_are_labels_never_paths(self):
        job = self.request("../../summer; $(touch nope)")
        self.complete(job)
        self.assertEqual(len(self.service.variants("alpha")), 1)
        self.assertFalse((self.home / "nope").exists())
        with self.assertRaises(StylesError):
            self.service.apply("alpha", "../../escape")


if __name__ == "__main__":
    unittest.main()
