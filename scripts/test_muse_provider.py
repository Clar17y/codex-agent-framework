"""Muse exec integration against JSONL v1 captured from native Muse Code 1.4.6.

The event shape comes from .llm-output/muse-runtime-fixtures/echo-success.stdout.jsonl.
The tests use a local fake CLI; no account, provider request, or token is needed.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

import provider_runner as runner


ROOT_ID = "9fbd91ca-c205-466a-a420-9df2aa643caa"
CHILD_ID = "01a13165-51c9-7422-a1d8-2b169dad8f73"
SESSION_ID = "01a13165-4e43-7072-8344-a1f2e4584d3b"


def record(event, payload, sequence=1):
    return {"schema_version": 1, "stream": {"kind": "session", "id": SESSION_ID},
            "sequence": sequence, "record_type": "event", "payload_type": event,
            "payload_schema_version": 1, "payload": payload}


def linked():
    return record("session.run.linked", {"kind": "session_run_linked", "command_id": ROOT_ID,
                                         "run_stream": {"kind": "run", "id": ROOT_ID}})


def terminal(state="completed", text="Done", reason=None, run_id=ROOT_ID):
    return record("run.terminal." + state,
                  {"kind": "run_terminal", "command_id": run_id,
                   "run_stream": {"kind": "run", "id": run_id},
                   "terminal": state, "text": text, "reason": reason}, 2)


class MuseProviderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent / ".llm-output" / ("test_muse_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.fake = self.root / "fake.py"
        self.config = self.root / "routing.json"
        self.task = self.root / "task.json"
        self.state = self.root / "state"
        runner.write_json(self.task, {"objective": "Exercise Muse", "owned_paths": ["owned.py"],
                                      "acceptance_criteria": ["Done"], "validation": [], "instructions_files": []})
        self.settings = {"providers": {"muse": {"executable": [sys.executable, str(self.fake)],
                                                  "model": "muse-wonder-1.0", "effort": "medium",
                                                  "heartbeat_seconds": 0, "timeout_seconds": 3,
                                                  "profiles": {
                                                      "complex": {"model": "muse-wonder-full-1.0", "effort": "medium"},
                                                      "escalation": {"model": "muse-wonder-full-1.0", "effort": "high"}}}},
                         "quota_probe_seconds": 300}
        self.args = argparse.Namespace(role="implement", provider="muse", workspace=str(self.root),
                                       task_file=str(self.task), config=str(self.config),
                                       state_dir=str(self.state), dry_run=False, muse_profile=None)

    def emit(self, events, code=0, stderr=""):
        self.fake.write_text("import sys\n" +
                             "print(" + repr("\n".join(json.dumps(event) for event in events)) + ")\n" +
                             "print(" + repr(stderr) + ", file=sys.stderr)\n" +
                             "sys.exit(" + repr(code) + ")\n", encoding="utf-8")

    def run_muse(self):
        runner.write_json(self.config, self.settings)
        return runner.execute(self.args)

    def pending(self):
        path = runner.workspace_state_dir(self.state, self.root) / "gemini-pending"
        return [runner.read_json(item) for item in path.glob("*.json")]

    def test_command_and_cli_default(self):
        self.emit([linked(), terminal()])
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))
        command = runner.command_for("implement", self.settings["providers"]["muse"], "unused", 3,
                                     self.root, provider_name="muse", output_dir=self.root)
        self.assertIn("--prompt-file", command)
        self.assertEqual(command[command.index("--model") + 1], "muse-wonder-1.0")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "medium")
        self.assertEqual(self.pending()[0]["status"], "resolved")
        self.settings["providers"]["muse"]["model"] = "cli-default"
        args = runner.command_for("implement", self.settings["providers"]["muse"], "unused", 3,
                                  self.root, provider_name="muse", output_dir=self.root)
        self.assertNotIn("--model", args)
        self.assertNotIn("--yolo", args)
        self.assertNotIn("--disable-sandbox", args)
        self.assertIn("--disable-approval", args)

    def test_root_terminal_required_and_child_ignored(self):
        cases = [([linked(), terminal(run_id=CHILD_ID)], 0),
                 ([linked(), record("run.output.delta", {"kind": "run_output_delta", "command_id": ROOT_ID,
                                                       "run_stream": {"kind": "run", "id": ROOT_ID},
                                                       "text": "Done"})], 0),
                 ([linked()], 0), ([], 0)]
        other_session_terminal = terminal()
        other_session_terminal["stream"]["id"] = "different-session"
        cases.append(([linked(), other_session_terminal], 0))
        for events, exit_code in cases:
            with self.subTest(events=events):
                self.state = self.root / ("state_" + uuid.uuid4().hex)
                self.args.state_dir = str(self.state)
                self.emit(events, exit_code)
                result, code = self.run_muse()
                self.assertEqual((result["status"], code), ("uncertain_exit", 1))
                self.assertEqual(self.pending()[0]["status"], "uncertain_exit")

    def test_malformed_terminal_and_large_log(self):
        self.fake.write_text("print(" + repr(json.dumps(linked()) + "\n{\"schema_version\":1,\"payload_type\":\"run.terminal.completed\"") + ")\n",
                             encoding="utf-8")
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("uncertain_exit", 1))
        self.state = self.root / "large_state"
        self.args.state_dir = str(self.state)
        # An oversize progress record drops out of telemetry, while the early
        # link and later terminal remain available in the bounded stream parser.
        self.fake.write_text("print(" + repr(json.dumps(linked())) + ")\n" +
                             "print('x' * 2200000)\n" +
                             "print(" + repr(json.dumps(terminal())) + ")\n", encoding="utf-8")
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertEqual(self.pending()[0]["status"], "resolved")

    def test_malformed_terminal_fields_remain_pending(self):
        cases = []
        for key, value in (("text", None), ("text", 7), ("reason", []),
                           ("reason", {"message": "failed"})):
            event = terminal("failed", "", "model failed")
            event["payload"][key] = value
            cases.append(event)
        for key in ("text", "reason"):
            event = terminal("failed", "", "model failed")
            del event["payload"][key]
            cases.append(event)
        event = terminal("failed", "", "model failed")
        event["payload_schema_version"] = 2
        cases.append(event)
        for malformed in cases:
            with self.subTest(malformed=malformed):
                self.state = self.root / ("state_" + uuid.uuid4().hex)
                self.args.state_dir = str(self.state)
                self.emit([linked(), malformed], 1)
                result, code = self.run_muse()
                self.assertEqual((result["status"], code), ("uncertain_exit", 1))
                self.assertEqual(self.pending()[0]["status"], "uncertain_exit")
        self.state = self.root / "empty_text_state"
        self.args.state_dir = str(self.state)
        self.emit([linked(), terminal("completed", "", None)], 0)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertEqual(self.pending()[0]["status"], "resolved")

    def test_cross_provider_ownership_overlap_and_disjoint(self):
        workspace_state = runner.workspace_state_dir(self.state, self.root)
        pending_dir = workspace_state / "gemini-pending"
        pending_dir.mkdir(parents=True)
        record_path = pending_dir / "gemini.json"
        runner.write_json(record_path, {"status": "running", "workspace": str(self.root),
                                        "owned_paths": ["owned.py"], "provider": "gemini"})
        self.emit([linked(), terminal()])
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("overlap launched")):
            blocked, code = self.run_muse()
        self.assertEqual((blocked["status"], code), ("blocked_pending_run", 1))
        runner.write_json(self.task, {"objective": "Disjoint", "owned_paths": ["other.py"],
                                      "acceptance_criteria": ["Done"], "validation": [], "instructions_files": []})
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))
        runner.write_json(self.task, {"objective": "Ancestor", "owned_paths": ["src/file.py"],
                                      "acceptance_criteria": ["Done"], "validation": [], "instructions_files": []})
        runner.write_json(record_path, {"status": "running", "workspace": str(self.root),
                                        "owned_paths": ["src"], "provider": "gemini"})
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("ancestor overlap launched")):
            blocked, code = self.run_muse()
        self.assertEqual((blocked["status"], code), ("blocked_pending_run", 1))

    def test_uncertain_muse_claim_blocks_gemini(self):
        self.emit([linked()], 0)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("uncertain_exit", 1))
        self.settings["providers"]["gemini"] = {"executable": [sys.executable, str(self.fake)],
                                                   "model": "gemini-3.8-flash-medium"}
        runner.write_json(self.config, self.settings)
        gemini = argparse.Namespace(**{**vars(self.args), "provider": "gemini"})
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("overlap launched")):
            blocked, code = runner.execute(gemini)
        self.assertEqual((blocked["status"], code), ("blocked_pending_run", 1))

    def test_timeout_retains_pending_claim(self):
        self.settings["providers"]["muse"]["timeout_seconds"] = 0.1
        self.settings["providers"]["muse"]["termination_grace_seconds"] = 0
        self.fake.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("timeout", 1))
        self.assertTrue(result["fallback_blocked_by_pending"])
        self.assertNotEqual(self.pending()[0]["status"], "resolved")
        self.assertFalse((self.state / "muse-quota.json").exists())

    def test_terminal_failure_and_recovered_error_then_success(self):
        self.emit([linked(), record("task.lifecycle.failed", {"kind": "task_lifecycle", "reason": "daily quota exhausted"}),
                   terminal()], 0)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertFalse((self.state / "muse-quota.json").exists())
        self.state = self.root / "failed_state"
        self.args.state_dir = str(self.state)
        self.emit([linked(), terminal("failed", "", "model failed")], 1)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertEqual(self.pending()[0]["status"], "resolved")

    def test_cancelled_and_incoherent_terminal(self):
        self.emit([linked(), terminal("cancelled", "", "Cancelled")], 1)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertEqual(self.pending()[0]["status"], "resolved")
        for terminal_state, exit_code in (("completed", 1), ("failed", 0)):
            with self.subTest(terminal_state=terminal_state, exit_code=exit_code):
                self.state = self.root / ("state_" + uuid.uuid4().hex)
                self.args.state_dir = str(self.state)
                self.emit([linked(), terminal(terminal_state)], exit_code)
                result, code = self.run_muse()
                self.assertEqual((result["status"], code), ("uncertain_exit", 1))
                self.assertEqual(self.pending()[0]["status"], "uncertain_exit")

    def test_terminal_quota_cache_expiry_and_local_status(self):
        self.emit([linked(), terminal("failed", "", "API error 403: Weekly usage limit reached. Resets in 1h. (quota_exhausted)")], 1)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("fallback_required", 20))
        self.assertEqual(result["fallback"], runner.luna_fallback())
        state = runner.read_json(self.state / "muse-quota.json")
        self.assertEqual((state["reason"], state["provenance"]), ("provider_reset", "provider_reset"))
        self.assertEqual(self.pending()[0]["status"], "resolved")
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            cached, code = self.run_muse()
            self.assertEqual((cached["status"], code), ("fallback_required", 20))
            status, status_code = runner.cli_quota_status(argparse.Namespace(provider="muse", config=str(self.config),
                                                                            state_dir=str(self.state), check_live=False))
        self.assertEqual((status["status"], status_code), ("fallback_required", 20))
        state["retry_at"] = 1
        runner.write_json(self.state / "muse-quota.json", state)
        self.emit([linked(), terminal()], 0)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))

    def test_non_subscription_errors_do_not_cache(self):
        reasons = ["API error 429: rate limit per minute (quota_exhausted)",
                   "API error 429: Weekly usage limit reached. Resets in 1h. (quota_exhausted)",
                   "API error 403: permission denied (quota_exhausted)",
                   "API error 403: Weekly usage limit reached. Resets in 1h. (invalid_request_error)",
                   "API error 400: invalid model (invalid_request_error)",
                   "API error 401: authentication required (auth_error)",
                   "quoted: Weekly usage limit reached. (quota_exhausted)"]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.state = self.root / ("state_" + uuid.uuid4().hex)
                self.args.state_dir = str(self.state)
                self.emit([linked(), terminal("failed", "", reason)], 1)
                result, code = self.run_muse()
                self.assertEqual((result["status"], code), ("provider_error", 1))
                self.assertFalse((self.state / "muse-quota.json").exists())

    def test_malformed_quota_state_fails_closed_without_launch(self):
        self.state.mkdir()
        (self.state / "muse-quota.json").write_text('{"retry_at":"bad"}', encoding="utf-8")
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("state_error", 1))
        self.assertEqual(result["fallback"], runner.luna_fallback())

    def test_manual_quota_set_preserves_longer_existing_block(self):
        self.settings["providers"]["muse"]["model"] = "cli-default"
        runner.write_json(self.config, self.settings)
        reset_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        args = argparse.Namespace(provider="muse", config=str(self.config), state_dir=str(self.state),
                                  reason="User-reported exhaustion", reset_at=reset_at)
        first, code = runner.cli_quota_set(args)
        self.assertEqual(code, 0)
        original = first["quota"]["retry_at"]
        args.reset_at = None
        second, code = runner.cli_quota_set(args)
        self.assertEqual(code, 0)
        self.assertEqual(second["quota"]["retry_at"], original)
        self.assertEqual(second["quota"]["model"], "cli-default")

    def test_manual_quota_set_redacts_environment_secret(self):
        runner.write_json(self.config, self.settings)
        secret = "manual-secret-reset-012345"
        args = argparse.Namespace(provider="muse", config=str(self.config), state_dir=str(self.state),
                                  reason="User limit: " + secret, reset_at=None)
        with mock.patch.dict(os.environ, {"META_API_KEY": secret}):
            result, code = runner.cli_quota_set(args)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, (self.state / "muse-quota.json").read_text(encoding="utf-8"))

    def test_native_missing_auth_is_established_setup_failure(self):
        self.emit([], 1, "missing meta credentials: run `muse login` or set META_API_KEY, or save credentials at C:/tmp/key")
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertEqual(self.pending()[0]["status"], "resolved")
        self.assertFalse((self.state / "muse-quota.json").exists())

    def test_child_environment_strips_muse_keys_for_all_providers(self):
        config = {"providers": {"muse": {"api_key_env": "CUSTOM_META_KEY"}},
                  "retired_secret_env_vars": ["OLD_KEY"]}
        values = {"META_API_KEY": "meta-secret", "MUSE_API_KEY": "muse-secret",
                  "META_MODEL_API_KEY": "model-secret", "CUSTOM_META_KEY": "custom-secret",
                  "OLD_KEY": "retired-secret", "TYPESAFE_API_KEY": "helper-secret"}
        with mock.patch.dict(os.environ, values):
            for provider in ("muse", "gemini", "claude"):
                with self.subTest(provider=provider):
                    child = runner.child_environment(config, provider)
                    self.assertTrue(values.keys().isdisjoint(child))
                    self.assertTrue(set(values).issubset(dict(runner.framework_secrets(config))))

    def test_secret_redaction_and_runtime_child_environment(self):
        secret = "meta-secret-test-value-012345"
        self.fake.write_text(
            "import json, os, sys\n" +
            "assert 'META_API_KEY' not in os.environ\n" +
            "print(" + repr(json.dumps(linked())) + ")\n" +
            "print(" + repr(json.dumps(terminal(text="Done " + secret))) + ")\n" +
            "print(" + repr("diagnostic " + secret) + ", file=sys.stderr)\n",
            encoding="utf-8")
        with mock.patch.dict(os.environ, {"META_API_KEY": secret}):
            result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertNotIn(secret, json.dumps(result))
        for path in Path(result["logs"]).iterdir():
            if path.is_file() and path.suffix in (".json", ".log", ".txt"):
                self.assertNotIn(secret, path.read_text(encoding="utf-8"), str(path))

    def test_quota_cache_never_retains_terminal_environment_secret(self):
        secret = "secret-reset-marker-012345"
        self.emit([linked(), terminal("failed", "", "API error 403: Weekly usage limit reached. Resets in " +
                                           secret + " (quota_exhausted)")], 1)
        with mock.patch.dict(os.environ, {"META_API_KEY": secret}):
            result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("fallback_required", 20))
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, (self.state / "muse-quota.json").read_text(encoding="utf-8"))
        for path in Path(result["logs"]).iterdir():
            if path.is_file() and path.suffix in (".json", ".log", ".txt"):
                self.assertNotIn(secret, path.read_text(encoding="utf-8"), str(path))

    def test_profile_resolution_matrix_and_immutability(self):
        import copy
        original = copy.deepcopy(self.settings)
        cases = [(None, "routine", "muse-wonder-1.0", "medium"),
                 ("routine", "routine", "muse-wonder-1.0", "medium"),
                 ("complex", "complex", "muse-wonder-full-1.0", "medium"),
                 ("escalation", "escalation", "muse-wonder-full-1.0", "high")]
        for requested, profile, model, effort in cases:
            with self.subTest(requested=requested):
                effective, meta = runner.resolve_muse_profile(self.settings, requested)
                self.assertEqual(meta, {"muse_profile": profile, "model": model, "effort": effort})
                self.assertEqual(effective["executable"], original["providers"]["muse"]["executable"])
                self.assertEqual(effective["timeout_seconds"], 3)
                self.assertNotIn("profiles", effective)
                command = runner.command_for("implement", effective, "unused", 3, self.root,
                                             provider_name="muse", output_dir=self.root)
                self.assertEqual(command[command.index("--model") + 1], model)
                self.assertEqual(command[command.index("--reasoning-effort") + 1], effort)
        self.assertEqual(self.settings, original)
        # Custom base + cli-default profile and partial override inherit base field.
        self.settings["providers"]["muse"]["model"] = "custom-base"
        self.settings["providers"]["muse"]["profiles"]["complex"] = {"model": "cli-default"}
        effective, meta = runner.resolve_muse_profile(self.settings, "complex")
        self.assertEqual((meta["model"], meta["effort"]), ("cli-default", "medium"))
        command = runner.command_for("implement", effective, "unused", 3, self.root,
                                     provider_name="muse", output_dir=self.root)
        self.assertNotIn("--model", command)

    def test_profile_execute_dry_run_and_metadata(self):
        for profile, model, effort in (("complex", "muse-wonder-full-1.0", "medium"),
                                       ("escalation", "muse-wonder-full-1.0", "high")):
            with self.subTest(profile=profile):
                self.state = self.root / ("state_" + uuid.uuid4().hex)
                self.args.state_dir = str(self.state)
                self.args.muse_profile = profile
                self.args.dry_run = True
                runner.write_json(self.config, self.settings)
                dry, code = runner.execute(self.args)
                self.assertEqual(code, 0)
                self.assertEqual((dry["muse_profile"], dry["model"], dry["effort"]), (profile, model, effort))
                self.assertEqual(dry["command"][dry["command"].index("--model") + 1], model)
                self.args.dry_run = False
                self.emit([linked(), terminal()])
                result, code = self.run_muse()
                self.assertEqual((result["status"], code), ("completed", 0))
                self.assertEqual((result["muse_profile"], result["model"], result["effort"]), (profile, model, effort))
                pending = self.pending()[0]
                self.assertEqual((pending["muse_profile"], pending["model"], pending["effort"]), (profile, model, effort))
                progress = runner.read_json(Path(result["logs"]) / "progress.json")
                self.assertEqual(progress["muse_profile"], profile)

    def test_missing_malformed_profile_rejected_without_launch(self):
        bad = []
        settings = json.loads(json.dumps(self.settings))
        del settings["providers"]["muse"]["profiles"]
        bad.append((settings, "complex"))
        settings = json.loads(json.dumps(self.settings))
        settings["providers"]["muse"]["profiles"] = []
        bad.append((settings, "routine"))
        settings = json.loads(json.dumps(self.settings))
        settings["providers"]["muse"]["profiles"]["complex"] = {"model": "", "effort": "medium"}
        bad.append((settings, "complex"))
        settings = json.loads(json.dumps(self.settings))
        settings["providers"]["muse"]["profiles"]["escalation"] = {"model": "x", "effort": "bogus"}
        bad.append((settings, "escalation"))
        settings = json.loads(json.dumps(self.settings))
        settings["providers"]["muse"]["profiles"]["complex"] = {"model": "x", "timeout_seconds": 1}
        bad.append((settings, "complex"))
        for candidate, profile in bad:
            with self.subTest(profile=profile, candidate=candidate["providers"]["muse"].get("profiles")):
                runner.write_json(self.config, candidate)
                before = self.config.read_bytes()
                self.args.muse_profile = profile
                with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("launched")):
                    with self.assertRaises(ValueError):
                        runner.execute(self.args)
                self.assertEqual(self.config.read_bytes(), before)
        # Legacy config without profiles still supports routine.
        legacy = json.loads(json.dumps(self.settings))
        del legacy["providers"]["muse"]["profiles"]
        runner.write_json(self.config, legacy)
        self.args.muse_profile = None
        self.emit([linked(), terminal()])
        result, code = runner.execute(self.args)
        self.assertEqual((result["status"], result["muse_profile"]), ("completed", "routine"))

    def test_profile_rejected_for_non_muse_and_review(self):
        self.settings["providers"]["gemini"] = {"executable": "agy", "model": "gemini-3.8-flash-medium"}
        self.settings["providers"]["claude"] = {"executable": "claude", "model": "claude-opus-5-5", "effort": "medium"}
        runner.write_json(self.config, self.settings)
        for role, provider in (("implement", "gemini"), ("review", "claude")):
            with self.subTest(role=role):
                args = argparse.Namespace(**{**vars(self.args), "role": role, "provider": provider,
                                             "muse_profile": "complex"})
                with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("launched")):
                    with self.assertRaises(ValueError):
                        runner.execute(args)
        with self.assertRaises(ValueError):
            runner.cli_quota_status(argparse.Namespace(provider="gemini", config=str(self.config),
                                                       state_dir=str(self.state), check_live=False,
                                                       muse_profile="complex"))
        with self.assertRaises(ValueError):
            runner.cli_quota_set(argparse.Namespace(provider="claude", config=str(self.config),
                                                    state_dir=str(self.state), reason="x", reset_at=None,
                                                    muse_profile="escalation"))
        # Review pin remains Claude-only via command_for.
        with self.assertRaises(ValueError):
            runner.command_for("review", self.settings["providers"]["muse"], "x", 3, self.root,
                               provider_name="muse")

    def test_profile_status_local_and_shared_quota(self):
        runner.write_json(self.config, self.settings)
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            status, code = runner.cli_quota_status(argparse.Namespace(
                provider="muse", config=str(self.config), state_dir=str(self.state),
                check_live=False, muse_profile="escalation"))
        self.assertEqual(code, 0)
        self.assertEqual((status["muse_profile"], status["model"], status["effort"]),
                         ("escalation", "muse-wonder-full-1.0", "high"))
        self.assertTrue(status["quota_path"].endswith("muse-quota.json"))
        # Full exhaustion records Full model in the one shared cache and blocks routine.
        self.args.muse_profile = "complex"
        self.emit([linked(), terminal("failed", "", "API error 403: Weekly usage limit reached. Resets in 1h. (quota_exhausted)")], 1)
        result, code = self.run_muse()
        self.assertEqual((result["status"], code), ("fallback_required", 20))
        state = runner.read_json(self.state / "muse-quota.json")
        self.assertEqual((state["model"], state["muse_profile"], state["effort"]),
                         ("muse-wonder-full-1.0", "complex", "medium"))
        self.args.muse_profile = None
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            blocked, code = self.run_muse()
        self.assertEqual((blocked["status"], blocked["muse_profile"]), ("fallback_required", "routine"))
        # Routine-recorded exhaustion (operator) blocks Full, same file.
        self.state = self.root / "quota2"
        self.args.state_dir = str(self.state)
        recorded, code = runner.cli_quota_set(argparse.Namespace(
            provider="muse", config=str(self.config), state_dir=str(self.state),
            reason="User-reported exhaustion", reset_at=None, muse_profile=None))
        self.assertEqual((recorded["quota"]["model"], recorded["quota"]["muse_profile"]),
                         ("muse-wonder-1.0", "routine"))
        self.args.muse_profile = "escalation"
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            blocked, code = self.run_muse()
        self.assertEqual(blocked["status"], "fallback_required")
        # Malformed shared state blocks Full too.
        self.state = self.root / "quota3"
        self.args.state_dir = str(self.state)
        self.state.mkdir()
        (self.state / "muse-quota.json").write_text('{"retry_at":"bad"}', encoding="utf-8")
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("no CLI call")):
            result, code = self.run_muse()
        self.assertEqual(result["status"], "state_error")

    def test_resolved_failure_then_escalation_and_uncertain_blocks(self):
        # Resolved routine terminal failure: explicit escalation may launch.
        self.emit([linked(), terminal("failed", "", "model failed")], 1)
        result, code = self.run_muse()
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(self.pending()[0]["status"], "resolved")
        self.args.muse_profile = "escalation"
        self.emit([linked(), terminal()])
        result, code = self.run_muse()
        self.assertEqual((result["status"], result["muse_profile"]), ("completed", "escalation"))
        # Uncertain routine run retains claim; escalation on same paths blocked, no launch.
        self.state = self.root / "uncertain_state"
        self.args.state_dir = str(self.state)
        self.args.muse_profile = None
        self.emit([linked()], 0)
        result, code = self.run_muse()
        self.assertEqual(result["status"], "uncertain_exit")
        self.args.muse_profile = "escalation"
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("launched")):
            blocked, code = self.run_muse()
        self.assertEqual(blocked["status"], "blocked_pending_run")

    def test_heartbeat_reports_profile(self):
        effective, _ = runner.resolve_muse_profile(self.settings, "escalation")
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        import time as _time
        data = runner.heartbeat(self.root, "muse", effective, process, "running", _time.time(), _time.monotonic())
        self.assertEqual((data["muse_profile"], data["model"], data["effort"]),
                         ("escalation", "muse-wonder-full-1.0", "high"))

    def test_windows_launcher_translation(self):
        folder = self.root / "launcher"
        folder.mkdir()
        cmd = folder / "muse.cmd"
        cmd.write_text("shim", encoding="utf-8")
        script = folder / ".muse-launcher.ps1"
        script.write_text("launcher", encoding="utf-8")
        with mock.patch.object(runner.shutil, "which", side_effect=lambda value: "powershell.exe" if value == "powershell" else None):
            actual = runner.direct_windows_muse_command([str(cmd), "exec"], platform_name="nt")
        self.assertEqual(actual[-3:], ["-File", str(script), "exec"])


if __name__ == "__main__":
    unittest.main()
