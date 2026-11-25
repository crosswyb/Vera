"""Provider substitution, private persistence, and paid-task boundaries (offline)."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import requests

from live.model_budget import ModelBudget
from live.output_validation import OutputValidationError
from live.provider_settings import ModelService, ProviderSettings, SettingsUpdate, from_environment
from live.source_reader import Researcher, SearchOutcomeUnknown, TavilySearch
from live.store import LiveError
from live.workbench_contracts import NewMeeting, NewQuestion, Utterance
from live.workbench_cli import check_provider
from live.workbench_runtime import ANSWER_SYSTEM, COORDINATOR_SYSTEM, SUMMARY_SYSTEM, TextModel, WorkbenchRuntime
from live_api import create_app


class Response:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self.data = json.dumps(data or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _size):
        yield self.data


def completion(value):
    return Response({"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


class ProviderSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {
            "ZSJH_LIVE_MODEL_PROVIDER": "deepseek", "ZSJH_LIVE_MODEL_ENDPOINT": "https://api.deepseek.com",
            "ZSJH_LIVE_MODEL_NAME": "deepseek-flash", "ZSJH_LIVE_MODEL_API_KEY": "model-fixture-secret",
            "ZSJH_WB_MODEL_APPROVED": "true", "ZSJH_WB_SEARCH_APPROVED": "true",
            "ZSJH_WB_MODEL_REQUEST_LIMIT": "unlimited", "ZSJH_WB_SEARCH_REQUEST_LIMIT": "unlimited",
            "ZSJH_LIVE_MODEL_USAGE_DB": str(self.root / "model.sqlite3"),
            "ZSJH_WB_SEARCH_USAGE_DB": str(self.root / "search.sqlite3"),
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = self.root / "private/providers.json"
        self.app = create_app(self.root / "workbench.sqlite3", {"TESTING": True,
            "WORKBENCH_SETTINGS_PATH": self.path, "WORKBENCH_ROOT": self.root / "meetings"})
        self.store = self.app.extensions["live_store"]
        seed = self.store.seed(fixture=True)
        self.owner, _, _, self.agent = [self.store.authenticate(m["token"]) for m in seed["members"]]
        self.wb = self.app.extensions["workbench"]
        self.settings = self.app.extensions["workbench_settings"]
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer fixture-owner-0"}

    def draft(self):
        value = self.settings.current.model_dump()
        value["model"]["api_key"] = None
        value["search"]["api_key"] = None
        return value

    def save(self, value=None):
        return self.client.put("/live/v2/settings", json=value or self.draft(), headers=self.headers)

    def budget(self, kind="test"):
        return ModelBudget(self.root / (kind + ".sqlite3"), limit="unlimited")

    def test_settings_are_owner_only_and_origin_checked(self):
        self.assertEqual(self.client.get("/live/v2/settings").status_code, 401)
        for token in ("fixture-member-1", "fixture-agent-3"):
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(self.client.get("/live/v2/settings", headers=headers).status_code, 403)
            self.assertEqual(self.client.put("/live/v2/settings", json=self.draft(), headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=headers).status_code, 403)
        self.assertEqual(self.client.put("/live/v2/settings", json=self.draft(), headers={**self.headers,
            "Origin": "https://untrusted.invalid"}).status_code, 403)

    def test_legacy_deepseek_migration_masks_keys_and_preserves_unlimited(self):
        result = self.client.get("/live/v2/settings", headers=self.headers).get_json()
        self.assertTrue(result["model"]["api_key_configured"])
        self.assertTrue(result["search"]["use_model_key"])
        self.assertNotIn("model-fixture-secret", json.dumps(result))
        self.assertNotIn("api_key", result["model"])
        home = self.client.get("/live/v2/home", headers=self.headers).get_json()
        self.assertIsNone(home["capabilities"]["model_request_limit"])
        self.assertIsNone(home["capabilities"]["search_request_limit"])

    def test_save_is_private_atomic_and_reopened_settings_override_environment(self):
        draft = self.draft()
        draft["model"]["name"] = "my-model"
        draft["search"].update(provider="tavily", base_url="https://api.tavily.com", use_model_key=False, api_key="tavily-fixture-secret")
        response = self.save(draft)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("fixture-secret", response.get_data(as_text=True))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        reopened = ProviderSettings(self.path)
        self.assertEqual(reopened.current.model.name, "my-model")
        self.assertEqual(reopened.current.model.api_key, "model-fixture-secret")
        self.assertEqual(reopened.current.search.key(reopened.current.model), "tavily-fixture-secret")
        self.assertEqual(reopened.current.revision, 1)
        self.assertEqual(list(self.path.parent.glob(".providers-*")), [])

    def test_clear_key_is_explicit_and_does_not_resurrect_environment_on_restart(self):
        draft = self.draft()
        draft["model"]["clear_api_key"] = True
        result = self.save(draft).get_json()
        self.assertFalse(result["capabilities"]["model_enabled"])
        self.assertFalse(ProviderSettings(self.path).current.model.api_key)
        self.assertNotIn("model-fixture-secret", self.path.read_text())

    def test_wait_limit_is_local_configurable_and_survives_restart(self):
        self.assertEqual(self.settings.current.model.timeout_seconds, 180)
        draft = self.draft()
        draft["model"]["timeout_seconds"] = 240
        result = self.save(draft)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(ProviderSettings(self.path).current.model.timeout_seconds, 240)
        payload = ProviderSettings(self.path).current.model.payload("synthetic", {})
        self.assertNotIn("timeout_seconds", payload)
        for value in (0, 601, "180"):
            with self.subTest(value=value):
                draft = self.settings.current.model_dump()
                draft["model"]["timeout_seconds"] = value
                self.assertEqual(self.save(draft).status_code, 400)

    def test_automatic_generation_settings_round_trip_and_preserve_numeric_profiles(self):
        self.assertIsNone(self.settings.current.model.max_tokens)
        self.assertEqual(self.settings.current.model.token_parameter, "auto")
        legacy = self.draft()
        legacy["model"].update(name="legacy-model", max_tokens=4200, token_parameter="max_tokens")
        self.assertEqual(self.save(legacy).status_code, 200)
        draft = self.draft()
        draft["model"].update(name="automatic-model", max_tokens=None, token_parameter="auto")
        self.assertEqual(self.save(draft).status_code, 200)
        reopened = ProviderSettings(self.path)
        self.assertIsNone(reopened.current.model.max_tokens)
        self.assertEqual(reopened.current.model.token_parameter, "auto")
        saved = next(p for p in reopened.model_profiles if p.name == "legacy-model")
        self.assertEqual(saved.max_tokens, 4200)
        self.assertEqual(saved.token_parameter, "max_tokens")
        draft = self.draft()
        draft["model"] = saved.model_dump()
        self.assertEqual(self.save(draft).status_code, 200)
        self.assertEqual(ProviderSettings(self.path).current.model.max_tokens, 4200)

    def test_automatic_environment_and_legacy_numeric_limits(self):
        for value in (None, "", "auto", " AUTO "):
            env = {} if value is None else {"ZSJH_LIVE_MODEL_MAX_TOKENS": value}
            with self.subTest(value=value):
                config = from_environment(env).model
                self.assertIsNone(config.max_tokens)
                self.assertEqual(config.token_parameter, "auto")
        for provider, parameter in (("deepseek", "max_tokens"), ("openai_compatible", "max_completion_tokens")):
            config = from_environment({"ZSJH_LIVE_MODEL_PROVIDER": provider, "ZSJH_LIVE_MODEL_MAX_TOKENS": "4200"}).model
            self.assertEqual(config.max_tokens, 4200)
            self.assertEqual(config.token_parameter, parameter)
            self.assertEqual(config.payload("synthetic", {})[parameter], 4200)

    def test_invalid_custom_generation_limits_do_not_change_saved_settings(self):
        for value in (0, 255, 65537, "32768", True):
            with self.subTest(value=value):
                draft = self.draft()
                draft["model"]["max_tokens"] = value
                self.assertEqual(self.save(draft).status_code, 400)
        self.assertEqual(self.settings.current.revision, 0)
        self.assertIsNone(self.settings.current.model.max_tokens)

    def test_saved_settings_are_read_before_invalid_legacy_environment(self):
        self.assertEqual(self.save().status_code, 200)
        with patch.dict(os.environ, {"ZSJH_LIVE_MODEL_ENDPOINT": "invalid legacy address"}):
            reopened = ProviderSettings(self.path)
        self.assertTrue(reopened.current.model.configured)
        self.assertEqual(reopened.current.revision, 1)
        self.assertEqual(reopened.load_error, "")

    def test_corrupt_settings_disable_network_instead_of_silently_reverting_destinations(self):
        self.path.parent.mkdir(); self.path.write_text("{corrupt}")
        reopened = ProviderSettings(self.path)
        self.assertTrue(reopened.load_error)
        self.assertFalse(reopened.current.capabilities()["model_enabled"])
        self.assertFalse(reopened.current.capabilities()["search_enabled"])

    def test_no_credential_is_reused_for_new_destinations_or_protocols(self):
        draft = self.draft()
        draft["model"].update(provider="openai_compatible", base_url="https://gateway.example/v1", name="custom-model")
        draft["search"].update(provider="tavily", base_url="https://api.tavily.com", use_model_key=False)
        result = self.save(draft).get_json()
        self.assertFalse(result["capabilities"]["model_configured"])
        self.assertFalse(result["capabilities"]["search_configured"])
        self.assertFalse(self.settings.current.model.api_key)
        self.assertFalse(self.settings.current.search.api_key)

    def test_different_provider_cannot_borrow_model_key(self):
        draft = self.draft()
        draft["search"].update(provider="tavily", base_url="https://api.tavily.com", use_model_key=True)
        self.assertEqual(self.save(draft).status_code, 400)
        self.assertFalse(self.path.exists())

    def test_compatible_model_rename_preserves_key_for_same_service(self):
        draft = self.draft()
        draft["model"].update(provider="openai_compatible", base_url="https://gateway.example/tenant-a/v1",
                              name="model-a", api_key="compatible-fixture-secret")
        draft["search"]["use_model_key"] = False
        self.assertEqual(self.save(draft).status_code, 200)

        draft = self.draft()
        draft["model"]["name"] = "model-b"
        with patch("requests.Session.post", return_value=completion({"ok": True})) as post:
            probe = self.client.post("/live/v2/settings/test/model", json=draft, headers=self.headers)
        self.assertEqual(probe.status_code, 200, probe.get_json())
        self.assertTrue(probe.get_json()["ok"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer compatible-fixture-secret")
        self.assertEqual(self.save(draft).status_code, 200)
        self.assertEqual(ProviderSettings(self.path).current.model.api_key, "compatible-fixture-secret")

        for different in ({"base_url": "https://gateway.example/tenant-b/v1"},
                          {"base_url": "https://other.example/tenant-a/v1"}, {"provider": "deepseek"}):
            with self.subTest(different=different):
                changed = self.draft()
                changed["model"].update(name="model-c", **different)
                self.assertFalse(self.settings.preview(SettingsUpdate.model_validate(changed)).model.api_key)
        draft = self.draft()
        draft["model"].update(name="model-c", clear_api_key=True)
        self.assertFalse(self.settings.preview(SettingsUpdate.model_validate(draft)).model.api_key)

    def test_saved_compatible_models_restore_private_options_and_probe_keys(self):
        url = "https://gateway.example/tenant-a/v1"
        for name in ("A", "B"):
            draft = self.draft()
            draft["model"].update(provider="openai_compatible", base_url=url + "/chat/completions",
                                  name=name, api_key="fixture-key-" + name, max_tokens=4200 if name == "A" else 4800)
            draft["search"]["use_model_key"] = False
            self.assertEqual(self.save(draft).status_code, 200)
        reopened = ProviderSettings(self.path)
        public = reopened.public()["model_profiles"]
        self.assertEqual([p["name"] for p in public], ["B", "A", "deepseek-flash"])
        self.assertEqual(public[1]["max_tokens"], 4200)
        self.assertTrue(all(p["api_key_configured"] and "api_key" not in p for p in public))
        self.assertNotIn("fixture-key", json.dumps(public))
        draft = self.draft()
        draft["model"] = {k:v for k,v in public[1].items() if k != "api_key_configured"}
        with patch("requests.Session.post", return_value=completion({"ok": True})) as post:
            probe = self.client.post("/live/v2/settings/test/model", json=draft, headers=self.headers)
        self.assertTrue(probe.get_json()["ok"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fixture-key-A")
        self.assertEqual(reopened.current.model.name, "B")
        candidate = reopened.preview(SettingsUpdate.model_validate(draft))
        self.assertEqual(candidate.model.api_key, "fixture-key-A")
        renamed = {**draft, "model": {**draft["model"], "name": "C"}}
        self.assertEqual(reopened.preview(SettingsUpdate.model_validate(renamed)).model.api_key, "fixture-key-B")
        for different in ({"base_url": "https://gateway.example/tenant-b/v1"}, {"provider": "deepseek"}):
            with self.subTest(different=different):
                changed = {**draft, "model": {**draft["model"], **different}}
                self.assertFalse(reopened.preview(SettingsUpdate.model_validate(changed)).model.api_key)

    def test_migrated_deepseek_alias_shares_rotation_and_clear_across_templates(self):
        legacy = self.settings.current.model_dump()
        legacy["model"]["base_url"] += "/v1"
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(legacy))
        settings = ProviderSettings(self.path)
        self.assertFalse(settings.load_error)
        for name, key, clear, expected in (("deepseek-pro", None, False, "model-fixture-secret"),
                                            ("deepseek-flash", "rotated-key", False, "rotated-key"),
                                            ("deepseek-pro", None, True, "")):
            draft = settings.current.model_dump()
            draft["model"].update(base_url="https://api.deepseek.com", name=name, api_key=key, clear_api_key=clear)
            settings.commit(settings.preview(SettingsUpdate.model_validate(draft)))
            settings = ProviderSettings(self.path)
            self.assertEqual(settings.current.model.api_key, expected)
            self.assertTrue(all(p.api_key == expected for p in settings.model_profiles))
        self.assertNotIn("rotated-key", self.path.read_text())
        self.assertNotIn("model-fixture-secret", self.path.read_text())

    def test_stale_update_does_not_overwrite_saved_settings(self):
        stale = self.draft()
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(stale).status_code, 409)
        self.assertEqual(self.settings.current.revision, 1)

    def test_archive_preference_defaults_off_for_new_and_existing_settings(self):
        self.assertFalse(self.client.get("/live/v2/settings", headers=self.headers).get_json()["keep_original_caf"])
        legacy = self.settings.current.model_dump(exclude={"keep_original_caf"})
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        reopened = ProviderSettings(self.path)
        self.assertFalse(reopened.load_error)
        self.assertFalse(reopened.current.keep_original_caf)
        self.assertEqual(reopened.current.model, self.settings.current.model)

    def test_archive_preference_round_trip_and_legacy_client_preserves_choice(self):
        draft = self.draft()
        draft["keep_original_caf"] = True
        self.assertTrue(self.save(draft).get_json()["keep_original_caf"])
        self.assertTrue(ProviderSettings(self.path).current.keep_original_caf)
        legacy = self.draft()
        del legacy["keep_original_caf"]
        legacy["model"]["name"] = "other-model"
        self.assertTrue(self.save(legacy).get_json()["keep_original_caf"])
        self.assertEqual(self.settings.current.model.name, "other-model")
        for bad in ("false", 0, None):
            self.assertEqual(self.save({**self.draft(), "keep_original_caf": bad}).status_code, 400)
        self.assertTrue(self.settings.current.keep_original_caf)
        self.assertFalse(self.save({**self.draft(), "keep_original_caf": False}).get_json()["keep_original_caf"])
        self.assertFalse(ProviderSettings(self.path).current.keep_original_caf)

    def test_bad_addresses_and_unknown_protocols_do_not_change_settings(self):
        for address in ("http://api.example.com", "https://user:secret@example.com", "https://example.com?key=hidden",
                        "https://example.com#secret", "https://example.com:0", "https://example.com/\nprivate"):
            draft = self.draft()
            draft["model"]["base_url"] = address
            self.assertEqual(self.save(draft).status_code, 400, address)
        draft = self.draft(); draft["search"]["provider"] = "unknown"
        self.assertEqual(self.save(draft).status_code, 400)
        self.assertEqual(self.settings.current.revision, 0)

    def test_symlink_is_never_read_or_replaced(self):
        target = self.root / "unrelated.txt"; target.write_text("preserve me")
        self.path.parent.mkdir(); self.path.symlink_to(target)
        loaded = ProviderSettings(self.path)
        self.assertTrue(loaded.load_error)
        with self.assertRaises(LiveError):
            loaded.commit(loaded.preview(SettingsUpdate.model_validate(self.draft())))
        self.assertEqual(target.read_text(), "preserve me")

    def test_failed_write_leaves_runtime_and_saved_revision_unchanged(self):
        runtime = WorkbenchRuntime.configured(self.wb); self.addCleanup(runtime.close)
        old_model = runtime.model
        with patch("live.provider_settings.os.replace", side_effect=OSError("fixture failure")):
            self.assertEqual(self.save().status_code, 500)
        self.assertIs(runtime.model, old_model)
        self.assertEqual(self.settings.current.revision, 0)
        self.assertFalse(self.path.exists())

    def test_generic_payload_omits_vendor_parameters_and_honors_output_setting(self):
        config = ModelService(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1/chat/completions",
                              name="local-model", api_key="", json_mode=False, max_tokens=8192,
                              token_parameter="max_completion_tokens")
        self.assertTrue(config.configured)
        with patch("requests.Session.post", return_value=completion({"ok": True})) as post:
            self.assertEqual(TextModel(self.budget(), config).call(SUMMARY_SYSTEM, {"synthetic": True}), {"ok": True})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(kwargs["headers"], {})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["max_completion_tokens"], 8192)
        for field in ("max_tokens", "thinking", "reasoning_effort", "response_format"):
            self.assertNotIn(field, kwargs["json"])

    def test_automatic_budgets_reach_requests_for_answers_summaries_and_fast_tasks(self):
        cases = [
            (ModelService(), "max_tokens", 8192),
            (ModelService(thinking="enabled"), "max_tokens", 65536),
            (ModelService(thinking="enabled", base_url="https://gateway.example/v1"), "max_tokens", 32768),
            (ModelService(provider="openai_compatible", base_url="https://api.openai.com/v1", name="gpt-5"), "max_completion_tokens", 32768),
            (ModelService(provider="openai_compatible", base_url="https://api.openai.com/v1", name="gpt-5", reasoning_effort="none"), "max_completion_tokens", 8192),
            (ModelService(provider="openai_compatible", base_url="https://api.openai.com/v1", name="gpt-4o"), "max_completion_tokens", 8192),
            (ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="openai/o3"), "max_completion_tokens", 32768),
            (ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="custom"), "max_tokens", 8192),
            (ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="custom", reasoning_effort="high"), "max_tokens", 32768),
        ]
        for config, parameter, normal_budget in cases:
            model = TextModel(self.budget(), config)
            self.addCleanup(model.close)
            for system, options, expected in ((ANSWER_SYSTEM, {}, normal_budget), (SUMMARY_SYSTEM, {}, normal_budget),
                                              ("detection", {"detection": True}, 8192), ("routing", {"routing": True}, 8192)):
                with self.subTest(config=config, task=system[:12]), patch("requests.Session.post", return_value=completion({"ok": True})) as post:
                    self.assertEqual(model.call(system, {}, **options), {"ok": True})
                    payload = post.call_args.kwargs["json"]
                    self.assertEqual(payload[parameter], expected)
                    self.assertNotIn("auto", payload)
                    self.assertNotIn("max_tokens" if parameter == "max_completion_tokens" else "max_completion_tokens", payload)
            self.assertIsNone(config.max_tokens)

    def test_automatic_generation_honors_explicit_parameter_and_inherited_detection(self):
        config = ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="gpt-5",
                              token_parameter="max_tokens", detection_mode="inherit")
        for options in ({}, {"detection": True}, {"routing": True}):
            payload = config.payload("synthetic", {}, **options)
            self.assertEqual(payload["max_tokens"], 32768)
            self.assertNotIn("max_completion_tokens", payload)

    def test_automatic_limit_truncation_reports_the_resolved_budget(self):
        config = ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="gpt-5")
        response = Response({"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                             "usage": {"completion_tokens": 32768}})
        model = TextModel(self.budget(), config)
        self.addCleanup(model.close)
        with patch("requests.Session.post", return_value=response) as post, self.assertRaises(OutputValidationError) as caught:
            model.call(ANSWER_SYSTEM, {})
        self.assertEqual(caught.exception.response["max_tokens"], 32768)
        self.assertEqual(caught.exception.issues[0]["code"], "output_token_limit")
        self.assertTrue(caught.exception.terminal)
        self.assertEqual(post.call_count, 1)

    def test_read_only_doctor_checks_selected_model_without_a_paid_completion(self):
        config = ModelService(provider="openai_compatible", base_url="https://gateway.example/v1", name="custom-model", api_key="custom-fixture")
        with patch("requests.Session.post", side_effect=AssertionError("paid call")), patch("requests.Session.get", return_value=Response()) as get:
            self.assertEqual(check_provider(config)["state"], "connected")
        self.assertEqual(get.call_args.args[0], "https://gateway.example/v1/models")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_deepseek_thinking_and_explicit_reasoning_are_configurable(self):
        config = ModelService(thinking="enabled", reasoning_effort="max", max_tokens=4096)
        payload = config.payload("JSON", {"synthetic": True})
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 4096)

    def test_fenced_json_is_accepted_but_truncated_output_is_rejected(self):
        result = {"choices": [{"message": {"content": '```json\n{"ok":true}\n```'}, "finish_reason": "stop"}]}
        model = TextModel(self.budget())
        with patch("requests.Session.post", return_value=Response(result)):
            self.assertEqual(model.call("JSON", {}), {"ok": True})
        result["choices"][0]["finish_reason"] = "length"
        with patch("requests.Session.post", return_value=Response(result)), self.assertRaises(ValueError):
            model.call("JSON", {})

    def test_hot_swap_pins_inflight_model_and_keeps_audio_and_permissions(self):
        runtime = WorkbenchRuntime.configured(self.wb); self.addCleanup(runtime.close)
        first_model = runtime.model
        meeting = self.wb.create(self.owner, NewMeeting(title="合成切换", data_scope="synthetic"))
        entered, release = threading.Event(), threading.Event()
        sent = []
        def post(url, **kwargs):
            sent.append((url, kwargs["headers"].get("Authorization")))
            if len(sent) == 1:
                entered.set(); self.assertTrue(release.wait(3))
            return completion({"ok": True})
        with patch("requests.Session.post", side_effect=post), ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(first_model.call, "JSON", {"synthetic": True})
            self.assertTrue(entered.wait(2))
            draft = self.draft()
            draft["model"].update(base_url="https://gateway.example/v1", provider="openai_compatible", api_key="new-fixture-secret")
            draft["search"]["use_model_key"] = False
            try:
                self.assertEqual(self.save(draft).status_code, 200)
                self.wb.utterance(self.owner, meeting["id"], Utterance(text="切换时转写仍可接收"))
            finally:
                release.set()
            self.assertEqual(active.result(), {"ok": True})
            runtime.model.call("JSON", {"synthetic": True})
        self.assertEqual(sent, [("https://api.deepseek.com/chat/completions", "Bearer model-fixture-secret"),
                                ("https://gateway.example/v1/chat/completions", "Bearer new-fixture-secret")])
        self.assertIsNot(runtime.model, first_model)
        snapshot = self.wb.snapshot(self.owner, meeting["id"])
        self.assertEqual(snapshot["meeting"]["permissions"], meeting["permissions"])
        self.assertEqual(len(snapshot["segments"]), 1)
        with sqlite3.connect(first_model.budget.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_model_probe_uses_synthetic_content_without_saving_or_leaking_keys(self):
        with patch("requests.Session.post", return_value=completion({"ok": True})) as post:
            result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
        self.assertTrue(result["ok"])
        self.assertIn("synthetic", post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.settings.current.revision, 0)
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_opencode_probe_sends_required_headers_without_saving_the_draft(self):
        draft = self.draft()
        draft["model"].update(provider="openai_compatible", base_url="https://opencode.ai/zen/go/v1",
                              name="deepseek-v4.1-flash", api_key="opencode-fixture-secret", json_mode=False)
        draft["search"]["use_model_key"] = False
        def go(url, **kwargs):
            headers = kwargs["headers"]
            if not headers.get("x-opencode-session"):
                return Response({"error": {"type": "MissingSessionID", "message": "missing session"}}, 400)
            self.assertTrue(headers["User-Agent"].startswith("ZSJHWorkbench/"))
            self.assertEqual(headers["Authorization"], "Bearer opencode-fixture-secret")
            return completion({"ok": True})
        with patch("requests.Session.post", side_effect=go) as post:
            result = self.client.post("/live/v2/settings/test/model", json=draft, headers=self.headers).get_json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.settings.current.revision, 0)

    def test_opencode_session_follows_the_meeting_across_all_model_tasks_and_reconfiguration(self):
        config = ModelService(provider="openai_compatible", base_url="https://opencode.ai/inference/go/openai/v1",
                              name="deepseek-v4.1-flash", api_key="opencode-fixture-secret", json_mode=False)
        work = {"meeting": {"id": "meeting-one", "title": "私有会议名称", "coordinator_cursor": 0}, "cursor": 2,
                "questions": [], "segments": [{"id": f"s{n}", "revision": 1, "text": "合成发言" * 3000} for n in range(2)]}
        def go(url, **kwargs):
            system = kwargs["json"]["messages"][0]["content"]
            prompt = json.loads(kwargs["json"]["messages"][1]["content"])
            if system.startswith(COORDINATOR_SYSTEM):
                return completion({"actions": []})
            if system.startswith(ANSWER_SYSTEM):
                return completion({"answer": "没有可核验资料。", "claims": [], "uncertainty": [], "evidence_status": "insufficient",
                    "brief": {"short_answer": "没有可核验资料。", "key_points": [{"text": "仅用于合成验证。", "kind": "explanation", "claim_indices": []}], "caveat": "仅用于离线验证。"}})
            if "chunk_notes" in prompt:
                return completion(prompt["chunk_notes"][0])
            segment = prompt["segments"][0]
            return completion({"overview": "合成纪要。", "topics": [{"text": "合成要点。", "evidence_ids": [segment["evidence_id"]]}],
                               "decisions": [], "actions": [], "open_questions": []})
        with patch("requests.Session.post", side_effect=go) as post:
            model = TextModel(self.budget(), config)
            model.decisions(work)
            model.answer(work["meeting"], {"title": "合成问题"}, [])
            model.summarize(work)
            TextModel(self.budget(), config).decisions(work)
            model.answer({**work["meeting"], "id": "meeting-two"}, {"title": "合成问题"}, [])
        sessions = [call.kwargs["headers"]["x-opencode-session"] for call in post.call_args_list]
        self.assertEqual(len(sessions), 7)  # Includes both summary batches and their reduction.
        self.assertEqual(len(set(sessions[:-1])), 1)
        self.assertNotEqual(sessions[0], sessions[-1])
        self.assertNotIn("私有会议名称", sessions[0])

    def test_opencode_connection_probes_have_separate_sessions(self):
        config = ModelService(provider="openai_compatible", base_url="https://opencode.ai/zen/go/v1")
        with patch("requests.Session.post", return_value=completion({"ok": True})) as post:
            first = TextModel(self.budget(), config)
            first.call("JSON", {})
            first.call("JSON", {})
            TextModel(self.budget(), config).call("JSON", {})
        sessions = [call.kwargs["headers"]["x-opencode-session"] for call in post.call_args_list]
        self.assertEqual(sessions[0], sessions[1])
        self.assertNotEqual(sessions[0], sessions[2])

    def test_opencode_deferred_detection_keeps_the_meeting_session_but_http_errors_are_not_retried(self):
        from live.model_transport import ModelRequestError
        config = ModelService(provider="openai_compatible", base_url="https://opencode.ai/zen/go/v1")
        work = {"meeting": {"id": "meeting-one", "coordinator_cursor": 0}, "cursor": 1, "questions": [], "segments": []}
        model = TextModel(self.budget(), config)
        with patch("requests.Session.post", side_effect=[completion({"actions": [{"kind": "invalid"}]}), completion({"actions": []})]) as post:
            rejected = model.decisions(work)
            self.assertTrue(rejected.failures)
            self.assertEqual(post.call_count, 1)
            model.decisions({**work, 'detection_task': {'kind': 'scan'}})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["x-opencode-session"],
                         post.call_args_list[1].kwargs["headers"]["x-opencode-session"])
        with patch("requests.Session.post", return_value=Response({"error": {"code": "api_key_expired"}}, 401)) as post:
            with self.assertRaises(ModelRequestError):
                model.decisions(work)
        self.assertEqual(post.call_count, 1)

    def test_opencode_headers_are_not_forwarded_to_other_hosts_or_routes(self):
        for url in ("https://opencode.ai/zen/v1", "https://opencode.ai/zen/go-else/v1",
                    "https://opencode.ai.attacker.invalid/zen/go/v1", "https://gateway.example/v1"):
            with self.subTest(url=url), patch("requests.Session.post", return_value=completion({"ok": True})) as post:
                config = ModelService(provider="openai_compatible", base_url=url)
                TextModel(self.budget(), config).call("JSON", {})
                self.assertNotIn("x-opencode-session", post.call_args.kwargs["headers"])

    def test_probe_identifies_expired_keys_without_leaking_body_or_retrying(self):
        body = {"error": {"type": "authentication_error", "code": "api_key_expired", "message": "model-fixture-secret"}}
        with patch("requests.Session.post", return_value=Response(body, 401)) as post:
            result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
        self.assertFalse(result["ok"])
        self.assertIn("已过期", result["message"])
        self.assertIn("api_key_expired", result["message"])
        self.assertNotIn("model-fixture-secret", json.dumps(result))
        self.assertEqual(post.call_count, 1)
        with sqlite3.connect(self.root / "model.sqlite3") as db:
            self.assertNotIn("model-fixture-secret", str(db.execute("SELECT usage FROM requests").fetchall()))

    def test_probe_identifies_missing_session_instead_of_blaming_the_key(self):
        body = {"error": {"type": "MissingSessionID", "message": "model-fixture-secret"}}
        with patch("requests.Session.post", return_value=Response(body, 400)):
            result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
        self.assertIn("x-opencode-session", result["message"])
        self.assertNotIn("model-fixture-secret", json.dumps(result))

    def test_probe_identifies_only_allowlisted_rejected_parameters(self):
        for parameter in ("reasoning_effort", "model-fixture-secret"):
            body = {"error": {"type": "invalid_request_error", "param": parameter, "message": "model-fixture-secret"}}
            with self.subTest(parameter=parameter), patch("requests.Session.post", return_value=Response(body, 400)):
                result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
            self.assertIn("参数", result["message"])
            self.assertNotIn("model-fixture-secret", json.dumps(result))
            if parameter == "reasoning_effort":
                self.assertIn(parameter, result["message"])

    def test_http_error_diagnostics_bound_body_and_ignore_unstructured_values(self):
        class OversizedResponse(Response):
            def iter_content(self, size):
                for _ in range(4):
                    yield b"x" * 4096
                raise AssertionError("error body was read without a size bound")
        malformed = Response()
        malformed.data = b"not JSON model-fixture-secret"
        responses = [OversizedResponse(status=400), malformed,
                     Response({"error": ["model-fixture-secret"]}, 400),
                     Response({"error": {"code": ["model-fixture-secret"], "type": "model-fixture-secret"}}, 400)]
        malformed.status_code = 400
        for response in responses:
            with self.subTest(response=type(response).__name__), patch("requests.Session.post", return_value=response) as post:
                result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
            self.assertFalse(result["ok"])
            self.assertIn("400", result["message"])
            self.assertNotIn("model-fixture-secret", json.dumps(result))
            self.assertEqual(post.call_count, 1)

    def test_probe_does_not_follow_redirect_or_print_remote_body(self):
        with patch("requests.Session.post", return_value=Response({"error": "model-fixture-secret"}, 302)) as post:
            result = self.client.post("/live/v2/settings/test/model", json=self.draft(), headers=self.headers).get_json()
        self.assertFalse(result["ok"])
        self.assertIn("302", result["message"])
        self.assertNotIn("model-fixture-secret", json.dumps(result))
        self.assertEqual(post.call_count, 1)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_tavily_uses_separate_key_and_raw_content_for_reference_markdown(self):
        quote = "这是合成的网页原文，只用于验证搜索接入与引用保存。"
        raw = quote * 8
        search = TavilySearch("tavily-fixture-secret", self.budget(), "https://api.tavily.com/search")
        meeting = self.wb.create(self.owner, NewMeeting(title="Tavily 合成验证", data_scope="synthetic"))
        self.wb.question(self.owner, meeting["id"], NewQuestion(title="工作流定义"))
        work = self.wb.claim(self.agent)
        class Model:
            def answer(_, meeting, question, sources):
                return {"answer": "合成资料已整理，供参会者核对。", "claims": [{"text": "合成测试主张。",
                    "source_id": sources[0]["id"], "quote": quote}], "uncertainty": ["离线协议验证。"], "evidence_status": "supported"}
        with patch("requests.Session.post", return_value=Response({"results": [{"title": "合成原文", "url": "https://fixture.invalid/workflow",
            "content": "不能当作原文的搜索摘要", "raw_content": raw}]})) as post:
            result = Researcher(search, Model()).run(work["meeting"], work["question"], lambda _: None)
        self.wb.finish(self.agent, work, result)
        self.assertEqual(post.call_args.args[0], "https://api.tavily.com/search")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tavily-fixture-secret")
        self.assertEqual(post.call_args.kwargs["json"]["include_raw_content"], "text")
        self.assertFalse(post.call_args.kwargs["json"]["include_answer"])
        source = result["sources"][0]
        self.assertEqual(source["text"], raw)
        self.assertEqual(source["read_method"], "tavily_search_returned_raw_content")
        doc = self.wb.snapshot(self.owner, meeting["id"])["documents"][0]
        content = self.wb.writer.path(doc["path"]).read_text()
        self.assertIn(quote, content)
        self.assertNotIn("fixture-secret", content)

    def test_tavily_snippets_without_original_bodies_never_become_evidence(self):
        with patch("requests.Session.post", return_value=Response({"results": [{"url": "https://fixture.invalid/example",
                "content": "This is only a snippet. " * 30, "raw_content": None}]})):
            search = TavilySearch("fixture", self.budget())
            result = Researcher(search, object()).run({}, {"title": "公开技术问题"}, lambda _: None)
        self.assertEqual(result["evidence_status"], "insufficient")
        self.assertEqual(result["sources"], [])

    def test_tavily_transport_failure_is_accounted_without_automatic_retry(self):
        budget = self.budget()
        with patch("requests.Session.post", side_effect=requests.ReadTimeout("tavily-fixture-secret")) as post:
            with self.assertRaises(SearchOutcomeUnknown) as caught:
                TavilySearch("tavily-fixture-secret", budget).search("public query")
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(post.call_count, 1)
        with sqlite3.connect(budget.path) as db:
            rows = db.execute("SELECT usage FROM requests").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["state"], "outcome_unknown")

    def test_search_probe_uses_public_fixed_query_and_does_not_save(self):
        draft = self.draft()
        draft["search"].update(provider="tavily", base_url="https://api.tavily.com", api_key="fixture", use_model_key=False)
        with patch("requests.Session.post", return_value=Response({"results": []})) as post:
            result = self.client.post("/live/v2/settings/test/search", json=draft, headers=self.headers).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_count"], 0)
        self.assertEqual(post.call_args.kwargs["json"]["query"], "Python official documentation")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
