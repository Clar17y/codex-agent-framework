import argparse
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import provider_runner as runner


class ProviderTests(unittest.TestCase):
    def setUp(self):
        # Scratch is retained for inspection; never delete repository scratch files.
        self.root = Path(__file__).resolve().parent.parent / ".llm-output" / str(uuid.uuid4())
        self.root.mkdir(parents=True)
        self.task = self.root / "task.json"
        runner.write_json(self.task, {"objective": "Test", "acceptance_criteria": ["Done"],
                                     "owned_paths": [], "validation": [], "instructions_files": []})
        self.fake = self.root / "fake.py"
        self.fake.write_text("print('{\"status\":\"SUCCESS\",\"response\":\"Done\"}')", encoding="utf-8")
        self.config = self.root / "config.json"
        self.settings = {"providers": {
            "gemini": {"executable": [sys.executable, str(self.fake)], "model": "gemini-3.8-flash-medium"},
            "claude": {"executable": [sys.executable, str(self.fake)], "model": "claude-opus-5"}},
            "timeout_seconds": 5}
        self.args = argparse.Namespace(role="implement", workspace=str(self.root), task_file=str(self.task),
                                       config=str(self.config), state_dir=str(self.root / "state"), dry_run=False)

    def run_provider(self):
        runner.write_json(self.config, self.settings)
        return runner.execute(self.args)

    @property
    def pending_path(self):
        records = list((runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending").glob("*.json"))
        return records[0] if records else runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending.json"

    def task_for(self, name, owned_paths):
        path = self.root / (name + ".json")
        runner.write_json(path, {"objective": name, "acceptance_criteria": ["Done"],
                                 "owned_paths": owned_paths, "validation": [], "instructions_files": []})
        return path

    def require_london_zone(self):
        try:
            ZoneInfo("Europe/London")
        except ZoneInfoNotFoundError:
            self.skipTest("IANA timezone data unavailable; cooldown behavior is tested separately")

    def test_parallel_workspaces_for_both_providers(self):
        for role in ("implement", "review"):
            with self.subTest(role=role):
                workspaces = [self.root / (role + str(i)) for i in range(2)]
                for workspace in workspaces:
                    workspace.mkdir()
                markers = [workspace / "started" for workspace in workspaces]
                payload = ({"status": "SUCCESS", "response": "Done"} if role == "implement"
                           else {"type": "result", "subtype": "success", "is_error": False})
                self.fake.write_text(
                    "from pathlib import Path\nimport time\n"
                    "Path('started').touch()\n"
                    f"markers = {list(map(str, markers))!r}\n"
                    "deadline = time.monotonic() + 4\n"
                    "while not all(Path(p).exists() for p in markers):\n"
                    "    assert time.monotonic() < deadline, 'providers did not overlap'\n"
                    "    time.sleep(0.02)\n"
                    f"print({json.dumps(payload)!r})\n", encoding="utf-8")
                runner.write_json(self.config, self.settings)
                arguments = [argparse.Namespace(**{**vars(self.args), "role": role, "workspace": str(w)})
                             for w in workspaces]
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(runner.execute, arguments))
                self.assertEqual([(r["status"], code) for r, code in results],
                                 [("completed", 0), ("completed", 0)])

    def test_same_workspace_disjoint_claims_run_concurrently(self):
        self.fake.write_text(
            "from pathlib import Path\nimport os, time\n"
            "Path('started-'+str(os.getpid())).touch()\n"
            "deadline=time.monotonic()+4\n"
            "while len(list(Path('.').glob('started-*'))) < 2:\n"
            "    assert time.monotonic() < deadline, 'claims did not run concurrently'\n"
            "    time.sleep(.02)\n"
            "print('{\"status\":\"SUCCESS\",\"response\":\"Done\"}')", encoding="utf-8")
        runner.write_json(self.config, self.settings)
        first = argparse.Namespace(**{**vars(self.args), "task_file": str(self.task_for("one", ["src/a.py"]))})
        second = argparse.Namespace(**{**vars(self.args), "task_file": str(self.task_for("two", ["src/b.py"]))})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(runner.execute, [first, second]))
        self.assertEqual([(result["status"], code) for result, code in results], [("completed", 0), ("completed", 0)])

    def test_overlapping_and_directory_claims_block(self):
        state = runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending"
        state.mkdir(parents=True)
        runner.write_json(state / "running.json", {"status": "running", "workspace": str(self.root),
                                                    "owned_paths": ["src"], "logs": "old"})
        for paths in (["src/a.py"], ["src"]):
            with self.subTest(paths=paths):
                self.args.task_file = str(self.task_for("overlap" + str(len(paths)), paths))
                result, code = self.run_provider()
                self.assertEqual((result["status"], code), ("blocked_pending_run", 1))

    def test_unresolved_file_claim_allows_disjoint_file_and_blocks_same_file(self):
        state = runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending"
        state.mkdir(parents=True)
        runner.write_json(state / "running.json", {"status": "running", "workspace": str(self.root),
                                                    "owned_paths": ["src/a.py"], "logs": "old"})
        self.args.task_file = str(self.task_for("disjoint", ["src/b.py"]))
        self.assertEqual(self.run_provider()[1], 0)
        self.args.task_file = str(self.task_for("same", ["src/a.py"]))
        self.assertEqual(self.run_provider()[0]["status"], "blocked_pending_run")

    def test_active_same_file_claim_blocks_second_run(self):
        self.fake.write_text(
            "from pathlib import Path\nimport time\nPath('started').touch()\n"
            "deadline = time.monotonic() + 4\n"
            "while not Path('release').exists():\n"
            "    assert time.monotonic() < deadline\n"
            "    time.sleep(.01)\n"
            "print('{\"status\":\"SUCCESS\",\"response\":\"Done\"}')", encoding="utf-8")
        runner.write_json(self.config, self.settings)
        task = self.task_for("same-active", ["src/a.py"])
        first = argparse.Namespace(**{**vars(self.args), "task_file": str(task)})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(runner.execute, first)
            deadline = time.monotonic() + 3
            while not (self.root / "started").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            second, code = runner.execute(first)
            (self.root / "release").touch()
            self.assertEqual((second["status"], code), ("blocked_pending_run", 1))
            self.assertEqual(running.result()[1], 0)

    def test_blocked_evidence_does_not_hold_claim_lock(self):
        state = runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending"
        runner.write_json(state / "running.json", {"status": "running", "workspace": str(self.root),
                                                   "owned_paths": ["src/a.py"]})
        self.settings["lock_timeout_seconds"] = .1
        runner.write_json(self.config, self.settings)
        blocked_args = argparse.Namespace(**{**vars(self.args), "task_file": str(self.task_for("blocked", ["src/a.py"]))})
        free_args = argparse.Namespace(**{**vars(self.args), "task_file": str(self.task_for("free", ["src/b.py"]))})
        collecting, release = threading.Event(), threading.Event()
        real_finish = runner.finish

        def slow_finish(result, *args):
            if result.get("status") == "blocked_pending_run":
                collecting.set()
                self.assertTrue(release.wait(5))
            return real_finish(result, *args)

        with mock.patch.object(runner, "finish", side_effect=slow_finish):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                blocked = pool.submit(runner.execute, blocked_args)
                try:
                    self.assertTrue(collecting.wait(3))
                    result, code = runner.execute(free_args)
                    self.assertEqual((result["status"], code), ("completed", 0))
                finally:
                    release.set()
                self.assertEqual(blocked.result()[0]["status"], "blocked_pending_run")

    def test_whole_workspace_claim_conflicts_with_parent_directory_both_directions(self):
        parent = self.root.parent
        self.assertTrue(runner.claims_overlap(self.root, [], parent, [str(parent)]))
        self.assertTrue(runner.claims_overlap(parent, [str(parent)], self.root, []))

    def test_glob_owned_path_rejected(self):
        for path in ("src/*.py", "src/?.py"):
            with self.subTest(path=path):
                self.args.task_file = str(self.task_for("glob", [path]))
                with self.assertRaisesRegex(ValueError, "literal paths"):
                    self.run_provider()

    def test_literal_bracketed_route_path_supported(self):
        self.args.task_file = str(self.task_for("route", ["app/[id]/page.tsx"]))
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))

    def test_nested_workspace_same_file_claim_blocks(self):
        nested = self.root / "nested"
        nested.mkdir()
        state = runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending"
        state.mkdir(parents=True)
        runner.write_json(state / "running.json", {"status": "running", "workspace": str(self.root),
                                                    "owned_paths": ["nested/file.py"], "logs": "old"})
        nested_task = self.task_for("nested", ["file.py"])
        runner.write_json(self.config, self.settings)
        result, code = runner.execute(argparse.Namespace(**{**vars(self.args), "workspace": str(nested), "task_file": str(nested_task)}))
        self.assertEqual((result["status"], code), ("blocked_pending_run", 1))

    def test_same_workspace_lock_blocks_second_run(self):
        self.settings["lock_timeout_seconds"] = 0.1
        with runner.provider_lock(Path(self.args.state_dir), 1):
            result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("setup_error", 1))
        self.assertIn("lock is busy", result["error"])
        self.assertFalse(self.pending_path.exists())

    def test_claude_same_workspace_ignores_gemini_gates(self):
        self.args.role = "review"
        legacy = Path(self.args.state_dir) / "gemini-pending.json"
        legacy.parent.mkdir(parents=True)
        runner.write_json(legacy, {"status": "running"})
        self.fake.write_text(
            "from pathlib import Path\nimport time, uuid\n"
            "Path(str(uuid.uuid4()) + '.started').touch()\n"
            "deadline = time.monotonic() + 4\n"
            "while len(list(Path('.').glob('*.started'))) < 2:\n"
            "    assert time.monotonic() < deadline, 'reviews did not overlap'\n"
            "    time.sleep(0.02)\n"
            "print('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false}')\n",
            encoding="utf-8")
        runner.write_json(self.config, self.settings)
        with runner.provider_lock(Path(self.args.state_dir), 1):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(runner.execute, [self.args, self.args]))
        self.assertEqual([(r["status"], code) for r, code in results],
                         [("completed", 0), ("completed", 0)])

    def test_workspace_identity_normalizes_alias(self):
        self.assertEqual(runner.workspace_state_dir(Path(self.args.state_dir), self.root),
                         runner.workspace_state_dir(Path(self.args.state_dir), self.root / "child" / ".."))

    def test_unresolved_other_workspace_does_not_block(self):
        other = self.root.parent / ("other-" + uuid.uuid4().hex)
        pending = runner.workspace_state_dir(Path(self.args.state_dir), other) / "gemini-pending.json"
        pending.parent.mkdir(parents=True)
        runner.write_json(pending, {"status": "running", "workspace": str(other)})
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertEqual(runner.read_json(pending)["status"], "running")

    def test_legacy_pending_scoped_and_preserved(self):
        legacy = Path(self.args.state_dir) / "gemini-pending.json"
        legacy.parent.mkdir(parents=True)
        for owner, expected in ((self.root.parent / ("other-" + uuid.uuid4().hex), "completed"), (self.root, "blocked_pending_run")):
            record = {"status": "running", "logs": str(owner / ".llm-output" / "agent-framework" / "old-run")}
            runner.write_json(legacy, record)
            result, code = self.run_provider()
            self.assertEqual(result["status"], expected)
            self.assertEqual(runner.read_json(legacy), record)
        for unknown in ({"status": "running"}, {}):
            runner.write_json(legacy, unknown)
            result, code = self.run_provider()
            self.assertEqual(result["status"], "blocked_pending_run")

    def test_quota_cache_shared_across_workspaces(self):
        self.fake_result({"error": {"code": "QUOTA_EXHAUSTED", "message": "Daily quota exhausted"}})
        self.assertEqual(self.run_provider()[1], 20)
        other = self.root / "other"
        other.mkdir()
        self.args.workspace = str(other)
        self.settings["providers"]["gemini"]["executable"] = "not-a-real-program"
        result, code = self.run_provider()
        self.assertEqual(code, 20)
        self.assertTrue(result["cached"])

    def fake_result(self, payload, code=1):
        self.fake.write_text(f"import sys\nprint({json.dumps(json.dumps(payload))})\nsys.exit({code})", encoding="utf-8")

    def test_success_and_evidence(self):
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertTrue(Path(result["evidence"]["status"]).exists())

    def test_actual_agy_denied_read_is_not_success(self):
        self.fake_result({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_file", "display_name": "ViewFile"}]}, 0)
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("permission_denied", 1))
        self.assertFalse((self.root / "state" / "gemini-quota.json").exists())
        self.args.dry_run = True
        result, code = self.run_provider()
        self.assertEqual(result["command"][result["command"].index("--add-dir") + 1], str(self.root))
        self.assertIn("--dangerously-skip-permissions", result["command"])
        self.assertNotIn("--sandbox", result["command"])

    def test_claude_permission_denial_is_not_success(self):
        self.args.role = "review"
        self.fake_result({"type": "result", "subtype": "success", "is_error": False, "permission_denials": [{"tool_name": "Read"}]}, 0)
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("permission_denied", 1))

    def test_quota_and_cached_fallback_without_spawn(self):
        self.fake_result({"error": {"code": "QUOTA_EXHAUSTED", "message": "Daily quota exhausted"}})
        first, code = self.run_provider()
        self.assertEqual(code, 20)
        self.assertFalse(first["cached"])
        self.assertEqual(first["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.settings["providers"]["gemini"]["executable"] = "not-a-real-program"
        second, code = self.run_provider()
        self.assertEqual(code, 20)
        self.assertTrue(second["cached"])

    def test_plain_terminal_quota_fallback(self):
        self.fake.write_text("import sys\nprint('Error: daily usage limit reached',file=sys.stderr)\nsys.exit(1)", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("fallback_required", 20))
        self.assertEqual(result["fallback"]["effort"], "medium")

    def test_transient_stderr_not_fallback(self):
        self.fake.write_text("import sys\nprint('Error: 429 Too many requests',file=sys.stderr)\nsys.exit(1)", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertFalse((self.root / "state" / "gemini-quota.json").exists())

    def test_plain_error_classifier_rejects_ambiguous_output(self):
        self.assertIsNone(runner.plain_terminal_error("Error: quota exhausted", 0))
        self.assertIsNone(runner.plain_terminal_error("Agent says: Error: quota exhausted", 1))
        self.assertIsNone(runner.plain_terminal_error("Session continuing\nError: quota exhausted", 1))
        self.assertIsNone(runner.plain_terminal_error("Error: quota exceeded", 1))
        self.assertIsNone(runner.plain_terminal_error("Error: quota exhausted", 1))

    def test_rate_quota_message_never_falls_back(self):
        for error_code in ("RESOURCE_EXHAUSTED", "429", "QUOTA_EXCEEDED", "QUOTA_EXHAUSTED"):
            with self.subTest(error_code=error_code):
                self.fake_result({"error": {"code": error_code, "message": "quota exceeded for requests per minute"}})
                result, code = self.run_provider()
                self.assertEqual((result["status"], code), ("provider_error", 1))
                self.assertFalse((self.root / "state" / "gemini-quota.json").exists())

    def test_success_with_ancillary_quota_error_does_not_fallback(self):
        self.fake.write_text("import sys\nprint('{\"status\":\"SUCCESS\",\"response\":\"Done\"}')\nprint('{\"error\":{\"code\":\"QUOTA_EXHAUSTED\",\"message\":\"daily quota exhausted\"}}',file=sys.stderr)", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        self.assertFalse((self.root / "state" / "gemini-quota.json").exists())

    def test_auth_invalid_model_throttling_do_not_fallback(self):
        for payload in ({"is_error": True, "subtype": "success", "result": "Not logged in · Please run /login"},
                        {"error": {"code": "MODEL_NOT_FOUND", "message": "Invalid model"}},
                        {"error": {"code": "RESOURCE_EXHAUSTED", "message": "Too many requests"}}):
            with self.subTest(payload=payload):
                self.fake_result(payload)
                result, code = self.run_provider()
                self.assertEqual((result["status"], code), ("provider_error", 1))
                self.assertFalse((self.root / "state" / "gemini-quota.json").exists())

    def test_success_text_cannot_poison_quota(self):
        self.fake_result({"status": "SUCCESS", "response": "quota exhausted QUOTA_EXHAUSTED"}, 0)
        result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")

    def test_agy_failure_envelope_with_zero_exit(self):
        self.fake_result({"status": "ERROR", "response": "Invalid model"}, 0)
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("provider_error", 1))

    def test_timeout(self):
        self.fake.write_text("import time\ntime.sleep(30)", encoding="utf-8")
        self.settings["timeout_seconds"] = 0.2
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("timeout", 1))
        result, code = self.run_provider()
        self.assertEqual(result["status"], "blocked_pending_run")

    def test_post_launch_state_failure_stops_child_and_blocks_retry(self):
        self.fake.write_text("import time\ntime.sleep(30)", encoding="utf-8")
        spawned = []
        real_popen, real_write = runner.subprocess.Popen, runner.write_json

        def track_spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if str(self.fake) in args[0]:
                spawned.append(process)
            return process

        def fail_running_write(path, data):
            if Path(path).parent.name == "gemini-pending" and data.get("status") == "running":
                raise PermissionError("Injected post-launch write failure")
            return real_write(path, data)

        with mock.patch.object(runner.subprocess, "Popen", side_effect=track_spawn), mock.patch.object(runner, "write_json", side_effect=fail_running_write):
            result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("setup_error", 1))
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(runner.read_json(self.pending_path)["status"], "launching")
        result, code = self.run_provider()
        self.assertEqual(result["status"], "blocked_pending_run")

    def test_launch_failure_resolves_pending(self):
        self.settings["providers"]["gemini"]["executable"] = "not-a-real-program"
        result, code = self.run_provider()
        self.assertEqual(result["status"], "setup_error")
        self.assertEqual(runner.read_json(self.pending_path)["status"], "resolved")

    def test_malformed_success_rejected(self):
        self.fake.write_text("print('')", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("provider_error", 1))
        result, code = self.run_provider()
        self.assertEqual(result["status"], "blocked_pending_run")

    def test_long_prompt_written_to_file(self):
        task = runner.read_json(self.task)
        task["objective"] = "x" * 40000
        runner.write_json(self.task, task)
        self.fake.write_text("import sys\nassert max(map(len,sys.argv)) < 10000\nprint('{\"status\":\"SUCCESS\",\"response\":\"Done\"}')", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertGreater((Path(result["logs"]) / "prompt.txt").stat().st_size, 40000)

    def test_review_restricted_tools_and_pinned_model(self):
        self.args.role = "review"
        self.args.dry_run = True
        result, code = self.run_provider()
        command = result["command"]
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--restricted", command)
        self.assertNotIn("--permission-mode", command)
        self.assertNotIn("--permission-prompts", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--fallback-model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-5")
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        self.assertFalse((self.root / "state").exists())
        for forbidden in ("sonnet", "fable", "opus"):
            self.settings["providers"]["claude"]["model"] = forbidden
            with self.assertRaises(ValueError):
                self.run_provider()

    def test_instruction_escape_rejected(self):
        task = runner.read_json(self.task)
        task["instructions_files"] = ["../outside.md"]
        runner.write_json(self.task, task)
        with self.assertRaises(ValueError):
            self.run_provider()

    def test_reset_timestamp_and_probe_semantics(self):
        stamp, reason = runner.retry_at({"reset_at": "2099-01-01T00:00:00Z"}, 3600)
        self.assertEqual(reason, "provider_reset")
        stamp, reason = runner.retry_at({"reset_at": "invalid"}, 3600)
        self.assertEqual(reason, "probe_cooldown")

    def test_gemini_individual_quota_duration_is_provider_reset(self):
        observed = 1_000_000
        stamp, reason = runner.retry_at({"message": "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 47h3m1s."}, 3600, observed)
        self.assertEqual(reason, "provider_reset")
        self.assertEqual(stamp, observed + 47 * 3600 + 3 * 60 + 1)
        self.assertEqual(runner.quota_error([{"message": "error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 47h3m1s."}], "gemini")["message"].startswith("error:"), True)

    def test_claude_dated_reset_and_explicit_matching(self):
        self.require_london_zone()
        observed = runner.dt.datetime(2026, 9, 11, 15, 0, tzinfo=runner.UTC).timestamp()
        stamp, reason = runner.retry_at({"message": "You've hit your weekly limit · resets Sep 13, 2am (Europe/London)"}, 3600, observed)
        self.assertEqual(reason, "provider_reset")
        self.assertEqual(stamp, runner.dt.datetime(2026, 9, 13, 1, 0, tzinfo=runner.UTC).timestamp())
        self.assertIsNotNone(runner.quota_error([{"message": "You've hit your Opus limit"}], "claude"))
        self.assertIsNone(runner.quota_error([{"message": "The weekly limit is discussed in the task prose"}], "claude"))

    def test_past_or_ambiguous_human_reset_uses_cooldown_without_year_rollover(self):
        observed = runner.dt.datetime(2026, 9, 14, 15, 0, tzinfo=runner.UTC).timestamp()
        for message in (
                "You've hit your weekly limit · resets Sep 13, 2am (Europe/London)",
                "You've hit your weekly limit · resets Sunday Sep 13, 2am (Europe/London)",
                "You've hit your weekly limit · resets Oct 25, 1:30am (Europe/London)"):
            with self.subTest(message=message):
                stamp, reason = runner.retry_at({"message": message}, 3600, observed)
                self.assertEqual((stamp, reason), (observed + 3600, "probe_cooldown"))

    def test_invalid_or_conflicting_reset_data_uses_cooldown(self):
        observed = runner.dt.datetime(2026, 9, 11, 15, 0, tzinfo=runner.UTC).timestamp()
        for error in (
                {"reset_at": float("inf")}, {"reset_at": True}, {"reset_at": 1e100},
                {"message": "resets Sep 13, 25am (Europe/London)"},
                {"message": "resets Sep 13, 0pm (Europe/London)"},
                {"message": "resets Sep 13, 2:99am (Europe/London)"},
                {"message": "resets Monday Sep 13, 2am (Europe/London)"},
                {"message": "resets Sep 13, 2am (No/Such_Zone)"},
                {"message": "resets in 2h then maybe later"}):
            with self.subTest(error=error):
                stamp, reason = runner.retry_at(error, 3600, observed)
                self.assertEqual((stamp, reason), (observed + 3600, "probe_cooldown"))

    def test_invalid_state_and_finite_retry_are_rejected_without_overwrite(self):
        state_path = self.root / "state" / "gemini-quota.json"
        state_path.parent.mkdir()
        state_path.write_text('{"retry_at": NaN}', encoding="utf-8")
        before = state_path.read_text()
        with self.assertRaises(ValueError):
            runner.quota_record("gemini", {"message": "Daily quota exhausted"}, self.root / "state", "operator", self.settings)
        self.assertEqual(state_path.read_text(), before)
        for value in (float("nan"), float("inf"), 0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.retry_at({}, value)
        self.settings["quota_probe_seconds"] = True
        with self.assertRaisesRegex(ValueError, "finite positive"):
            runner.quota_record("gemini", {"message": "Daily quota exhausted"}, self.root / "other-state", "operator", self.settings)

    def test_claude_limit_result_falls_back_and_sonnet_only_does_not(self):
        self.args.role = "review"
        self.fake_result({"type": "result", "is_error": True, "result": "You've hit your limit · resets 3pm (Europe/London)"})
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("fallback_required", 20))
        self.assertTrue((self.root / "state" / "claude-quota.json").exists())
        self.args.workspace = str(self.root / "sonnet")
        Path(self.args.workspace).mkdir()
        self.args.state_dir = str(self.root / "sonnet-state")
        self.fake_result({"type": "result", "is_error": True, "result": "You've hit your Sonnet limit"})
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("provider_error", 1))

    def test_cli_quota_set_and_status_without_task(self):
        runner.write_json(self.config, self.settings)
        command = [sys.executable, str(Path(runner.__file__)), "quota-set", "--provider", "gemini", "--config", str(self.config), "--state-dir", str(self.root / "state"), "--reason", "operator observed exhaustion"]
        recorded = json.loads(subprocess.check_output(command, text=True))
        self.assertEqual(recorded["status"], "recorded")
        command = [sys.executable, str(Path(runner.__file__)), "status", "--provider", "gemini", "--config", str(self.config), "--state-dir", str(self.root / "state")]
        completed = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 20)
        status = json.loads(completed.stdout)
        self.assertEqual(status["status"], "fallback_required")
        self.assertEqual(status["available_to_try"], 0)
        self.assertEqual(status["cached_evidence"]["provenance"], "operator_report")

    def test_status_aggregate_error_wins_over_other_provider_block(self):
        runner.write_json(self.config, self.settings)
        state = self.root / "state"
        state.mkdir()
        (state / "gemini-quota.json").write_text('{"retry_at": NaN}', encoding="utf-8")
        runner.write_json(state / "claude-quota.json", {"retry_at": time.time() + 3600})
        command = [sys.executable, str(Path(runner.__file__)), "status", "--config", str(self.config), "--state-dir", str(state)]
        completed = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertEqual(report["providers"]["gemini"]["status"], "state_error")
        self.assertEqual(report["providers"]["claude"]["status"], "fallback_required")

    def test_status_bad_config_without_selected_provider_has_no_invented_fallback(self):
        bad_config = self.root / "bad-config.json"
        bad_config.write_text("{", encoding="utf-8")
        command = [sys.executable, str(Path(runner.__file__)), "status", "--config", str(bad_config)]
        completed = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("fallback", json.loads(completed.stdout))

    def test_both_provider_caches_prevent_spawns_and_are_isolated(self):
        for role, payload, filename in (
                ("implement", {"error": {"code": "QUOTA_EXHAUSTED", "message": "Daily quota exhausted"}}, "gemini-quota.json"),
                ("review", {"type": "result", "is_error": True, "result": "You've hit your weekly limit"}, "claude-quota.json")):
            with self.subTest(role=role):
                self.args.role = role
                self.fake_result(payload)
                self.assertEqual(self.run_provider()[1], 20)
                self.fake.write_text("raise SystemExit('cached provider spawned')", encoding="utf-8")
                other = self.root / (role + "-other")
                other.mkdir()
                self.args.workspace = str(other)
                result, code = self.run_provider()
                self.assertEqual((result["status"], code), ("fallback_required", 20))
                self.assertTrue((self.root / "state" / filename).exists())
        self.assertTrue((self.root / "state" / "gemini-quota.json").exists())
        self.assertTrue((self.root / "state" / "claude-quota.json").exists())

    def test_expired_cache_allows_attempt_and_concurrent_records_keep_longest_block(self):
        state_dir = self.root / "state"
        runner.write_json(state_dir / "gemini-quota.json", {"retry_at": time.time() - 1})
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))
        observed = time.time()
        with mock.patch.object(runner.time, "time", return_value=observed):
            shorter = runner.quota_record("gemini", {"message": "Daily quota exhausted"}, state_dir, "operator", self.settings)
            self.settings["quota_probe_seconds"] = 7200
            longer = runner.quota_record("gemini", {"message": "Daily quota exhausted"}, state_dir, "operator", self.settings)
        self.assertGreater(longer["retry_at"], shorter["retry_at"])
        self.settings["quota_probe_seconds"] = 3600
        with mock.patch.object(runner.time, "time", return_value=observed):
            retained = runner.quota_record("gemini", {"message": "Daily quota exhausted"}, state_dir, "operator", self.settings)
        self.assertEqual(retained["retry_at"], longer["retry_at"])
        self.assertEqual(runner.read_json(state_dir / "gemini-quota.json")["retry_at"], longer["retry_at"])

    def test_concurrent_exhaustions_leave_complete_longest_quota_record(self):
        first, second = self.root / "quota-one", self.root / "quota-two"
        first.mkdir()
        second.mkdir()
        later = (runner.dt.datetime.now(runner.UTC) + runner.dt.timedelta(hours=2)).isoformat()
        self.fake.write_text(
            "from pathlib import Path\nimport json\n"
            f"reset = {later!r} if Path.cwd().name == 'quota-two' else None\n"
            "error = {'code':'QUOTA_EXHAUSTED', 'message':'Daily quota exhausted'}\n"
            "if reset: error['reset_at'] = reset\nprint(json.dumps({'error': error}))", encoding="utf-8")
        runner.write_json(self.config, self.settings)
        arguments = [argparse.Namespace(**{**vars(self.args), "workspace": str(workspace)})
                     for workspace in (first, second)]
        # Both requests must be in flight before either records exhaustion.
        # Otherwise correctly skipping the second request is a valid outcome.
        both_finished = threading.Barrier(2)
        classify = runner.quota_error
        def classify_together(*args):
            both_finished.wait(timeout=5)
            return classify(*args)
        with mock.patch.object(runner, "quota_error", side_effect=classify_together):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(runner.execute, arguments))
        self.assertEqual([code for _, code in results], [20, 20])
        state = runner.read_json(self.root / "state" / "gemini-quota.json")
        self.assertGreaterEqual(state["retry_at"], runner.dt.datetime.fromisoformat(later).timestamp())
        self.assertEqual(state["reason"], "provider_reset")

    def test_reset_boundary_never_rolls_into_next_year(self):
        self.require_london_zone()
        reset = runner.dt.datetime(2026, 9, 13, 1, tzinfo=runner.UTC).timestamp()
        error = {"message": "You've hit your weekly limit · resets Sep 13, 2am (Europe/London)"}
        self.assertEqual(runner.retry_at(error, 3600, reset - 1), (reset, "provider_reset"))
        for observed in (reset, reset + 1):
            self.assertEqual(runner.retry_at(error, 3600, observed), (observed + 3600, "probe_cooldown"))

    def test_missing_timezone_data_uses_probe_and_offset_iso_still_works(self):
        observed = runner.dt.datetime(2026, 9, 11, 15, tzinfo=runner.UTC).timestamp()
        with mock.patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing")):
            self.assertEqual(runner.retry_at({"message": "resets Sep 13, 2am (Europe/London)"}, 3600, observed),
                             (observed + 3600, "probe_cooldown"))
            self.assertEqual(runner.retry_at({"reset_at": "2026-09-13T02:00:00+01:00"}, 3600, observed),
                             (runner.dt.datetime(2026, 9, 13, 1, tzinfo=runner.UTC).timestamp(), "provider_reset"))

    def test_bad_provider_reset_still_records_confirmed_exhaustion(self):
        for index, reset in enumerate((float("inf"), float("nan"), 1e100, True)):
            with self.subTest(reset=reset):
                self.args.state_dir = str(self.root / f"invalid-reset-{index}")
                self.fake_result({"error": {"code": "QUOTA_EXHAUSTED", "message": "Daily quota exhausted", "reset_at": reset}})
                result, code = self.run_provider()
                self.assertEqual((result["status"], code), ("fallback_required", 20))
                self.assertEqual(result["quota"]["reason"], "probe_cooldown")
                self.assertIsNone(result["quota"]["reset_at"])
                self.assertAlmostEqual(result["quota"]["retry_at"] - runner.dt.datetime.fromisoformat(result["quota"]["observed_at"]).timestamp(), 3600, delta=0.001)

    def test_status_is_read_only_and_never_starts_a_process(self):
        runner.write_json(self.config, self.settings)
        args = argparse.Namespace(provider=None, config=str(self.config), state_dir=str(self.root / "missing-state"))
        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("status launched a process")), \
                mock.patch.object(runner, "write_json", side_effect=AssertionError("status wrote state")):
            result, code = runner.cli_quota_status(args)
        self.assertEqual(code, 0)
        self.assertEqual({item["status"] for item in result["providers"].values()}, {"available_to_try"})
        self.assertFalse(Path(args.state_dir).exists())

    def test_unreadable_quota_is_not_treated_as_missing(self):
        with mock.patch.object(runner, "read_json", side_effect=PermissionError("denied")):
            state, error = runner.quota_state(self.root / "state", "claude")
        self.assertIsNone(state)
        self.assertIn("denied", error)

    def test_error_prose_quoting_limits_does_not_poison_cache(self):
        for provider, message in (
                ("claude", "Unable to parse the example: You've hit your weekly limit"),
                ("gemini", "Test failed while checking Individual quota reached"),
                ("claude", "The task mentions daily quota exhausted but login failed")):
            self.assertIsNone(runner.quota_error([{"message": message}], provider))

    def test_in_flight_success_preserves_a_newer_exhaustion(self):
        success = runner.successful_response
        for role, provider, payload in (
                ("implement", "gemini", {"status": "SUCCESS", "response": "Done"}),
                ("review", "claude", {"type": "result", "subtype": "success", "is_error": False})):
            self.args.role = role
            self.args.state_dir = str(self.root / f"in-flight-{provider}")
            path = Path(self.args.state_dir) / f"{provider}-quota.json"
            record = {"retry_at": time.time() + 7200, "error": "Another request exhausted quota"}
            self.fake_result(payload, code=0)
            def observe_success(text, selected_role):
                valid = success(text, selected_role)
                if valid:
                    runner.write_json(path, record)
                return valid
            with mock.patch.object(runner, "successful_response", side_effect=observe_success):
                self.assertEqual(self.run_provider()[1], 0)
            self.assertEqual(runner.read_json(path), record)

    def test_manual_reset_requires_future_offset(self):
        runner.write_json(self.config, self.settings)
        args = argparse.Namespace(provider="claude", config=str(self.config), state_dir=str(self.root / "state"),
                                  reason="User reported exhaustion", reset_at=None)
        for stamp in ("invalid", "2099-01-01T12:00:00", "2000-01-01T12:00:00Z"):
            args.reset_at = stamp
            with self.assertRaises(ValueError):
                runner.cli_quota_set(args)
        self.assertFalse(Path(args.state_dir).exists())
        args.reset_at = "2099-01-01T12:00:00+01:00"
        result, code = runner.cli_quota_set(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["quota"]["reset_at"], "2099-01-01T11:00:00Z")
        self.assertEqual(result["quota"]["provenance"], "operator_report")

    def test_claude_omitted_legacy_config_effort_defaults_to_medium(self):
        self.args.role = "review"
        self.args.dry_run = True
        self.assertNotIn("effort", self.settings["providers"]["claude"])
        result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertEqual(result["review_effort"], "medium")
        self.assertIsNone(result["review_reason"])
        command = result["command"]
        self.assertEqual(command[command.index("--effort") + 1], "medium")

    def test_claude_configured_effort_enforced_medium(self):
        self.args.role = "review"
        self.args.dry_run = True
        self.settings["providers"]["claude"]["effort"] = "medium"
        result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertEqual(result["review_effort"], "medium")
        for invalid_effort in ("high", "low", "xhigh", "max"):
            with self.subTest(invalid_effort=invalid_effort):
                self.settings["providers"]["claude"]["effort"] = invalid_effort
                with self.assertRaises(ValueError):
                    self.run_provider()

    def test_claude_review_explicit_high_effort_with_reason(self):
        self.args.role = "review"
        self.args.dry_run = True
        self.args.review_effort = "high"
        self.args.review_reason = "Coupled contracts and state concurrency"
        result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertEqual(result["review_effort"], "high")
        self.assertEqual(result["review_reason"], "Coupled contracts and state concurrency")
        command = result["command"]
        self.assertEqual(command[command.index("--effort") + 1], "high")

    def test_claude_review_high_effort_missing_reason_rejected(self):
        self.args.role = "review"
        self.args.dry_run = True
        self.args.review_effort = "high"
        for empty_reason in (None, "", "   ", "\t\n"):
            with self.subTest(empty_reason=empty_reason):
                self.args.review_reason = empty_reason
                with self.assertRaises(ValueError):
                    self.run_provider()

    def test_implement_role_rejects_review_effort_and_reason_override(self):
        self.args.role = "implement"
        for kwargs in ({"review_effort": "medium"}, {"review_effort": "high"}, {"review_reason": "Any reason"}):
            with self.subTest(kwargs=kwargs):
                self.args = argparse.Namespace(role="implement", workspace=str(self.root), task_file=str(self.task),
                                               config=str(self.config), state_dir=str(self.root / "state"), dry_run=True,
                                               **kwargs)
                with self.assertRaises(ValueError):
                    self.run_provider()

    def test_claude_review_invalid_effort_rejected(self):
        self.args.role = "review"
        self.args.dry_run = True
        self.args.review_reason = "Valid reason"
        for invalid_effort in ("xhigh", "max", "low", "minimal"):
            with self.subTest(invalid_effort=invalid_effort):
                self.args.review_effort = invalid_effort
                with self.assertRaises(ValueError):
                    self.run_provider()

    def test_review_execution_records_effort_and_reason_in_result(self):
        self.args.role = "review"
        self.args.dry_run = False
        self.args.review_effort = "high"
        self.args.review_reason = "Coupled state and security gate review"
        self.fake_result({"type": "result", "subtype": "success", "is_error": False}, 0)
        result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))
        self.assertEqual(result["review_effort"], "high")
        self.assertEqual(result["review_reason"], "Coupled state and security gate review")
        result_on_disk = runner.read_json(Path(result["logs"]) / "result.json")
        self.assertEqual(result_on_disk["review_effort"], "high")
        self.assertEqual(result_on_disk["review_reason"], "Coupled state and security gate review")


if __name__ == "__main__":
    unittest.main()
