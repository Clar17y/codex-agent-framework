"""Adversarial offline checks for the activity router and subscription path."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import io
import os
import re
import sys
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provider_runner as runner


ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def mercury_fixture_environment():
    # Offline fixtures must not depend on unrelated CI/User credentials. Keep
    # actual credential classification and strict short-secret checks intact.
    environment = {name: value for name, value in os.environ.items()
                   if not re.search(r'(?i)(api_?key|secret|password|token)', name)}
    with mock.patch.dict(os.environ, environment, clear=True), \
         mock.patch.object(runner, 'windows_user_secret', return_value=None), \
         mock.patch.object(runner, 'get_inception_api_key', return_value=None):
        yield


class ActivityDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = json.loads((ROOT / "routing.example.json").read_text())
        self.config_path = self.root / "routing.json"
        self.config_path.write_text(json.dumps(self.config))
        self.task_path = self.root / "task.json"
        self.task_path.write_text(json.dumps({"objective": "bounded", "acceptance_criteria": [],
                                              "owned_paths": ["src"], "validation": [],
                                              "instructions_files": [], "review": {"author_provider": "claude"}}))

    def args(self, activity, **updates):
        data = dict(activity=activity, config=str(self.config_path), workspace=str(self.root),
                    task_file=str(self.task_path), state_dir=str(self.root / "state"),
                    profile=None, provider=None, dry_run=False, review_reason=None,
                    muse_profile=None, review_effort=None)
        data.update(updates)
        return argparse.Namespace(**data)

    @contextmanager
    def fixture_cwd(self):
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            yield
        finally:
            os.chdir(previous)

    def test_all_activities_have_four_providers_and_registry_is_immutable(self):
        original = copy.deepcopy(self.config)
        self.assertEqual(len(self.config["activities"]), 16)
        for activity in self.config["activities"]:
            chain = runner.activity_routes(self.config, activity)
            self.assertEqual({self.config["profiles"][p]["provider"] for p in chain},
                             {"openai", "claude", "gemini", "muse"})
            for name in chain:
                provider_name, effective = runner.effective_activity_provider(self.config, name)
                self.assertEqual(effective["model"], self.config["profiles"][name]["model"])
                self.assertEqual(provider_name, self.config["profiles"][name]["provider"])
        self.assertEqual(self.config, original)

    def test_explicit_muse_api_selection_rejected_without_provider_or_with_profile(self):
        for updates in ({}, {"provider": "gemini"}, {"profile": "muse-wonder"},
                        {"provider": "muse", "profile": "muse-wonder"}):
            args = self.args("implementation", muse_auth="api", **updates)
            with self.subTest(updates=updates), mock.patch.object(runner, "execute", side_effect=AssertionError("no launch")):
                for operation in (runner.activity_status, runner.dispatch_activity):
                    with self.assertRaisesRegex(ValueError, "--provider muse|--profile"):
                        operation(args)

    def test_api_complex_activities_report_and_launch_supported_wonder_model(self):
        self.config["workflow"]["require_jev_evidence"] = False
        runner.write_json(self.config_path, self.config)
        for activity in ("implementation_complex", "implementation_long_horizon"):
            with self.subTest(activity=activity):
                args = self.args(activity, provider="muse", muse_auth="api", dry_run=True)
                status, _ = runner.activity_status(args)
                dry, code = runner.dispatch_activity(args)
                self.assertEqual(code, 0)
                self.assertTrue(all(row["model"] == "muse-wonder-1.0" for row in status["routes"]))
                self.assertEqual(dry["attempts"][0]["model"], "muse-wonder-1.0")
                self.assertEqual(dry["result"]["model"], "muse-wonder-1.0")
                command = dry["result"]["command"]
                self.assertEqual(command[command.index("--model") + 1], "muse-wonder-1.0")
                stored, _ = runner.activity_status(self.args(activity, provider="muse"))
                self.assertTrue(any(row["model"] == "muse-wonder-full-1.0" for row in stored["routes"]))

    def test_api_status_dispatch_preflight_and_model_retry_use_selected_cache(self):
        self.config["profiles"]["muse-spark-api-test"] = {"provider": "muse", "model": "muse-spark-1.3", "effort": "medium"}
        self.config["activities"]["implementation"] = ["muse-wonder", "muse-spark-api-test"]
        runner.write_json(self.config_path, self.config)
        state_dir = self.root / "state"
        runner.write_json(state_dir / "muse-quota.json", {"provider": "muse", "retry_at": time.time() + 5000})
        args = self.args("implementation", provider="muse", muse_auth="api")
        status, _ = runner.activity_status(args)
        self.assertEqual({r["status"] for r in status["routes"]}, {"available_to_try"})
        self.assertTrue(all(r["muse_auth"] == "api" and r["quota_path"].endswith("muse-api-quota.json") for r in status["routes"]))
        calls = []

        def fake_execute(route_args):
            calls.append((route_args.profile, route_args.provider, route_args.muse_auth))
            if len(calls) == 1:
                return {"status": "provider_error", "terminal_established": True}, 1
            return {"status": "completed", "muse_auth": "api"}, 0

        with mock.patch.object(runner, "execute", side_effect=fake_execute):
            result, code = runner.dispatch_activity(args)
        self.assertEqual((code, result["selected"]), (0, "muse-spark-api-test"))
        self.assertEqual(calls, [("muse-wonder", "muse", "api"), ("muse-spark-api-test", "muse", "api")])
        runner.write_json(state_dir / "muse-api-quota.json", {"provider": "muse", "muse_auth": "api", "retry_at": time.time() + 5000})
        with mock.patch.object(runner, "execute", side_effect=AssertionError("API cache must block")):
            result, code = runner.dispatch_activity(args)
        self.assertEqual((code, result["status"]), (20, "all_routes_unavailable"))
        self.assertEqual([a["status"] for a in result["attempts"]], ["fallback_required", "skipped_account_block"])
        self.assertEqual(result["attempts"][0]["muse_auth"], "api")

    def test_api_does_not_retry_duplicate_effective_models(self):
        self.config["activities"]["implementation"] = ["muse-wonder", "muse-full"]
        runner.write_json(self.config_path, self.config)
        with mock.patch.object(runner, "execute", return_value=({"status": "provider_error", "terminal_established": True}, 1)) as launch:
            result, code = runner.dispatch_activity(self.args("implementation", provider="muse", muse_auth="api"))
        self.assertEqual((result["status"], code), ("all_routes_unavailable", 20))
        self.assertEqual(launch.call_count, 1)
        self.assertEqual([a["status"] for a in result["attempts"]],
                         ["provider_error", "skipped_duplicate_route"])

    def test_mercury_fixture_isolation_keeps_actual_short_secret_guard(self):
        with mock.patch.dict(os.environ, {'MYSQL_ROOT_PASSWORD': 'root'}), \
             mock.patch.object(runner, 'windows_user_secret', return_value=None), \
             mock.patch.object(runner, 'get_inception_api_key', return_value=None):
            with self.assertRaises(runner.UnsafeSecretError):
                runner.validate_redaction_secrets(runner.redaction_secrets(self.config, mercury=True))
            with mercury_fixture_environment():
                self.assertNotIn('MYSQL_ROOT_PASSWORD', os.environ)
                runner.validate_redaction_secrets(runner.redaction_secrets(self.config, mercury=True))
            self.assertEqual(os.environ['MYSQL_ROOT_PASSWORD'], 'root')

    @mercury_fixture_environment()
    def test_mercury_generic_example_is_redacted_without_false_secret_collision(self):
        task = runner.read_json(self.task_path)
        task["objective"] = "Document the example Bearer demo-token-value"
        runner.write_json(self.task_path, task)
        args = self.args("exploration", provider="inception", profile="mercury-explorer")
        args.role = "review"
        with mock.patch.object(runner, "command_for", side_effect=ValueError("reached command construction")):
            with self.assertRaisesRegex(ValueError, "reached command construction"):
                runner.execute(args)
        prompts = list((self.root / ".llm-output/agent-framework").glob("*/prompt.txt"))
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("demo-token-value", prompts[0].read_text(encoding="utf-8"))

    def test_default_dispatch_does_not_automatically_select_api_when_stored_auth_limited(self):
        self.config["activities"]["implementation"] = ["muse-wonder", "muse-full"]
        runner.write_json(self.config_path, self.config)
        runner.write_json(self.root / "state/muse-quota.json", {"retry_at": time.time() + 5000})
        with mock.patch.dict(os.environ, {"META_API_KEY": "fake-unused-key"}), \
                mock.patch.object(runner, "execute", side_effect=AssertionError("no API fallback")):
            result, code = runner.dispatch_activity(self.args("implementation", provider="muse"))
        self.assertEqual((code, result["status"]), (20, "all_routes_unavailable"))
        self.assertEqual(result["attempts"][0]["muse_auth"], "stored")
        self.assertFalse((self.root / "state/muse-api-quota.json").exists())

    def test_api_limit_does_not_poison_default_activity_routes(self):
        self.config["activities"]["implementation"] = ["muse-wonder", "muse-full", "openai-luna"]
        # Use an existing OpenAI profile regardless of the packaged profile name.
        self.config["activities"]["implementation"][-1] = next(n for n, p in self.config["profiles"].items() if p["provider"] == "openai")
        runner.write_json(self.config_path, self.config)
        runner.write_json(self.root / "state/muse-api-quota.json", {"retry_at": time.time() + 5000})
        status, _ = runner.activity_status(self.args("implementation", provider="muse"))
        self.assertEqual({r["status"] for r in status["routes"]}, {"available_to_try"})
        # Default routing walks the normal chain; it never adds API auth.
        seen = []

        def complete(args):
            seen.append((args.provider, getattr(args, "muse_auth", None)))
            return {"status": "completed"}, 0

        with mock.patch.object(runner, "execute", side_effect=complete):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual((code, result["selected"]), (0, "muse-wonder"))
        self.assertEqual(seen, [("muse", None)])

    def test_antigravity_review_defaults_and_family_status(self):
        self.assertEqual(self.config["activities"]["independent_review"][:2], ["agy-opus", "gemini-argon"])
        for name in ("agy-opus", "agy-opus-high", "agy-sonnet", "agy-sonnet-high", "gemini-argon", "gemini-argon-high"):
            provider, effective = runner.effective_activity_provider(self.config, name)
            self.assertEqual(provider, "gemini")
            self.assertTrue(effective["model"].endswith("-" + effective["effort"]))
        legacy = self.root / "state/gemini-quota.json"
        runner.write_json(legacy, {"model": "gemini-3.8-flash-medium", "retry_at": time.time()+400000,
                                  "error": {"message": "Individual quota reached. Resets in 94h6m29s."}})
        before = legacy.read_bytes()
        result, code = runner.activity_status(self.args("independent_review"))
        statuses = {item["profile"]: item["status"] for item in result["routes"]}
        self.assertEqual(code, 0)
        self.assertEqual(statuses["gemini-flash"], "fallback_required")
        for profile in ("agy-opus", "gemini-argon", "claude-opus"):
            self.assertEqual(statuses[profile], "available_to_try")
        status, code = runner.cli_quota_status(self.args(None, provider="gemini"))
        self.assertEqual((code, status["status"]), (20, "fallback_required"))
        self.assertEqual(legacy.read_bytes(), before)

    def configure_offline_antigravity(self, chain, activity="independent_review"):
        helper = self.root / ".gemini/config/agents/framework-reader.md"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes((ROOT / "agents/framework-reader.md").read_bytes())
        fake = self.root / "offline_agy.py"
        fake.write_text('''import sys, json, os, time
from pathlib import Path
model = sys.argv[sys.argv.index("--model")+1]
with Path("launched.txt").open("a") as f: f.write(model+"\\n")
mode = os.environ.get("OFFLINE_AGY_MODE", "family")
if mode == "unknown": sys.exit(7)
if mode == "timeout": time.sleep(10)
if mode == "auth":
 print(json.dumps({"status":"ERROR", "error":{"code":"UNAUTHENTICATED", "message":"authentication required"}}), flush=True)
 sys.exit(1)
if mode == "rate":
 print(json.dumps({"status":"ERROR", "error":{"code":"429", "message":"rate limit requests/min"}}), flush=True)
 sys.exit(1)
if model.startswith("claude-"):
 message = "Weekly limit reached" if mode == "account" else "Individual quota reached. Resets in 2h."
 print(json.dumps({"status":"ERROR", "error":{"code":"QUOTA_EXHAUSTED", "message":message}}), flush=True)
 sys.exit(1)
print(json.dumps({"status":"SUCCESS", "response":"offline complete"}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["gemini"].update(executable=[sys.executable, str(fake)],
            reader_agent_file=str(helper), timeout_seconds=1, termination_grace_seconds=1, heartbeat_seconds=0)
        self.config["activities"][activity] = chain
        self.config_path.write_text(json.dumps(self.config))

    def test_offline_opus_family_failure_skips_sonnet_then_runs_argon(self):
        self.configure_offline_antigravity(["agy-opus", "agy-sonnet", "gemini-argon", "gemini-flash"])
        with mock.patch.object(runner.Path, "home", return_value=self.root):
            result, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual((code, result["selected"]), (0, "gemini-argon"), result)
        self.assertEqual([a["status"] for a in result["attempts"]], ["fallback_required", "fallback_required", "completed"])
        self.assertEqual((self.root / "launched.txt").read_text().splitlines(),
                         ["claude-opus-5-5-medium", "gemini-4-argon-medium"])
        state, error = runner.quota_state(self.root / "state", "gemini", "claude-sonnet-5-5-high")
        self.assertIsNone(error)
        self.assertEqual(state["model_family"], "claude")
        self.assertEqual(runner.quota_state(self.root / "state", "claude", "claude-opus-5-5"), (None, None))
        # A subsequent dispatch uses the persisted cache and never launches Claude.
        with mock.patch.object(runner.Path, "home", return_value=self.root):
            repeat, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual((code, repeat["selected"]), (0, "gemini-argon"))
        self.assertEqual(len((self.root / "launched.txt").read_text().splitlines()), 3)

    def test_offline_account_auth_disabled_and_rate_transitions(self):
        self.configure_offline_antigravity(["agy-opus", "agy-sonnet", "gemini-argon", "gemini-flash"])
        for mode in ("account", "auth", "rate", "disabled"):
            with self.subTest(mode=mode):
                state_dir = self.root / mode
                self.config["providers"]["gemini"]["enabled"] = mode != "disabled"
                self.config_path.write_text(json.dumps(self.config))
                with mock.patch.object(runner.Path, "home", return_value=self.root), mock.patch.dict(os.environ, {"OFFLINE_AGY_MODE": mode}):
                    result, code = runner.dispatch_activity(self.args("independent_review", state_dir=str(state_dir)))
                self.assertEqual((code, result["status"]), (20, "all_routes_unavailable"), result)
                self.assertEqual([a["status"] for a in result["attempts"]][1:], ["skipped_account_block"]*3 if mode != "rate" else ["provider_error"]*3)
                self.assertEqual((state_dir / "gemini-quota.json").exists(), mode == "account")
                statuses, _ = runner.activity_status(self.args("independent_review", state_dir=str(state_dir)))
                expected = "fallback_required" if mode == "account" else "disabled" if mode == "disabled" else "available_to_try"
                self.assertEqual({route["status"] for route in statuses["routes"]}, {expected})

    def test_offline_unknown_and_timeout_writer_stop_cross_family_fallback(self):
        self.configure_offline_antigravity(["agy-opus", "gemini-argon"], activity="implementation")
        popen = runner.subprocess.Popen
        for mode in ("unknown", "timeout", "cancelled"):
            def cancellable(command, *args, **kwargs):
                process = popen(command, *args, **kwargs)
                if mode == "cancelled" and str(self.root / "offline_agy.py") in command:
                    wait = process.wait
                    def interrupt_once(*args, **kwargs):
                        process.wait = wait
                        raise KeyboardInterrupt
                    process.wait = interrupt_once
                return process
            with (self.subTest(mode=mode), mock.patch.dict(os.environ, {"OFFLINE_AGY_MODE": mode}),
                  mock.patch.object(runner.subprocess, "Popen", side_effect=cancellable)):
                state_dir = self.root / mode
                result, code = runner.dispatch_activity(self.args("implementation", state_dir=str(state_dir)))
                self.assertEqual((code, result["status"], len(result["attempts"])), (1, "blocked_pending_run", 1), result)
                pending = Path(result["attempts"][0]["pending_path"])
                self.assertNotEqual(runner.read_json(pending)["status"], "resolved")
                self.assertFalse(list(state_dir.glob("*-quota.json")))
                if mode == "cancelled":
                    self.assertEqual(result["attempts"][0]["status"], "cancelled")
                before = pending.read_bytes()
                again, code = runner.dispatch_activity(self.args("implementation", state_dir=str(state_dir)))
                self.assertEqual((code, again["status"], len(again["attempts"])), (1, "blocked_pending_run", 1))
                self.assertEqual(pending.read_bytes(), before)

    def test_execution_recheck_observes_family_quota_written_after_preflight(self):
        self.configure_offline_antigravity(["agy-opus", "gemini-argon"])
        execute = runner.execute
        def concurrent_limit(args):
            if args.profile == "agy-opus":
                with runner.provider_lock(self.root / "state", 5, "quota.lock"):
                    runner.quota_record("gemini", {"message": "Individual quota reached. Resets in 2h."},
                        self.root / "state", "offline", self.config, model="claude-sonnet-5-5-high")
            return execute(args)
        with mock.patch.object(runner, "execute", side_effect=concurrent_limit), mock.patch.object(runner.Path, "home", return_value=self.root):
            result, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual((code, result["selected"]), (0, "gemini-argon"), result)
        self.assertEqual((self.root / "launched.txt").read_text().splitlines(), ["gemini-4-argon-medium"])
        first = runner.read_json(Path(result["attempts"][0]["logs"]) / "result.json")
        self.assertTrue(first["cached"])

    def test_execution_recheck_account_corruption_and_disable_allow_other_provider(self):
        self.configure_offline_antigravity(["agy-opus", "gemini-argon", "claude-opus"])
        execute = runner.execute
        for mode in ("account", "corrupt", "disabled"):
            with self.subTest(mode=mode):
                state_dir = self.root / mode
                self.config["providers"]["gemini"]["enabled"] = True
                self.config_path.write_text(json.dumps(self.config))
                def concurrent_block(args):
                    if args.profile == "claude-opus":
                        return {"status": "completed"}, 0
                    self.assertEqual(args.profile, "agy-opus")
                    if mode == "disabled":
                        updated = copy.deepcopy(self.config)
                        updated["providers"]["gemini"]["enabled"] = False
                        self.config_path.write_text(json.dumps(updated))
                    else:
                        path = state_dir / "gemini-quota.json"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("{" if mode == "corrupt" else json.dumps({"retry_at": time.time()+1000}))
                    return execute(args)
                with mock.patch.object(runner, "execute", side_effect=concurrent_block), mock.patch.object(runner.Path, "home", return_value=self.root):
                    result, code = runner.dispatch_activity(self.args("independent_review", state_dir=str(state_dir)))
                self.assertEqual((code, result.get("selected")), (0, "claude-opus"), result)
                self.assertFalse((self.root / "launched.txt").exists())

    def test_expired_argon_family_allows_probe_and_keeps_evidence(self):
        self.configure_offline_antigravity(["gemini-argon"])
        path = self.root / "state/gemini-argon-quota.json"
        runner.write_json(path, {"model": "gemini-4-argon-low", "retry_at": time.time()-1,
                                 "error": {"message": "Individual quota reached. Resets in 2h."}})
        before = path.read_bytes()
        with mock.patch.object(runner.Path, "home", return_value=self.root):
            result, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual((code, result.get("selected")), (0, "gemini-argon"), result)
        self.assertEqual(path.read_bytes(), before)

    def test_antigravity_review_diversity_and_high_reason_are_preserved(self):
        task = json.loads(self.task_path.read_text())
        task["review"]["author_provider"] = "gemini"
        self.task_path.write_text(json.dumps(task))
        with mock.patch.object(runner, "execute", return_value=({"status": "completed"}, 0)) as launch:
            result, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual((code, result.get("selected")), (0, "claude-opus"))
        self.assertFalse(result["same_provider_review"])
        self.config["activities"]["independent_review"] = ["agy-opus-high", "gemini-argon", "claude-opus"]
        self.config_path.write_text(json.dumps(self.config))
        task["review"]["author_provider"] = "claude"
        self.task_path.write_text(json.dumps(task))
        with mock.patch.object(runner, "execute", return_value=({"status": "completed"}, 0)):
            result, code = runner.dispatch_activity(self.args("independent_review"))
        self.assertEqual(result["attempts"][0]["status"], "review_reason_required")
        self.assertEqual(result.get("selected"), "gemini-argon")
        with self.assertRaisesRegex(ValueError, "review-reason"):
            runner.dispatch_activity(self.args("security_review"))

    def test_invalid_chain_and_disabled_profile_fail_before_launch(self):
        for bad in (["openai-sol", "openai-sol"], ["missing"], ["openai-sol"]):
            config = copy.deepcopy(self.config)
            config["activities"]["planning"] = bad
            if bad == ["openai-sol"]:
                config["profiles"]["openai-sol"]["enabled"] = False
            with self.assertRaises(ValueError):
                runner.activity_routes(config, "planning")

    def test_registry_validator_rejects_unhashable_inputs_with_domain_errors(self):
        for profile_change, chain in (({"provider": ["openai"]}, None),
                                      ({"effort": ["medium"]}, None),
                                      (None, [["openai-sol"]]),
                                      (None, [{"profile": "openai-sol"}])):
            config = copy.deepcopy(self.config)
            if profile_change:
                config["profiles"]["openai-sol"].update(profile_change)
            if chain is not None:
                config["activities"]["planning"] = chain
            with self.subTest(profile_change=profile_change, chain=chain):
                with self.assertRaises(ValueError):
                    runner.activity_routes(config, "planning")
        config = copy.deepcopy(self.config)
        config["profiles"][""] = dict(config["profiles"]["openai-sol"])
        with self.assertRaisesRegex(ValueError, "Invalid profile"):
            runner.activity_routes(config, "implementation")

    def test_codex_terminal_requires_root_thread_and_complete_final_line(self):
        good = ('{"type":"thread.started","thread_id":"x"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                '{"type":"turn.completed","usage":{}}\n')
        self.assertTrue(runner.parse_openai_terminal(good)[1])
        for bad in ('{"type":"turn.completed"}\n', good[:-1],
                    good + '{"type":"item.completed"}\n',
                    '{"type":"thread.started","thread_id":""}\n{"type":"turn.failed"}\n',
                    '{"type":"thread.started","thread_id":"x"}\n{"type":"turn.failed","thread_id":"other"}\n',
                    '{"type":"thread.started","thread_id":"x"}\n{"type":"turn.failed","error":"quota"}\n{"type":"turn.completed"}\n'):
            self.assertIsNone(runner.parse_openai_terminal(bad)[0])
        self.assertFalse(runner.quota_error([{"code": "429", "message": "rate limit"}], "openai"))
        failed = ('{"type":"thread.started","thread_id":"x"}\n'
                  '{"type":"turn.failed","error":{"code":"QUOTA_EXHAUSTED","message":"weekly quota exhausted"}}\n')
        terminal, seen, errors = runner.parse_openai_terminal(failed)
        self.assertTrue(seen)
        self.assertEqual(terminal["type"], "turn.failed")
        self.assertIsNotNone(runner.quota_error(errors, "openai"))
        auth = failed.replace("QUOTA_EXHAUSTED", "UNAUTHENTICATED").replace("weekly quota exhausted", "authentication required")
        self.assertIsNone(runner.quota_error(runner.parse_openai_terminal(auth)[2], "openai"))
        state = runner.new_progress_state("openai")
        runner.update_progress(state, {"type": "thread.started", "thread_id": "x"}, "now")
        runner.update_progress(state, {"type": "turn.completed"}, "later")
        self.assertEqual((state["conversation_id"], state["terminal"]), ("x", "SUCCESS"))
        self.assertIsNotNone(runner.quota_error([{"code": "USAGE_LIMIT_REACHED", "message": "You've reached your weekly usage limit."}], "openai"))
        self.assertIsNone(runner.quota_error([{"code": "429", "message": "Too many requests per minute"}], "openai"))

    def test_legacy_activity_flags_and_direct_role_mismatch_never_launch(self):
        base = ["provider_runner.py", "review", "--workspace", str(self.root), "--task-file", str(self.task_path),
                "--config", str(self.config_path), "--provider", "gemini", "--activity", "implementation",
                "--profile", "gemini-flash"]
        for role, activity, profile in (("review", "implementation", "gemini-flash"),
                                        ("implement", "planning", "gemini-flash")):
            argv = base.copy()
            argv[1], argv[-3], argv[-1] = role, activity, profile
            with (mock.patch.object(runner.sys, "argv", argv),
                  mock.patch.object(runner, "execute") as launch,
                  mock.patch("sys.stdout", new_callable=io.StringIO)):
                self.assertEqual(runner.main(), 1)
                launch.assert_not_called()
        with mock.patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "mismatch"):
                runner.execute(self.args("implementation", role="review", provider="gemini", profile="gemini-flash"))
            with self.assertRaisesRegex(ValueError, "mismatch"):
                runner.execute(self.args("planning", role="review", provider="claude", profile="gemini-flash"))
            launch.assert_not_called()
        argv = ["provider_runner.py", "implement", "--workspace", str(self.root), "--task-file", str(self.task_path),
                "--config", str(self.config_path), "--parent-run-id", "12345678-1234-1234-1234-123456789abc"]
        with (mock.patch.object(runner.sys, "argv", argv), mock.patch.object(runner, "execute") as launch,
              mock.patch("sys.stdout", new_callable=io.StringIO)):
            self.assertEqual(runner.main(), 1)
            launch.assert_not_called()

    def test_auth_account_skip_from_code_or_expired_oauth_text(self):
        self.config["activities"]["independent_review"] = ["claude-opus", "claude-sonnet", "openai-sol"]
        self.config_path.write_text(json.dumps(self.config))
        task = json.loads(self.task_path.read_text())
        task["review"]["author_provider"] = "openai"
        self.task_path.write_text(json.dumps(task))
        for failure in ({"status": "provider_error", "terminal_established": True, "error_codes": ["AUTHENTICATION_ERROR"], "error": ""},
                        {"status": "provider_error", "terminal_established": True, "error": "OAuth token has expired. Please run /login to refresh"}):
            launched = []
            def fake_execute(args):
                launched.append(args.profile)
                return (failure, 1) if len(launched) == 1 else ({"status": "completed"}, 0)
            with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
                result, code = runner.dispatch_activity(self.args("independent_review"))
            self.assertEqual((code, launched), (0, ["claude-opus", "openai-sol"]))
            self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")

    def test_openai_execute_fake_cli_terminal_and_uncertain_writer(self):
        fake = self.root / "fake_codex.py"
        fake.write_text('''import os, json
from pathlib import Path
mode = os.environ["FAKE_CODEX_MODE"]
Path("child-env-names.json").write_text(json.dumps(sorted(k for k in os.environ if k.startswith("CODEX_"))))
Path("parent-env.txt").write_text(os.environ.get("FRAMEWORK_PARENT_RUN_ID", ""))
print(json.dumps({"type":"thread.started","thread_id":"root"}), flush=True)
if mode == "partial":
    Path("src").mkdir(exist_ok=True)
    Path("src/partial.txt").write_text("preserve")
elif mode == "failed":
    print(json.dumps({"type":"turn.failed","thread_id":"root","error":{"code":"UNAUTHENTICATED","message":"OAuth token has expired. Please run /login"}}), flush=True)
else:
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
    print(json.dumps({"type":"turn.completed","thread_id":"root","usage":{}}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"]["executable"] = [sys.executable, str(fake)]
        self.config["providers"]["openai"]["timeout_seconds"] = 3
        self.config["providers"]["openai"]["heartbeat_seconds"] = 0
        self.config_path.write_text(json.dumps(self.config))
        for activity, role, mode, expected in (("planning", "review", "complete", "completed"),
                                               ("implementation", "implement", "complete", "completed"),
                                               ("implementation", "implement", "failed", "provider_error"),
                                               ("implementation", "implement", "partial", "uncertain_exit")):
            with self.subTest(activity=activity, mode=mode), mock.patch.dict(os.environ, {
                    "FAKE_CODEX_MODE": mode, "CODEX_HOME": str(self.root / "auth-home"),
                    "CODEX_APP_TOOLS_PIPE_PATH": "fake-pipe", "CODEX_PERMISSION_PROFILE": "denied",
                    "CODEX_THREAD_ID": "host-thread", "CODEX_SESSION_ID": "host-session",
                    "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY": "host-policy", "CODEX_API_KEY": "override",
                    "FRAMEWORK_PARENT_RUN_ID": "inherited-parent"}):
                args = self.args(activity, role=role, provider="openai", profile="openai-sol")
                result, _ = runner.execute(args)
                self.assertEqual(result["status"], expected)
                self.assertEqual(json.loads((self.root / "child-env-names.json").read_text()), ["CODEX_HOME"])
                self.assertEqual((self.root / "parent-env.txt").read_text(), "")
                if mode == "failed":
                    self.assertTrue(runner.account_auth_error(result))
                if mode == "partial":
                    self.assertEqual((self.root / "src/partial.txt").read_text(), "preserve")
                    self.assertFalse(result["fallback_authorized"])
                    self.assertEqual(runner.read_json(Path(result["pending_path"]))["status"], "uncertain_exit")

    def test_claude_writer_terminal_and_account_failures_use_result_envelope(self):
        fake = self.root / "fake_claude.py"
        fake.write_text('''import json, os, sys
mode = os.environ["FAKE_CLAUDE_MODE"]
if mode == "partial":
    print(json.dumps({"type":"assistant","message":"working"}), flush=True)
else:
    event = {"type":"result", "subtype":"success", "is_error":False, "result":"done"}
    if mode != "success":
        event.update(subtype="error", is_error=True,
                     error={"code": {"failed":"MODEL_ERROR", "auth":"UNAUTHENTICATED",
                                    "quota":"USAGE_LIMIT_REACHED", "permission":"PERMISSION_DENIED"}[mode],
                            "message": "You've hit your usage limit. Try again at 3:05 PM." if mode == "quota" else mode})
    if mode == "permission": event["denied_actions"] = ["Write"]
    print(json.dumps(event), flush=True)
    if mode != "success": sys.exit(1)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["claude"].update(executable=[sys.executable, str(fake)], heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(self.config))
        for mode, expected in (("success", "completed"), ("failed", "provider_error"),
                               ("auth", "provider_error"), ("permission", "permission_denied"),
                               ("partial", "uncertain_exit"), ("quota", "fallback_required")):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": mode}):
                result, _ = runner.execute(self.args("implementation", role="implement", provider="claude",
                                                     profile="claude-sonnet"))
                self.assertEqual(result["status"], expected)
                pending = runner.read_json(Path(result["pending_path"]))
                self.assertEqual(pending["status"], "uncertain_exit" if mode == "partial" else "resolved")
                if mode == "partial":
                    runner.write_json(Path(result["pending_path"]), {**pending, "status": "resolved"})
                if mode == "auth":
                    self.assertTrue(runner.account_auth_error(result))
                if mode == "quota":
                    self.assertTrue((Path(self.args("implementation").state_dir) / "claude-quota.json").exists())

    def test_realistic_codex_quota_prefix_only_from_failed_terminal(self):
        for message in ("You've hit your usage limit. Try again at 3:05 PM.",
                        "You've hit your usage limit. Upgrade to Pro or try again later."):
            failed = ('{"type":"thread.started","thread_id":"root"}\n' +
                      json.dumps({"type":"turn.failed", "thread_id":"root", "error":{"message": message}}) + '\n')
            terminal, seen, errors = runner.parse_openai_terminal(failed)
            self.assertTrue(seen)
            self.assertEqual(terminal["type"], "turn.failed")
            self.assertIsNotNone(runner.quota_error(errors, "openai"))
            self.assertIsNone(runner.quota_error([{"message": "The task quotes: " + message}], "openai"))
        self.assertIsNone(runner.quota_error([{"code":"429", "message":"Too many requests per minute"}], "openai"))
        self.assertIsNone(runner.quota_error([{"code":"MODEL_ERROR", "message":"model unavailable"}], "openai"))

    def test_gemini_and_muse_writer_fake_cli_terminals(self):
        from test_muse_provider import linked, terminal
        fake = self.root / "fake_other_writer.py"
        fake.write_text('''import json, os, sys
provider, mode = os.environ["FAKE_WRITER"].split(":")
if provider == "gemini":
    if mode != "partial":
        print(json.dumps({"event":"result","result":{"status":"SUCCESS" if mode == "success" else "FAILED",
                                                     "response":"done" if mode == "success" else "bad"}}), flush=True)
else:
    if mode != "partial":
        from pathlib import Path
        events = json.loads(Path("muse-events.json").read_text())
        for event in events[mode]: print(json.dumps(event), flush=True)
if mode == "failure": sys.exit(1)
''')
        (self.root / "muse-events.json").write_text(json.dumps({
            "success":[linked(), terminal()],
            "failure":[linked(), terminal("failed", "", "model failed")]}))
        self.config["workflow"]["require_jev_evidence"] = False
        for provider in ("gemini", "muse"):
            self.config["providers"][provider].update(executable=[sys.executable, str(fake)],
                                                      heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(self.config))
        for provider, profile in (("gemini", "gemini-flash"), ("muse", "muse-wonder")):
            for mode, expected in (("success", "completed"), ("failure", "provider_error"),
                                   ("partial", "uncertain_exit")):
                with self.subTest(provider=provider, mode=mode), mock.patch.dict(os.environ, {
                        "FAKE_WRITER": provider + ":" + mode}):
                    result, _ = runner.execute(self.args("implementation", role="implement",
                                                         provider=provider, profile=profile))
                    self.assertEqual(result["status"], expected)
                    pending = runner.read_json(Path(result["pending_path"]))
                    self.assertEqual(pending["status"], "uncertain_exit" if mode == "partial" else "resolved")
                    if mode == "partial":
                        runner.write_json(Path(result["pending_path"]), {**pending, "status":"resolved"})

    def test_orchestrator_child_blocks_replacement_then_resolves(self):
        fake = self.root / "fake_parent.py"
        fake.write_text('''import json, os, time
from pathlib import Path
Path("orchestrator-env.txt").write_text(os.environ.get("FRAMEWORK_PARENT_RUN_ID", ""))
time.sleep(0.6)
print(json.dumps({"type":"thread.started","thread_id":"parent"}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
print(json.dumps({"type":"turn.completed","thread_id":"parent"}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"]["executable"] = [sys.executable, str(fake)]
        self.config["providers"]["openai"]["timeout_seconds"] = 3
        self.config["providers"]["openai"]["heartbeat_seconds"] = 0
        self.config_path.write_text(json.dumps(self.config))
        task = json.loads(self.task_path.read_text())
        task["allow_delegation"] = True
        self.task_path.write_text(json.dumps(task))
        args = self.args("orchestration", role="review", provider="openai", profile="openai-sol")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.execute, args)
            directory = runner.workspace_state_dir(Path(args.state_dir), self.root) / "orchestrations"
            deadline = time.monotonic() + 2
            while not list(directory.glob("*.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            parent_path = next(directory.glob("*.json"))
            parent_id = parent_path.stem
            child_path = runner.register_child_dispatch(Path(args.state_dir), self.root, parent_id, ["src"])
            result, _ = future.result(timeout=5)
        self.assertEqual(result["status"], "blocked_pending_run")
        self.assertEqual(runner.read_json(parent_path)["status"], "uncertain_exit")
        prompt = (Path(result["logs"]) / "prompt.txt").read_text()
        self.assertIn("--state-dir " + str(Path(args.state_dir).resolve()), prompt)
        self.assertIn("--parent-run-id " + parent_id, prompt)
        self.assertEqual((self.root / "orchestrator-env.txt").read_text(), parent_id)
        replacement, _ = runner.execute(args)
        self.assertEqual(replacement["status"], "blocked_pending_run")
        other_task = self.root / "other.json"
        other_task.write_text(json.dumps({**task, "owned_paths": ["other"]}))
        disjoint, _ = runner.execute(self.args("orchestration", role="review", provider="openai", profile="openai-sol", task_file=str(other_task)))
        self.assertEqual(disjoint["status"], "provider_error")
        runner.resolve_child_dispatch(Path(args.state_dir), child_path, {"status": "completed"})
        self.assertEqual(runner.read_json(parent_path)["status"], "resolved")
        later, _ = runner.execute(args)
        self.assertEqual(later["status"], "provider_error")

    def test_child_dispatch_registers_in_parent_state_before_launch(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        runner.write_json(parent_path, {"status": "running", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id})
        observed = []
        def core(_):
            children = runner.orchestration_children(parent_path)
            observed.append(children[0]["status"])
            return {"status": "completed"}, 0
        with mock.patch.object(runner, "_dispatch_activity_core", side_effect=core):
            result, code = runner.dispatch_activity(self.args("implementation", parent_run_id=run_id))
        self.assertEqual((code, result["status"], observed), (0, "completed", ["running"]))
        self.assertEqual(runner.orchestration_children(parent_path)[0]["status"], "completed")
        runner.write_json(parent_path, {"status": "resolved", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id})
        with mock.patch.object(runner, "_dispatch_activity_core", side_effect=AssertionError("must not launch")):
            with self.assertRaisesRegex(ValueError, "no longer accepting"):
                runner.dispatch_activity(self.args("implementation", parent_run_id=run_id))

    def test_interrupted_child_dispatch_stays_unresolved(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        runner.write_json(parent_path, {"status": "running", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id})
        with mock.patch.object(runner, "_dispatch_activity_core", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.dispatch_activity(self.args("implementation", parent_run_id=run_id))
        children = runner.orchestration_children(parent_path)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["status"], "unresolved")
        runner.write_json(parent_path, {"status": "uncertain_exit", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id,
                                        "local_process_stopped": True})
        self.assertEqual(runner.settle_orchestration_parent(parent_path)["status"], "uncertain_exit")

    def test_orchestrator_claim_blocks_external_writer_but_registered_child_runs(self):
        fake = self.root / "fake_child.py"
        fake.write_text('''import json, sys
assert sys.stdin.read() == ""
print(json.dumps({"type":"thread.started","thread_id":"child"}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
print(json.dumps({"type":"turn.completed","thread_id":"child"}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"].update(executable=[sys.executable, str(fake)], heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(self.config))
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        runner.write_json(parent_path, {"status":"running", "workspace":str(self.root),
                                        "owned_paths":["src"], "run_id":run_id})
        args = self.args("implementation", role="implement", provider="openai", profile="openai-sol")
        external, _ = runner.execute(args)
        self.assertEqual(external["status"], "blocked_pending_run")
        self.assertEqual(external["pending_path"], str(parent_path))
        with self.fixture_cwd():
            child, _ = runner.dispatch_activity(self.args("implementation", profile="openai-sol",
                                                          parent_run_id=run_id,
                                                          state_dir=os.path.relpath(state_dir, Path.cwd())))
        self.assertEqual(child["status"], "completed")
        self.assertEqual(runner.orchestration_children(parent_path)[0]["status"], "completed")
        disjoint_task = self.root / "disjoint.json"
        disjoint_task.write_text(json.dumps({**json.loads(self.task_path.read_text()), "owned_paths":["other"]}))
        disjoint, _ = runner.execute(self.args("implementation", role="implement", provider="openai",
                                               profile="openai-sol", task_file=str(disjoint_task)))
        self.assertEqual(disjoint["status"], "completed")
        nested = self.root / "src"
        nested.mkdir(exist_ok=True)
        nested_task = nested / "task.json"
        nested_task.write_text(json.dumps({**json.loads(self.task_path.read_text()), "owned_paths":["nested"]}))
        cross, _ = runner.execute(self.args("implementation", role="implement", provider="openai",
                                            profile="openai-sol", workspace=str(nested),
                                            task_file=str(nested_task)))
        self.assertEqual(cross["status"], "blocked_pending_run")
        resolved = runner.operator_resolve_orchestration(state_dir, self.root, run_id, "CLI confirmed stopped")
        self.assertEqual(resolved["status"], "resolved")
        later, _ = runner.execute(args)
        self.assertEqual(later["status"], "completed")

    def test_reader_child_empty_scope_and_operator_recovery_without_provider_pending(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        runner.write_json(parent_path, {"status":"running", "workspace":str(self.root),
                                        "owned_paths":["src"], "run_id":run_id})
        with mock.patch.object(runner, "_dispatch_activity_core", return_value=({"status":"completed"}, 0)):
            reader_task = self.root / "reader.json"
            reader_task.write_text(json.dumps({**json.loads(self.task_path.read_text()), "owned_paths":[]}))
            result, _ = runner.dispatch_activity(self.args("planning", task_file=str(reader_task),
                                                            parent_run_id=run_id))
        self.assertEqual(result["status"], "completed")
        with mock.patch.object(runner, "_dispatch_activity_core", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.dispatch_activity(self.args("planning", task_file=str(reader_task),
                                                   parent_run_id=run_id))
        child = next(child for child in runner.orchestration_children(parent_path)
                     if child["status"] == "unresolved")
        self.assertEqual(child["status"], "unresolved")
        with self.assertRaisesRegex(ValueError, "every child"):
            runner.operator_resolve_orchestration(state_dir, self.root, run_id, "parent stopped")
        child_path = next(path for path in (parent_path.with_suffix("") / "children").glob("*.json")
                          if runner.read_json(path)["status"] == "unresolved")
        runner.operator_resolve_orchestration(state_dir, self.root, run_id, "reader stopped", child_id=child_path.stem)
        self.assertEqual(runner.read_json(child_path)["status"], "failed")
        self.assertEqual(runner.operator_resolve_orchestration(state_dir, self.root, run_id,
                                                               "parent stopped")["status"], "resolved")

    def test_operator_recovery_canonical_alias_and_mismatch(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        alias = Path(str(self.root).swapcase()) if os.name == "nt" else self.root
        runner.write_json(parent_path, {"status":"uncertain_exit", "workspace":str(alias),
                                        "owned_paths":["src"], "run_id":run_id})
        with self.fixture_cwd():
            state_alias = Path(os.path.relpath(state_dir, Path.cwd()))
            result = runner.operator_resolve_orchestration(state_alias, self.root, run_id,
                                                           "confirmed stopped")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(Path(result["orchestration_pending_path"]), parent_path)
        if os.name == "nt":
            runner.write_json(parent_path, {"status":"uncertain_exit", "workspace":str(alias),
                                            "owned_paths":["src"], "run_id":run_id})
            self.assertEqual(runner.operator_resolve_orchestration(Path(str(state_dir).swapcase()),
                                                                    self.root, run_id,
                                                                    "confirmed stopped")["status"], "resolved")
        other = self.root / "different-workspace"
        other.mkdir()
        runner.write_json(parent_path, {"status":"uncertain_exit", "workspace":str(other),
                                        "owned_paths":["src"], "run_id":run_id})
        with self.fixture_cwd():
            with self.assertRaisesRegex(ValueError, "workspace does not match"):
                runner.operator_resolve_orchestration(state_alias, self.root, run_id,
                                                      "must reject different workspace")

    def test_relative_state_fixture_uses_same_drive_and_restores_cwd(self):
        original_cwd = Path.cwd()
        real_relpath = os.path.relpath

        def reject_checkout_cwd(path, start=None):
            if Path(start).resolve() != self.root:
                raise ValueError("simulated cross-drive relative path")
            return real_relpath(path, start)

        with self.assertRaisesRegex(ValueError, "simulated cross-drive"):
            reject_checkout_cwd(self.root / "state", original_cwd)
        with self.fixture_cwd(), mock.patch.object(os.path, "relpath", side_effect=reject_checkout_cwd):
            self.assertEqual(Path(os.path.relpath(self.root / "state", Path.cwd())), Path("state"))
        self.assertEqual(Path.cwd(), original_cwd)

    def test_post_launch_parent_metadata_failure_and_timeout_keep_claim(self):
        fake = self.root / "slow_parent.py"
        fake.write_text("import time\ntime.sleep(3)\n")
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"].update(executable=[sys.executable, str(fake)],
                                                   timeout_seconds=0.2, termination_grace_seconds=0,
                                                   heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(self.config))
        task = json.loads(self.task_path.read_text())
        task["allow_delegation"] = True
        self.task_path.write_text(json.dumps(task))
        args = self.args("orchestration", role="review", provider="openai", profile="openai-sol")
        original = runner.write_json
        for mode in ("metadata", "timeout"):
            def fail_running(path, data):
                if mode == "metadata" and Path(path).parent.name == "orchestrations" and data.get("status") == "running":
                    raise OSError("post-launch metadata failure")
                return original(path, data)
            with mock.patch.object(runner, "write_json", side_effect=fail_running if mode == "metadata" else original):
                result, _ = runner.execute(args)
            self.assertEqual(result["status"], "setup_error" if mode == "metadata" else "timeout")
            self.assertFalse(result["fallback_authorized"])
            parent_path = Path(result["orchestration_pending_path"])
            self.assertEqual(runner.read_json(parent_path)["status"], "uncertain_exit")
            blocked, _ = runner.execute(self.args("implementation", role="implement", provider="openai",
                                                  profile="openai-sol"))
            self.assertEqual(blocked["status"], "blocked_pending_run")
            self.assertEqual(runner.operator_resolve_orchestration(Path(args.state_dir), self.root, parent_path.stem,
                                                                   "process tree and session stopped")["status"], "resolved")

    def test_legacy_root_claim_blocks_orchestrator_but_disjoint_workspace_runs(self):
        fake = self.root / "fake_parent_legacy.py"
        fake.write_text('''import json
print(json.dumps({"type":"thread.started","thread_id":"parent"}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
print(json.dumps({"type":"turn.completed","thread_id":"parent"}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"].update(executable=[sys.executable, str(fake)], heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(self.config))
        task = json.loads(self.task_path.read_text())
        task["allow_delegation"] = True
        self.task_path.write_text(json.dumps(task))
        state_dir = self.root / "state"
        runner.write_json(state_dir / "gemini-pending.json",
                          {"status":"uncertain_exit", "workspace":str(self.root), "owned_paths":["src"]})
        blocked, _ = runner.execute(self.args("orchestration", role="review", provider="openai",
                                              profile="openai-sol"))
        self.assertEqual(blocked["status"], "blocked_pending_run")
        self.assertEqual(blocked["pending_path"], str(state_dir / "gemini-pending.json"))
        self.assertFalse(list(runner.workspace_state_dir(state_dir, self.root).glob("orchestrations/*.json")))
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        other = Path(elsewhere.name)
        other_task = other / "task.json"
        other_task.write_text(json.dumps(task))
        disjoint, _ = runner.execute(self.args("orchestration", role="review", provider="openai",
                                               profile="openai-sol", workspace=str(other),
                                               task_file=str(other_task)))
        self.assertEqual(disjoint["status"], "provider_error")

    def test_cleanup_proves_launch_even_without_post_popen_flag(self):
        state_dir = self.root / "state"
        run_id = "12345678-1234-1234-1234-123456789abc"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        output = self.root / ".llm-output" / "cleanup-window"
        output.mkdir(parents=True)
        for stopped, status in ((False, "completed"), (True, "setup_error")):
            runner.write_json(parent_path, {"status":"launching", "workspace":str(self.root),
                                            "owned_paths":["src"], "run_id":run_id})
            result = {"status":status, "terminal_established":status == "completed",
                      "cleanup":{"attempted":True, "local_process_stopped":stopped},
                      "_orchestration":{"path":str(parent_path), "state_dir":str(state_dir)}}
            with mock.patch.object(runner, "git_evidence", return_value={}):
                finished, _ = runner.finish(result, output, self.root, 1)
            self.assertEqual(runner.read_json(parent_path)["status"], "uncertain_exit")
            self.assertFalse(finished["fallback_authorized"])

    def test_failed_and_partial_parent_with_running_child_remain_blocked(self):
        fake = self.root / "fake_failed_parent.py"
        fake.write_text('''import json, os, time
time.sleep(0.35)
print(json.dumps({"type":"thread.started","thread_id":"parent"}), flush=True)
if os.environ["FAKE_PARENT_MODE"] == "failed":
    print(json.dumps({"type":"turn.failed","thread_id":"parent","error":{"code":"MODEL_ERROR","message":"model failed"}}), flush=True)
''')
        self.config["workflow"]["require_jev_evidence"] = False
        self.config["providers"]["openai"]["executable"] = [sys.executable, str(fake)]
        self.config["providers"]["openai"]["timeout_seconds"] = 2
        self.config["providers"]["openai"]["heartbeat_seconds"] = 0
        self.config_path.write_text(json.dumps(self.config))
        for mode in ("failed", "partial"):
            workspace = self.root / mode
            workspace.mkdir()
            task = workspace / "task.json"
            task.write_text(json.dumps({"objective": "orchestrate", "acceptance_criteria": [], "owned_paths": ["src"],
                                        "validation": [], "instructions_files": [], "allow_delegation": True}))
            args = self.args("orchestration", role="review", provider="openai", profile="openai-sol",
                             workspace=str(workspace), task_file=str(task))
            with mock.patch.dict(os.environ, {"FAKE_PARENT_MODE": mode}), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(runner.execute, args)
                directory = runner.workspace_state_dir(Path(args.state_dir), workspace) / "orchestrations"
                deadline = time.monotonic() + 2
                while not list(directory.glob("*.json")) and time.monotonic() < deadline:
                    time.sleep(0.02)
                parent_path = next(directory.glob("*.json"))
                child_path = runner.register_child_dispatch(Path(args.state_dir), workspace, parent_path.stem, ["src"])
                result, _ = future.result(timeout=4)
            self.assertEqual(result["status"], "blocked_pending_run")
            self.assertEqual(runner.read_json(parent_path)["status"], "uncertain_exit")
            runner.resolve_child_dispatch(Path(args.state_dir), child_path, {"status": "completed"})
            self.assertEqual(runner.read_json(parent_path)["status"], "resolved" if mode == "failed" else "uncertain_exit")

    def test_orchestrator_child_uncertainty_requires_pending_resolution(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        state_dir = self.root / "state"
        parent_path = runner.orchestration_path(state_dir, self.root, run_id)
        runner.write_json(parent_path, {"status": "running", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id})
        child_path = runner.register_child_dispatch(state_dir, self.root, run_id, ["src"])
        pending = self.root / "provider-pending.json"
        runner.write_json(pending, {"status": "uncertain_exit"})
        runner.resolve_child_dispatch(state_dir, child_path, {"status": "blocked_pending_run", "pending_path": str(pending)})
        runner.write_json(parent_path, {"status": "uncertain_exit", "workspace": str(self.root),
                                        "owned_paths": ["src"], "run_id": run_id,
                                        "local_process_stopped": True, "terminal_established": True})
        self.assertEqual(runner.settle_orchestration_parent(parent_path)["status"], "uncertain_exit")
        runner.write_json(pending, {"status": "resolved"})
        self.assertEqual(runner.settle_orchestration_parent(parent_path)["status"], "resolved")

    def test_structured_thinking_activity_routes_and_capabilities(self):
        chain = runner.activity_routes(self.config, "structured_thinking")
        self.assertEqual(chain, ["claude-sonnet", "agy-sonnet", "openai-sol", "gemini-flash", "muse-wonder"])
        self.assertIn("structured_thinking", runner.READ_ACTIVITIES)
        self.assertNotIn("structured_thinking", runner.WRITE_ACTIVITIES)

        # Test registry validation requires nonempty chain and known profiles
        config = copy.deepcopy(self.config)
        config["activities"]["structured_thinking"] = []
        with self.assertRaisesRegex(ValueError, "requires a nonempty chain"):
            runner.validate_activity_registry(config)

        config["activities"]["structured_thinking"] = ["nonexistent-profile"]
        with self.assertRaisesRegex(ValueError, "references unknown or disabled profile"):
            runner.validate_activity_registry(config)

    def test_prompt_for_injects_canonical_protocol_only_for_structured_thinking(self):
        task = {
            "objective": "Design advice",
            "acceptance_criteria": ["Evaluate trade-offs"],
            "owned_paths": [],
            "validation": ["echo check"],
            "instructions_files": []
        }
        task_file = self.root / "task.json"
        task_file.write_text(json.dumps(task))

        prompt = runner.prompt_for(self.root, task_file, task, activity="structured_thinking")
        self.assertIn("ACTIVITY structured_thinking: Read and search only; do not write or run commands.", prompt)
        self.assertIn("1. Restate the question in your own words.", prompt)
        self.assertIn("2. List three plausible interpretations of the question, naming the critical assumption of each.", prompt)
        self.assertIn("3. List the strongest case against the answer you are about to give.", prompt)
        self.assertIn("4. Final answer.", prompt)
        self.assertIn("The earlier sections are committed claims", prompt)
        self.assertIn("Do not edit files, run commands, or spawn subagents.", prompt)
        self.assertIn("place the four numbered sections inside its narrative summary", prompt)

        # Other activities do not include structured thinking protocol
        for other_act in ("planning", "implementation", "independent_review"):
            with self.subTest(activity=other_act):
                other_prompt = runner.prompt_for(self.root, task_file, task, activity=other_act)
                self.assertNotIn("1. Restate the question in your own words.", other_prompt)
                self.assertNotIn("List three plausible interpretations", other_prompt)

    def test_load_structured_thinker_protocol_for_both_layouts(self):
        # 1. Codex installed layout:
        # <root>/agent-framework/scripts/provider_runner.py
        # <root>/agents/structured-thinker.toml
        codex_root = self.root / "codex_install"
        codex_agents = codex_root / "agents"
        codex_agents.mkdir(parents=True)
        codex_script_dir = codex_root / "agent-framework" / "scripts"
        codex_script_dir.mkdir(parents=True)
        codex_runner = codex_script_dir / "provider_runner.py"
        codex_runner.write_text("# runner stub")
        (codex_agents / "structured-thinker.toml").write_text(
            'name = "structured_thinker"\ndeveloper_instructions = """Codex layout protocol instructions"""'
        )
        protocol = runner.load_structured_thinker_protocol(codex_runner)
        self.assertEqual(protocol, "Codex layout protocol instructions")

        # 2. Claude installed layout (and source repo layout):
        # <root>/agent-framework/scripts/provider_runner.py
        # <root>/agent-framework/agents/structured-thinker.toml
        claude_root = self.root / "claude_install"
        claude_pkg = claude_root / "agent-framework"
        claude_agents = claude_pkg / "agents"
        claude_agents.mkdir(parents=True)
        claude_script_dir = claude_pkg / "scripts"
        claude_script_dir.mkdir(parents=True)
        claude_runner = claude_script_dir / "provider_runner.py"
        claude_runner.write_text("# runner stub")
        (claude_agents / "structured-thinker.toml").write_text(
            'name = "structured_thinker"\ndeveloper_instructions = """Claude layout protocol instructions"""'
        )
        protocol = runner.load_structured_thinker_protocol(claude_runner)
        self.assertEqual(protocol, "Claude layout protocol instructions")

    def test_structured_thinker_protocol_rejects_missing_or_empty_instructions(self):
        package = self.root / "protocol-package"
        script = package / "scripts" / "provider_runner.py"
        with self.assertRaises(FileNotFoundError):
            runner.load_structured_thinker_protocol(script)

        agents = package / "agents"
        agents.mkdir(parents=True)
        role = agents / "structured-thinker.toml"
        for content in ('name = "structured_thinker"',
                        'developer_instructions = "   "',
                        'developer_instructions = []'):
            with self.subTest(content=content):
                role.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Expected nonempty developer_instructions"):
                    runner.load_structured_thinker_protocol(script)

    def test_reader_capabilities_are_flags_not_plan_prompt(self):
        helper = self.root / ".gemini" / "config" / "agents" / "framework-reader.md"
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / "agents/framework-reader.md").read_bytes())
        for name in ("openai-sol", "claude-opus", "muse-full", "gemini-flash"):
            provider_name, provider = runner.effective_activity_provider(self.config, name)
            # Command construction must not discover the operator's installed CLI.
            provider["executable"] = [sys.executable]
            if provider_name == "gemini":
                provider.update(reader_agent_file=str(helper))
            with mock.patch.object(runner.Path, "home", return_value=self.root):
                argv = runner.command_for("review", provider, "inspect", 100, self.root,
                                          provider["effort"], provider_name, self.root, activity="planning")
            joined = " ".join(argv)
            if provider_name == "openai":
                self.assertIn("read-only", argv)
                self.assertIn('forced_login_method="chatgpt"', argv)
                self.assertIn("features.multi_agent=false", argv)
                self.assertIn("features.apps=false", argv)
                self.assertIn("agents.enabled=false", argv)
                if os.name == "nt":
                    self.assertIn('windows.sandbox="mxc"', argv)
                self.assertIn('approval_policy="never"', argv)
            elif provider_name == "claude":
                self.assertIn("Read,Glob,Grep", argv)
                self.assertIn("--strict-mcp-config", argv)
            elif provider_name == "muse":
                self.assertIn("--disable-shell", argv)
                self.assertIn("--disable-write", argv)
                self.assertIn('run.toolset=["read_file"]', argv)
            elif provider_name == "gemini":
                self.assertIn("--agent", argv)
                self.assertNotIn("--mode", argv)
                self.assertNotIn("--dangerously-skip-permissions", argv)
        helper.write_text("---\nname: framework-reader\n---\n")
        with mock.patch.object(runner.Path, "home", return_value=self.root):
            with self.assertRaises(ValueError):
                runner.validate_gemini_reader({"reader_agent": "framework-reader", "reader_agent_file": str(helper)}, self.root)

    def test_openai_orchestration_writable_roots_are_explicit_and_bounded(self):
        _, provider = runner.effective_activity_provider(self.config, "openai-sol")
        provider["executable"] = [sys.executable]
        state = self.root / "shared-state"
        cache = self.root / "provider-cache"
        provider["orchestration_writable_roots"] = [str(cache)]
        argv = runner.command_for("review", provider, "orchestrate", 30, self.root,
                                  "medium", "openai", self.root, activity="orchestration",
                                  state_dir=state)
        self.assertIn("workspace-write", argv)
        # Codex rejects --approve-for-me together with an explicit --sandbox.
        self.assertNotIn("--approve-for-me", argv)
        self.assertIn('approval_policy="never"', argv)
        if os.name == "nt":
            self.assertIn('windows.sandbox="unelevated"', argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn("sandbox_workspace_write.writable_roots=" +
                      json.dumps([str(state.resolve()), str(cache.resolve())]), argv)
        self.assertNotIn("danger-full-access", argv)
        with self.assertRaisesRegex(ValueError, "shared state"):
            runner.command_for("review", provider, "orchestrate", 30, self.root,
                               "medium", "openai", self.root, activity="orchestration")
        provider["orchestration_writable_roots"] = [str(Path.home())]
        with self.assertRaisesRegex(ValueError, "too broad"):
            runner.command_for("review", provider, "orchestrate", 30, self.root,
                               "medium", "openai", self.root, activity="orchestration",
                               state_dir=state)

    def test_windows_muse_json_config_uses_native_binary(self):
        launcher = self.root / "muse.cmd"
        launcher.write_text("@echo off\n")
        (self.root / ".muse-version").write_text("1.4.7-R6101.1\n")
        binary = self.root / "muse-bin-1.4.7-R6101.1.exe"
        binary.write_bytes(b"fixture")
        with mock.patch.object(runner.shutil, "which", return_value=str(launcher)):
            argv = runner.direct_windows_muse_config_command([str(launcher), "exec", "-c", 'run.toolset=["read_file"]'], "nt")
        self.assertEqual(argv, [str(binary.resolve()), "exec", "-c", 'run.toolset=["read_file"]'])
        binary.unlink()
        with mock.patch.object(runner.shutil, "which", return_value=str(launcher)):
            with self.assertRaises(ValueError):
                runner.direct_windows_muse_config_command([str(launcher)], "nt")

    def test_volta_codex_shell_shim_resolves_node_entry(self):
        launcher = self.root / "codex.cmd"
        launcher.write_text("@echo off\nvolta run codex %*\n")
        posix_shim = self.root / "codex"
        posix_shim.write_text("#!/bin/sh\n")
        entry = self.root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("// fixture\n")
        node = self.root / "node.exe"
        node.write_bytes(b"fixture")
        with (mock.patch.object(runner.shutil, "which", return_value=str(launcher)),
              mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(posix_shim)))):
            argv = runner.direct_windows_codex_command([str(launcher), "exec"], "nt")
        self.assertEqual(argv, [str(node.resolve()), str(entry.resolve()), "exec"])
        with (mock.patch.object(runner.shutil, "which", return_value=str(launcher)),
              mock.patch.object(runner.subprocess, "run", side_effect=runner.subprocess.TimeoutExpired("volta", 5))):
            with self.assertRaisesRegex(ValueError, "discovery timed out"):
                runner.direct_windows_codex_command([str(launcher), "exec"], "nt")
        (self.root / ".muse-version").write_text("../outside")
        with mock.patch.object(runner.shutil, "which", return_value=str(launcher)):
            with self.assertRaises(ValueError):
                runner.direct_windows_muse_config_command([str(launcher)], "nt")

    def test_gemini_reader_rejects_project_shadow_and_unselected_metadata(self):
        home = self.root / "home"
        helper = home / ".gemini" / "config" / "agents" / "framework-reader.md"
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / "agents/framework-reader.md").read_bytes())
        provider = {"reader_agent": "framework-reader", "reader_agent_file": str(helper)}
        with mock.patch.object(runner.Path, "home", return_value=home):
            runner.validate_gemini_reader(provider, self.root)
            for shadow in (self.root / ".agents" / "agents" / "framework-reader.md",
                           self.root / ".agents" / "agents" / "framework-reader" / "agent.md"):
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shadow.write_text("unsafe tools: shell\n")
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    runner.validate_gemini_reader(provider, self.root)
                shadow.unlink()
            for alias in (self.root / ".agents" / "agents" / "different-file.md",
                          home / ".gemini" / "plugins" / "example" / "agents" / "alias.md"):
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.write_text("---\nname: 'framework-reader' # selected YAML identity\ntools:\n  - shell\n---\n")
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    runner.validate_gemini_reader(provider, self.root)
                alias.unlink()
            with self.assertRaisesRegex(ValueError, "selected global"):
                runner.validate_gemini_reader({**provider, "reader_agent_file": str(self.root / "copy.md")}, self.root)
            with self.assertRaisesRegex(ValueError, "selected global"):
                runner.validate_gemini_reader({**provider, "reader_agent": "other-reader"}, self.root)

    def test_account_block_skips_same_provider_and_review_prefers_other(self):
        self.config["activities"]["implementation"] = ["openai-sol", "openai-luna", "claude-sonnet"]
        self.config_path.write_text(json.dumps(self.config))
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            if args.profile == "openai-sol":
                return {"status": "fallback_required", "logs": "first"}, 20
            return {"status": "completed", "logs": "second"}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual(code, 0)
        self.assertEqual(launches, ["openai-sol", "claude-sonnet"])
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")
        launches.clear()
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, _ = runner.dispatch_activity(self.args("independent_review"))
        self.assertNotEqual(launches[0], "claude-opus")
        self.assertFalse(result["same_provider_review"])

    def test_uncertain_writer_prevents_next_launch(self):
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            return {"status": "uncertain_exit", "fallback_authorized": False}, 1
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual((result["status"], code), ("blocked_pending_run", 1))
        self.assertEqual(len(launches), 1)

    def test_auth_failure_skips_all_same_account_profiles(self):
        self.config["activities"]["implementation"] = ["openai-sol", "openai-luna", "claude-sonnet"]
        self.config_path.write_text(json.dumps(self.config))
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            if args.provider == "openai":
                return {"status": "provider_error", "terminal_established": True,
                        "error": "authentication required"}, 1
            return {"status": "completed"}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual((code, launches), (0, ["openai-sol", "claude-sonnet"]))
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")

    def test_revoked_claude_oauth_token_skips_other_claude_profiles(self):
        self.config["activities"]["implementation"] = ["claude-opus", "claude-sonnet", "openai-sol"]
        self.config_path.write_text(json.dumps(self.config))
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            if args.provider == "claude":
                return {"status": "provider_error", "terminal_established": True, "error_codes": [],
                        "error": "Failed to authenticate. API Error: 401 OAuth access token has been revoked."}, 1
            return {"status": "completed"}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual((code, launches), (0, ["claude-opus", "openai-sol"]))
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")
        self.assertFalse((self.root / "state" / "claude-quota.json").exists())
        self.assertFalse(runner.account_auth_error({"error": "Model not found", "error_codes": []}))
        self.assertFalse(runner.account_auth_error({"error": "Rate limit exceeded", "error_codes": ["429"]}))

    def test_cached_quota_skips_same_provider_and_all_unavailable_is_explicit(self):
        self.config["activities"]["implementation"] = ["openai-sol", "openai-luna", "claude-sonnet"]
        self.config_path.write_text(json.dumps(self.config))
        blocked_state = {"retry_at": 4102444800}
        with mock.patch.object(runner, "execute") as launch, mock.patch.object(runner, "quota_state", return_value=(blocked_state, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        launch.assert_not_called()
        self.assertEqual((result["status"], code), ("all_routes_unavailable", 20))
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")
        with mock.patch.object(runner, "execute") as launch, mock.patch.object(runner, "quota_state", return_value=(None, "corrupt state")):
            result, code = runner.dispatch_activity(self.args("implementation"))
        launch.assert_not_called()
        self.assertEqual((result["status"], code), ("all_routes_unavailable", 20))
        self.assertEqual(result["attempts"][0]["status"], "state_error")

    def test_known_failure_preserves_partial_edit_and_context(self):
        self.config["activities"]["implementation"] = ["openai-sol", "claude-sonnet"]
        self.config_path.write_text(json.dumps(self.config))
        partial = self.root / "src"
        contexts = []
        def fake_execute(args):
            contexts.append(copy.deepcopy(args.prior_context))
            if args.provider == "openai":
                partial.write_text("partial user work")
                return {"status": "provider_error", "terminal_established": True,
                        "logs": "first", "evidence": {"head": "candidate"}}, 1
            self.assertEqual(partial.read_text(), "partial user work")
            return {"status": "completed", "logs": "second"}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_execute), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))
        self.assertEqual(code, 0)
        self.assertEqual(contexts[1][0]["logs"], "first")
        self.assertEqual(result["selected"], "claude-sonnet")

    def test_dry_run_pins_codex_model_without_process_or_input_mutation(self):
        self.config["workflow"]["require_jev_evidence"] = False
        self.config_path.write_text(json.dumps(self.config))
        before = self.config_path.read_bytes()
        with mock.patch.object(runner, "direct_windows_codex_command", side_effect=lambda argv, **kwargs: argv), mock.patch.object(runner.subprocess, "Popen") as launch:
            result, code = runner.dispatch_activity(self.args("planning", profile="openai-sol", dry_run=True))
        launch.assert_not_called()
        self.assertEqual((result["status"], code), ("dry_run", 0))
        argv = result["result"]["command"]
        self.assertIn("gpt-6.1-sol", argv)
        self.assertIn("read-only", argv)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_status_never_launches_and_shared_cache_path(self):
        with mock.patch.object(runner, "execute") as launch:
            result, code = runner.activity_status(self.args("verification"))
        self.assertEqual(code, 0)
        launch.assert_not_called()
        self.assertTrue(all(x["quota_path"].endswith(x["provider"] + "-quota.json") for x in result["routes"]))

    def test_mercury_explorer_validation_and_activity_restrictions(self):
        profile = self.config["profiles"].get("mercury-explorer")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["provider"], "inception")
        self.assertEqual(profile["model"], "mercury-3")
        self.assertEqual(profile["effort"], "low")

        # Exploration allows mercury-explorer when added
        config = copy.deepcopy(self.config)
        config["activities"]["exploration"].insert(0, "mercury-explorer")
        routes = runner.activity_routes(config, "exploration")
        self.assertIn("mercury-explorer", routes)

        # Other activities reject mercury-explorer / inception
        for act in ("planning", "implementation", "orchestration"):
            with self.subTest(activity=act):
                bad_config = copy.deepcopy(self.config)
                bad_config["activities"][act].insert(0, "mercury-explorer")
                with self.assertRaisesRegex(ValueError, "exploration"):
                    runner.activity_routes(bad_config, act)

        # Unsupported effort for inception is rejected
        for bad_effort in ("medium", "high", "xhigh"):
            with self.subTest(effort=bad_effort):
                bad_config = copy.deepcopy(self.config)
                bad_config["profiles"]["mercury-explorer"]["effort"] = bad_effort
                bad_config["activities"]["exploration"].insert(0, "mercury-explorer")
                with self.assertRaisesRegex(ValueError, "Invalid effort"):
                    runner.activity_routes(bad_config, "exploration")

        # command_for restrictions
        provider_name, effective = runner.effective_activity_provider(self.config, "mercury-explorer")
        out_dir = self.root / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Rejects legacy implement/review role without activity
        with self.assertRaisesRegex(ValueError, "exploration"):
            runner.command_for("implement", effective, "prompt", 180, self.root, "low", "inception", out_dir)
        with self.assertRaisesRegex(ValueError, "exploration"):
            runner.command_for("review", effective, "prompt", 180, self.root, "low", "inception", out_dir)
        # Rejects non-exploration activity
        with self.assertRaisesRegex(ValueError, "exploration"):
            runner.command_for("review", effective, "prompt", 180, self.root, "low", "inception", out_dir,
                               activity="implementation")

    def test_mercury_child_environment_and_command_construction(self):
        # Child environment filters INCEPTION_API_KEY for all providers except inception
        with mock.patch.dict(os.environ, {"INCEPTION_API_KEY": "inception-secret-xyz"}):
            for p in ("openai", "claude", "gemini", "muse"):
                env = runner.child_environment(self.config, p)
                self.assertNotIn("INCEPTION_API_KEY", env)
            env_inc = runner.child_environment(self.config, "inception")
            self.assertEqual(env_inc.get("INCEPTION_API_KEY"), "inception-secret-xyz")

        # Command construction for exploration activity
        _, effective = runner.effective_activity_provider(self.config, "mercury-explorer")
        out_dir = self.root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = runner.command_for("review", effective, "prompt", 180, self.root, "low", "inception", out_dir,
                                  activity="exploration")
        self.assertTrue(any("mercury_provider.py" in arg for arg in argv))
        self.assertIn("--corpus", argv)
        self.assertIn(str(self.root), argv)
        self.assertIn("--task-file", argv)
        self.assertIn(str(out_dir / "prompt.txt"), argv)
        self.assertIn("--output", argv)
        self.assertIn(str(out_dir / "mercury-result.json"), argv)
        self.assertIn("--temperature", argv)
        self.assertIn("1.0", argv)

    def test_mercury_dispatch_execution_and_error_handling(self):
        config = copy.deepcopy(self.config)
        config["activities"]["exploration"] = ["mercury-explorer", "claude-opus"]
        self.config_path.write_text(json.dumps(config))

        # Direct quota classification check
        self.assertIsNone(runner.inception_quota_error(
            {"status": "rate_limit_error", "error": "Rate limit 429: per-minute limit reached"}, []))
        self.assertIsNone(runner.inception_quota_error(
            {"status": "auth_error", "error": "Invalid API key"}, []))
        self.assertIsNotNone(runner.inception_quota_error(
            {"status": "quota_exhausted", "error": "You have exceeded your account quota"}, []))

        # 1. Successful exploration
        def fake_success(args):
            return {"status": "completed", "logs": "output", "terminal_established": True}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_success), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("exploration"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected"], "mercury-explorer")

        # 2. Transient 429 rate limit authorizes fallback to next route without caching quota
        def fake_rate_limit(args):
            if args.profile == "mercury-explorer":
                return {"status": "provider_error", "terminal_established": True, "error": "Rate limit 429: requests/min exceeded"}, 1
            return {"status": "completed", "logs": "fallback_done", "terminal_established": True}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_rate_limit), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("exploration"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected"], "claude-opus")
        # Quota file must not have been created
        self.assertFalse((self.root / "state" / "inception-quota.json").exists())

        # 3. True account quota exhaustion records quota and marks fallback
        def fake_quota(args):
            if args.profile == "mercury-explorer":
                return {"status": "fallback_required", "quota": {"retry_at": 4102444800}, "terminal_established": True}, 20
            return {"status": "completed", "logs": "fallback_done", "terminal_established": True}, 0
        with mock.patch.object(runner, "execute", side_effect=fake_quota), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("exploration"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected"], "claude-opus")
        self.assertEqual(result["attempts"][0]["status"], "fallback_required")

        # 4. Process aborted / unestablished terminal halts dispatch with blocked_pending_run
        def fake_killed(args):
            return {"status": "uncertain_exit", "terminal_established": False, "fallback_authorized": False}, 1
        with mock.patch.object(runner, "execute", side_effect=fake_killed), mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("exploration"))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked_pending_run")
        self.assertEqual(len(result["attempts"]), 1)

    def test_mercury_artifacts_and_host_secret_fingerprints(self):
        import hashlib
        output = self.root/'artifacts'
        output.mkdir()
        secret = 'opaque host fixture'
        source = 'api_key = "source-fixture"\nprint("' + secret + '")\n'
        (output/'prompt.txt').write_text(source)
        payload = {'response': 'token="response-fixture"', 'nested': [secret]}
        (output/'mercury-result.json').write_text(json.dumps(payload))
        (output/'stdout.log').write_text(json.dumps(payload)+'\nmalformed token="log-fixture"\n')
        (output/'invalid.json').write_text('broken api_key="invalid-fixture"')
        (output/'diff.txt').write_text('x' * (4*1024*1024+1))
        runner.sanitize_mercury_artifacts(output, [secret])
        for path in output.iterdir():
            text = path.read_text()
            for value in (secret, 'source-fixture', 'response-fixture', 'log-fixture', 'invalid-fixture'):
                self.assertNotIn(value, text)
        self.assertEqual((output/'prompt.txt').read_text().count('\n'), source.count('\n'))
        self.assertEqual(json.loads((output/'mercury-result.json').read_text())['nested'], ['[REDACTED]'])
        self.assertIn('Artifact omitted', (output/'diff.txt').read_text())
        with mock.patch.dict(os.environ, {'TYPESAFE_API_KEY':secret, 'OPENAI_API_KEY':'offline-openai-key',
                                          'INCEPTION_API_KEY':'offline-inception-key'}):
            env = runner.child_environment(self.config, 'inception')
        encoded = env['MERCURY_REDACTION_DIGESTS']
        self.assertNotIn(secret, encoded)
        self.assertNotIn('TYPESAFE_API_KEY', env)
        self.assertIn(hashlib.sha256(secret.encode()).hexdigest(), encoded)
        self.assertIn(hashlib.sha256(b'offline-openai-key').hexdigest(), encoded)
        with mock.patch.dict(os.environ, {'MERCURY_REDACTION_DIGESTS':encoded}):
            self.assertNotIn('MERCURY_REDACTION_DIGESTS', runner.child_environment(self.config, 'claude'))

    @mercury_fixture_environment()
    def test_mercury_real_subprocess_terminals_and_dispatch_fallback(self):
        # Both executables are offline fixtures: no provider CLI or network.
        wrapper = self.root / 'mercury_offline.py'
        fallback = self.root / 'fallback_offline.py'
        fixture = self.root / 'transport.json'
        fallback.write_text('import json\nprint(json.dumps({"type":"result", "subtype":"success", "is_error":False, "result":"offline fallback"}))\n')
        wrapper.write_text(
            'import sys, json, io, urllib.request, urllib.error, time\n'
            f'sys.path.insert(0, {str(ROOT / "scripts")!r})\n'
            'import mercury_provider as m\n'
            f'fixture=json.load(open({str(fixture)!r}))\n'
            'm.get_inception_api_key=lambda: None if fixture.get("missing_key") else "offline-fixture-key"\n'
            'm.MAX_TURNS=fixture.get("turns", 48)\n'
            'm.MAX_TOTAL_COMPLETION_TOKENS=fixture.get("tokens", 48000)\n'
            'm.OVERALL_TIMEOUT_SECONDS=fixture.get("deadline", 180)\n'
            'if fixture.get("terminal"):\n'
            ' print(json.dumps(fixture["terminal"]), flush=True)\n'
            ' sys.exit(fixture.get("exit", 1))\n'
            'if fixture.get("malformed"):\n'
            ' print("malformed-json", flush=True)\n'
            'if fixture.get("killed"):\n'
            ' sys.exit(7)\n'
            'class Transport:\n'
            ' def open(self, req, timeout):\n'
            '  if fixture.get("http"):\n'
            '   raise urllib.error.HTTPError(m.INCEPTION_ENDPOINT, fixture["http"], "offline", {}, io.BytesIO(fixture["message"].encode()))\n'
            '  if fixture.get("slow"):\n'
            '   class Slow(io.BytesIO):\n'
            '    def read1(self, n):\n'
            '     time.sleep(.06)\n'
            '     return super().read1(n)\n'
            '   return Slow(fixture["stream"].encode())\n'
            '  return io.BytesIO(fixture["stream"].encode())\n'
            'urllib.request.build_opener=lambda *args: Transport()\n'
            'sys.argv.pop(1)\n'
            'sys.exit(m.main())\n')
        config = copy.deepcopy(self.config)
        config['activities']['exploration'] = ['mercury-explorer', 'claude-opus']
        config['providers']['inception'].update(executable=[sys.executable, str(wrapper)],
            timeout_seconds=5, termination_grace_seconds=1, heartbeat_seconds=0)
        config['providers']['claude'].update(executable=[sys.executable, str(fallback)], heartbeat_seconds=0)
        self.config_path.write_text(json.dumps(config))
        answer = json.dumps({'answer':'ok','findings':[{'claim':'found','evidence':[{'path':'module.py','start':1,'end':1}]}], 'tests':[], 'caveats':[]})
        def stream(content=answer, reason='stop', calls=None):
            delta = {'content': content} if calls is None else {'tool_calls': calls}
            return 'data: ' + json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':reason}],
                'usage':{'completion_tokens':1,'prompt_tokens':2}}) + '\n\ndata: [DONE]\n\n'
        call = {'index':0,'id':'c','function':{'name':'list_files','arguments':'{}'}}
        known = [({'missing_key':True}, 'auth_error', 1),
                 ({'stream':stream('')}, 'error', 1),
                 ({'stream':stream('not JSON')}, 'error', 1),
                 ({'stream':'data: broken\n\n'}, 'error', 1),
                 ({'stream':stream('partial', 'length')}, 'length_truncated', 1),
                 ({'stream':stream(reason='tool_calls', calls=[call]), 'turns':1}, 'budget_exhausted', 1),
                 ({'stream':stream(), 'slow':True, 'deadline':.04}, 'timeout', 1),
                 ({'stream':stream(), 'tokens':0}, 'budget_exhausted', 1)]
        for http, message, status in ((401,'invalid_api_key','auth_error'), (404,'model_not_found','model_error'),
                                      (429,'requests/min exceeded','rate_limit_error'), (429,'insufficient_quota','quota_exhausted'),
                                      (500,'server failed','error')):
            known.append(({'http':http,'message':message}, status, 20 if status=='quota_exhausted' else 1))
            known.append(({'stream':'data: '+json.dumps({'error':{'status':http,'message':message}})+'\n\n'},
                          status, 20 if status=='quota_exhausted' else 1))
        for phrase in ('too many requests', 'tokens/min exceeded', 'tokens/sec exceeded', 'concurrency limit reached'):
            message = 'quota_exceeded: ' + phrase
            known.extend([
                ({'http':429, 'message':message}, 'rate_limit_error', 1),
                ({'stream':'data: '+json.dumps({'error':{'status':429,'message':message}})+'\n\n'}, 'rate_limit_error', 1),
            ])
        # Success runs through execute's real Popen and parses the adapter terminal.
        fixture.write_text(json.dumps({'stream':stream()}))
        with mock.patch.object(runner, 'get_inception_api_key', return_value='offline-fixture-key'):
            result, code = runner.dispatch_activity(self.args('exploration'))
        self.assertEqual((code, result['selected']), (0, 'mercury-explorer'))
        for scenario, status, exit_code in known:
            with self.subTest(status=status, scenario=scenario):
                (self.root / 'state' / 'inception-quota.json').unlink(missing_ok=True)
                fixture.write_text(json.dumps(scenario))
                with mock.patch.object(runner, 'get_inception_api_key', return_value='offline-fixture-key'):
                    result, code = runner.dispatch_activity(self.args('exploration'))
                self.assertEqual((code, result.get('selected')), (0, 'claude-opus'), result)
                attempt = result['attempts'][0]
                logs = Path(attempt['logs'])
                adapter = json.loads((logs/'mercury-result.json').read_text())
                execution = json.loads((logs/'result.json').read_text())
                self.assertEqual(adapter['status'], status)
                self.assertEqual(execution.get('exit_code', exit_code), exit_code)
                self.assertTrue(execution['terminal_established'])
                self.assertEqual((self.root/'state'/'inception-quota.json').exists(), status=='quota_exhausted')
                self.assertFalse(execution.get('cleanup'), 'Adapter should fail coherently without forced termination')
        unknown = [{'killed':True}, {'malformed':True, 'stream':stream('not JSON')}, {'terminal':{'event':'mercury.terminal','status':'running','terminal':'running'}},
                   {'terminal':{'event':'mercury.terminal','status':'error','terminal':'completed','final_parse_valid':False}},
                   {'terminal':{'event':'mercury.terminal','status':'quota_exhausted','terminal':'quota_exhausted','final_parse_valid':False},'exit':7},
                   {'terminal':{'event':'mercury.terminal','status':'completed','terminal':'completed','final_parse_valid':False}}]
        for scenario in unknown:
            with self.subTest(unknown=scenario):
                (self.root/'state'/'inception-quota.json').unlink(missing_ok=True)
                fixture.write_text(json.dumps(scenario))
                with mock.patch.object(runner, 'get_inception_api_key', return_value='offline-fixture-key'):
                    result, code = runner.dispatch_activity(self.args('exploration'))
                self.assertEqual((code, result['status']), (1,'blocked_pending_run'), result)
                self.assertEqual(len(result['attempts']),1)
                self.assertFalse((self.root/'state'/'inception-quota.json').exists())

    def test_hydrogen_model_unavailable_falls_through_to_flash_success(self):
        """Authoritative terminal model failure on Hydrogen falls through to Flash."""
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            if args.profile == "gemini-hydrogen":
                return {
                    "status": "provider_error",
                    "terminal_established": True,
                    "error": "model gemini-4-hydrogen-medium is unavailable",
                    "logs": "hydrogen-model-fail"
                }, 1
            if args.profile == "gemini-flash":
                return {"status": "completed", "logs": "flash-ok"}, 0
            raise AssertionError(f"Unexpected launch: {args.profile}")

        with mock.patch.object(runner, "execute", side_effect=fake_execute), \
             mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected"], "gemini-flash")
        self.assertEqual(launches, ["gemini-hydrogen", "gemini-flash"])
        self.assertEqual(result["attempts"][0]["profile"], "gemini-hydrogen")
        self.assertEqual(result["attempts"][0]["status"], "provider_error")
        self.assertEqual(result["attempts"][1]["profile"], "gemini-flash")
        self.assertEqual(result["attempts"][1]["status"], "completed")
        self.assertFalse((Path(self.args("implementation").state_dir) / "gemini-quota.json").exists())

    def test_active_cached_gemini_quota_blocks_both_hydrogen_and_flash(self):
        """Active cached Gemini quota blocks Hydrogen and Flash without launching either."""
        state_dir = Path(self.args("implementation").state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        quota_file = state_dir / "gemini-quota.json"
        cached_data = {"retry_at": time.time() + 7200, "reason": "rate_limit_exceeded"}
        runner.write_json(quota_file, cached_data)
        initial_bytes = quota_file.read_bytes()

        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            return {"status": "completed"}, 0

        with mock.patch.object(runner, "execute", side_effect=fake_execute):
            result, code = runner.dispatch_activity(self.args("implementation"))

        self.assertEqual(code, 0)
        self.assertEqual(result["selected"], "muse-wonder")
        self.assertEqual(launches, ["muse-wonder"])
        self.assertEqual(result["attempts"][0]["profile"], "gemini-hydrogen")
        self.assertEqual(result["attempts"][0]["status"], "fallback_required")
        self.assertEqual(result["attempts"][1]["profile"], "gemini-flash")
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")
        self.assertEqual(quota_file.read_bytes(), initial_bytes)

    def test_hydrogen_confirmed_quota_skips_same_provider_flash_and_launches_next_provider(self):
        """Confirmed quota during Hydrogen run skips same-provider Flash and advances to Muse."""
        launches = []
        def fake_execute(args):
            launches.append(args.profile)
            if args.profile == "gemini-hydrogen":
                return {
                    "status": "fallback_required",
                    "error": "Usage limit reached for account",
                    "logs": "hydrogen-quota"
                }, 20
            if args.profile == "muse-wonder":
                return {"status": "completed", "logs": "muse-ok"}, 0
            raise AssertionError(f"Unexpected launch: {args.profile}")

        with mock.patch.object(runner, "execute", side_effect=fake_execute), \
             mock.patch.object(runner, "quota_state", return_value=(None, None)):
            result, code = runner.dispatch_activity(self.args("implementation"))

        self.assertEqual(code, 0)
        self.assertEqual(result["selected"], "muse-wonder")
        self.assertEqual(launches, ["gemini-hydrogen", "muse-wonder"])
        self.assertEqual(result["attempts"][0]["profile"], "gemini-hydrogen")
        self.assertEqual(result["attempts"][0]["status"], "fallback_required")
        self.assertEqual(result["attempts"][1]["profile"], "gemini-flash")
        self.assertEqual(result["attempts"][1]["status"], "skipped_account_block")
        self.assertEqual([(a["profile"], a["status"]) for a in result["attempts"]], [
            ("gemini-hydrogen", "fallback_required"),
            ("gemini-flash", "skipped_account_block"),
            ("agy-sonnet", "skipped_account_block"),
            ("muse-wonder", "completed"),
        ])

    def test_unknown_or_generic_gemini_quota_remains_account_wide(self):
        """Gemini quota is account-wide; neither profile uses an independent allowance pool."""
        state_dir = Path(self.args("implementation").state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)

        # 1. Effective provider for both profiles is gemini
        p1, eff1 = runner.effective_activity_provider(self.config, "gemini-hydrogen")
        p2, eff2 = runner.effective_activity_provider(self.config, "gemini-flash")
        self.assertEqual((p1, eff1["model"], eff1["effort"]), ("gemini", "gemini-4-hydrogen-medium", "medium"))
        self.assertEqual((p2, eff2["model"], eff2["effort"]), ("gemini", "gemini-3.8-flash-medium", "medium"))

        # 2. Activity status shows both routing through gemini-quota.json
        status_res, code = runner.activity_status(self.args("implementation"))
        self.assertEqual(code, 0)
        gemini_routes = [r for r in status_res["routes"] if r["provider"] == "gemini"]
        self.assertEqual({r["profile"] for r in gemini_routes},
                         {"gemini-hydrogen", "gemini-flash", "agy-sonnet", "gemini-argon"})
        self.assertTrue(gemini_routes[0]["quota_path"].endswith("gemini-quota.json"))

        # 3. Setting quota on gemini provider blocks both
        quota_data = {"retry_at": time.time() + 3600, "observed_at": "2026-11-16T18:00:00Z"}
        runner.write_json(state_dir / "gemini-quota.json", quota_data)
        status_after, _ = runner.activity_status(self.args("implementation"))
        gemini_after = [r for r in status_after["routes"] if r["provider"] == "gemini"]
        self.assertEqual({r["profile"] for r in gemini_after},
                         {r["profile"] for r in gemini_routes})
        for r in gemini_after:
            self.assertEqual(r["status"], "fallback_required")

        # Confirm no hydrogen-specific quota file was created
        self.assertFalse((state_dir / "hydrogen-quota.json").exists())
        self.assertFalse((state_dir / "gemini-hydrogen-quota.json").exists())

    def test_unresolved_writer_or_timeout_on_hydrogen_stops_before_flash(self):
        """Unresolved writer, uncertain exit, or timeout on Hydrogen prevents Flash launch."""
        for failure in [
            {"status": "uncertain_exit", "fallback_authorized": False},
            {"status": "timeout", "fallback_authorized": False},
            {"status": "cancelled", "fallback_authorized": False},
            {"status": "provider_error", "terminal_established": False, "error": "broken stream"}
        ]:
            with self.subTest(failure_status=failure.get("status")):
                launches = []
                def fake_execute(args):
                    launches.append(args.profile)
                    return dict(failure), 1

                with mock.patch.object(runner, "execute", side_effect=fake_execute), \
                     mock.patch.object(runner, "quota_state", return_value=(None, None)):
                    result, code = runner.dispatch_activity(self.args("implementation"))

                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "blocked_pending_run")
                self.assertEqual(launches, ["gemini-hydrogen"])
                self.assertEqual(len(result["attempts"]), 1)


class AtomicStateWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        runner.write_json(self.path, {"state": "old"})

    @staticmethod
    def windows_error(code):
        error = PermissionError(13, "rename denied")
        error.winerror = code
        return error

    def test_transient_windows_rename_preserves_old_state_until_success(self):
        original = runner.os.replace
        for code in (5, 32, 33):
            with self.subTest(code=code):
                runner.write_json(self.path, {"state": "old"})
                calls = []
                def replace(source, target):
                    calls.append(target)
                    if len(calls) < 3:
                        self.assertEqual(runner.read_json(self.path), {"state": "old"})
                        raise self.windows_error(code)
                    return original(source, target)
                with mock.patch.object(runner.os, "replace", side_effect=replace), mock.patch.object(runner.time, "sleep") as sleep:
                    runner.write_json(self.path, {"state": "new"})
                self.assertEqual(runner.read_json(self.path), {"state": "new"})
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleep.call_args_list, [mock.call(0.01), mock.call(0.02)])
                self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_persistent_windows_rename_fails_bounded_and_keeps_old_state(self):
        with mock.patch.object(runner.os, "replace", side_effect=self.windows_error(5)) as replace, mock.patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                runner.write_json(self.path, {"state": "new"})
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(sleep.call_args_list, [mock.call(0.01), mock.call(0.02), mock.call(0.04), mock.call(0.08)])
        self.assertEqual(runner.read_json(self.path), {"state": "old"})
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_other_rename_errors_are_not_retried(self):
        for error in (PermissionError(13, "not a Windows lock"), self.windows_error(123)):
            with self.subTest(error=error), mock.patch.object(runner.os, "replace", side_effect=error) as replace, mock.patch.object(runner.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    runner.write_json(self.path, {"state": "new"})
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(runner.read_json(self.path), {"state": "old"})
            self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_interrupted_retry_propagates_and_keeps_old_state(self):
        with mock.patch.object(runner.os, "replace", side_effect=self.windows_error(32)), mock.patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.write_json(self.path, {"state": "new"})
        self.assertEqual(runner.read_json(self.path), {"state": "old"})
        self.assertFalse(list(self.path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
