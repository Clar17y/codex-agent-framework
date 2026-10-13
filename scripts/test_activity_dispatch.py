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
import sys
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import provider_runner as runner


ROOT = Path(__file__).resolve().parent.parent


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
        self.assertEqual(len(self.config["activities"]), 15)
        for activity in self.config["activities"]:
            chain = runner.activity_routes(self.config, activity)
            self.assertEqual({self.config["profiles"][p]["provider"] for p in chain},
                             {"openai", "claude", "gemini", "muse"})
            for name in chain:
                provider_name, effective = runner.effective_activity_provider(self.config, name)
                self.assertEqual(effective["model"], self.config["profiles"][name]["model"])
                self.assertEqual(provider_name, self.config["profiles"][name]["provider"])
        self.assertEqual(self.config, original)

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

    def test_reader_capabilities_are_flags_not_plan_prompt(self):
        helper = self.root / ".gemini" / "config" / "agents" / "framework-reader.md"
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / "agents/framework-reader.md").read_bytes())
        for name in ("openai-sol", "claude-opus", "muse-full", "gemini-flash"):
            provider_name, provider = runner.effective_activity_provider(self.config, name)
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
        with mock.patch.object(runner, "direct_windows_codex_command", side_effect=lambda argv: argv), mock.patch.object(runner.subprocess, "Popen") as launch:
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
