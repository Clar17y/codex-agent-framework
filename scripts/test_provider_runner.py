import argparse
import concurrent.futures
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
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

        def slow_finish(result, *args, **kwargs):
            if result.get("status") == "blocked_pending_run":
                collecting.set()
                self.assertTrue(release.wait(5))
            return real_finish(result, *args, **kwargs)

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
        self.assertEqual(result["status"], "provider_error")

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

    def test_deepseek_command_construction(self):
        output_dir = self.root / "output"
        output_dir.mkdir()
        deepseek_config = {
            "executable": "codex.exe",
            "profile": "deepseek",
            "model": "deepseek-flash",
        }
        cmd = runner.command_for(
            role="implement",
            provider=deepseek_config,
            prompt="Test prompt",
            timeout=300,
            workspace=self.root,
            effort="medium",
            provider_name="deepseek",
            output_dir=output_dir,
        )
        self.assertEqual(cmd[0], "codex.exe")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "deepseek")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek-flash")
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("-s", cmd)
        self.assertFalse(any(arg.startswith("sandbox_mode") for arg in cmd))
        self.assertIn("--approve-for-me", cmd)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "shell_environment_policy.ignore_default_excludes=false")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn("--output-last-message", cmd)
        self.assertEqual(cmd[cmd.index("--output-last-message") + 1], str(output_dir / "last_message.txt"))
        self.assertIn("--ephemeral", cmd)
        self.assertEqual(cmd[-1], "Test prompt")

        # Rejects unpinned model
        with self.assertRaisesRegex(ValueError, "pinned model deepseek-flash"):
            runner.command_for(
                role="implement",
                provider={"executable": "codex", "profile": "deepseek", "model": "deepseek-chat"},
                prompt="p", timeout=10, workspace=self.root, effort="medium",
                provider_name="deepseek", output_dir=output_dir,
            )

        # Rejects review role for deepseek
        with self.assertRaisesRegex(ValueError, "Review role is Claude-only"):
            runner.command_for(
                role="review",
                provider=deepseek_config,
                prompt="p", timeout=10, workspace=self.root, effort="medium",
                provider_name="deepseek", output_dir=output_dir,
            )

        # Rejects review role for deepseek in execute
        self.settings["providers"]["deepseek"] = deepseek_config
        runner.write_json(self.config, self.settings)
        review_args = argparse.Namespace(role="review", workspace=str(self.root), task_file=str(self.task),
                                         config=str(self.config), state_dir=str(self.root / "state"),
                                         dry_run=True, provider="deepseek")
        with self.assertRaisesRegex(ValueError, "Claude-only"):
            runner.execute(review_args)

    def test_windows_deepseek_cmd_uses_direct_node_launcher(self):
        launcher_dir = self.root / "npm"
        codex_js = launcher_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        codex_js.parent.mkdir(parents=True)
        codex_js.write_text("", encoding="utf-8")
        node = launcher_dir / "node.exe"
        node.write_bytes(b"")
        shim = launcher_dir / "codex.cmd"
        shim.write_text("@echo off", encoding="utf-8")

        command = runner.direct_windows_codex_command([str(shim), "exec", "prompt"], platform_name="nt")

        self.assertEqual(command, [str(node.resolve()), str(codex_js), "exec", "prompt"])
        with mock.patch.object(runner.shutil, "which", return_value=str(shim)):
            discovered_command = runner.direct_windows_codex_command(["codex", "exec", "prompt"], platform_name="nt")
        self.assertEqual(discovered_command, [str(node.resolve()), str(codex_js), "exec", "prompt"])

    def test_windows_deepseek_unrecognized_batch_launcher_fails_closed(self):
        shim = self.root / "custom-codex.cmd"
        shim.write_text("@echo off", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "direct npm Node entrypoint could not be found"):
            runner.direct_windows_codex_command([str(shim)], platform_name="nt")

    def test_child_environment_isolates_and_maps_deepseek_credentials(self):
        config = {"providers": {"deepseek": {"api_key_env": "CUSTOM_DEEPSEEK_KEY"}}}
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "other-account", "CUSTOM_DEEPSEEK_KEY": "checked-account"}, clear=False):
            gemini = runner.child_environment(config, "gemini")
            claude = runner.child_environment(config, "claude")
            deepseek = runner.child_environment(config, "deepseek", "checked-account")
        self.assertNotIn("DEEPSEEK_API_KEY", gemini)
        self.assertNotIn("CUSTOM_DEEPSEEK_KEY", gemini)
        self.assertNotIn("DEEPSEEK_API_KEY", claude)
        self.assertNotIn("CUSTOM_DEEPSEEK_KEY", claude)
        self.assertEqual(deepseek["DEEPSEEK_API_KEY"], "checked-account")
        self.assertNotIn("CUSTOM_DEEPSEEK_KEY", deepseek)

    def test_deepseek_balance_redirects_are_refused(self):
        handler = runner.NoRedirectHandler()
        request = urllib.request.Request("https://api.deepseek.com/user/balance")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.invalid/capture",
            )
        self.assertEqual(caught.exception.code, 302)
        self.assertIn("not permitted", str(caught.exception))
        caught.exception.close()

    def test_finish_redacts_timeout_artifacts(self):
        secret = "timeout-secret-" + uuid.uuid4().hex
        output = self.root / "timeout-output"
        output.mkdir()
        (output / "stderr.log").write_text(secret, encoding="utf-8")
        result, _ = runner.finish({"status": "timeout", "error": secret}, output, self.root, 1, secrets=[secret])
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, (output / "stderr.log").read_text(encoding="utf-8"))

    def test_git_evidence_strips_deepseek_credentials_and_external_helpers(self):
        first_secret = "evidence-secret-" + uuid.uuid4().hex
        second_secret = "configured-secret-" + uuid.uuid4().hex
        output = self.root / "evidence-output"
        output.mkdir()
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": first_secret, "CUSTOM_DEEPSEEK_KEY": second_secret},
            clear=False,
        ):
            with mock.patch.object(runner.subprocess, "run", return_value=completed) as run:
                runner.git_evidence(self.root, output, [first_secret, second_secret])

        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            command = call.args[0]
            environment = call.kwargs["env"]
            self.assertNotIn("DEEPSEEK_API_KEY", environment)
            self.assertNotIn("CUSTOM_DEEPSEEK_KEY", environment)
            self.assertIn("-c", command)
            self.assertIn("core.fsmonitor=false", command)
        diff_command = next(call.args[0] for call in run.call_args_list if "diff" in call.args[0])
        self.assertIn("--no-ext-diff", diff_command)
        self.assertIn("--no-textconv", diff_command)

    def test_query_deepseek_balance_parsing(self):
        config = {"providers": {"deepseek": {"api_key_env": "TEST_DS_KEY", "base_url": "https://api.deepseek.com"}}}

        # 1. Missing or empty API key
        with mock.patch.dict(os.environ, {}, clear=True):
            data, err = runner.query_deepseek_balance(config)
            self.assertIsNone(data)
            self.assertIn("not set or empty", err)

        # 2. Valid positive balance
        valid_payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "12.50", "granted_balance": "0.00", "topped_up_balance": "12.50"}
            ]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps(valid_payload).encode("utf-8")
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNone(err)
                self.assertEqual(data["is_available"], True)
                self.assertEqual(data["balance_infos"][0]["total_balance"], "12.50")

        # 3. Depleted balance (total_balance: "0.00")
        depleted_payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"}
            ]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps(depleted_payload).encode("utf-8")
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNotNone(err)
                self.assertIn("depleted", err)

        # 4. is_available: false
        unavailable_payload = {
            "is_available": False,
            "balance_infos": [
                {"currency": "USD", "total_balance": "5.00", "granted_balance": "0.00", "topped_up_balance": "5.00"}
            ]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps(unavailable_payload).encode("utf-8")
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNotNone(err)
                self.assertIn("is_available=false", err)

        # 5. HTTP 402 error
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_opener:
                mock_opener.return_value.open.side_effect = urllib.error.HTTPError("url", 402, "Payment Required", {}, None)
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNone(data)
                self.assertIn("402", err)

        # 6. Malformed JSON
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = b"NOT_JSON"
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNone(data)
                self.assertIn("malformed", err)

    def test_deepseek_execution_success_and_balance_refresh(self):
        # Fake codex runner writes valid structured JSON to --output-last-message and exits 0
        self.fake.write_text(
            "import sys, pathlib, json\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text(json.dumps({'status': 'SUCCESS', 'response': 'Task completed successfully'}), encoding='utf-8')\n"
            "        break\n"
            "print('{\"type\": \"completed\"}')\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "8.00", "granted_balance": "0.00", "topped_up_balance": "8.00"}]
        }
        refreshed_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "7.98", "granted_balance": "0.00", "topped_up_balance": "7.98"}]
        }
        call_count = 0
        def fake_query(cfg, timeout=15):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return valid_payload, None
            return refreshed_payload, None

        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", side_effect=fake_query):
                result, code = self.run_provider()

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertGreaterEqual(call_count, 2)  # preflight + post-run refresh

        # Verify balance snapshot on disk
        balance_file = runner.balance_path(Path(self.args.state_dir))
        self.assertTrue(balance_file.exists())
        balance_data = runner.read_json(balance_file)
        self.assertEqual(balance_data["is_available"], True)
        self.assertEqual(balance_data["total_balance"], "7.98")

    def test_deepseek_normal_invalid_output_resolves_pending(self):
        # A local Codex exit is certain even when it does not produce a valid final.
        self.fake.write_text("import sys\nprint('starting work...')\nsys.exit(0)\n", encoding="utf-8")
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        self.assertIn("malformed", result["error"])
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})

        # Pending ownership is released after a normal local process exit.
        pending_record = runner.read_json(self.pending_path)
        self.assertEqual(pending_record.get("status"), "resolved")

    def test_deepseek_preflight_fail_closed_zero_balance(self):
        sentinel = self.root / "codex_spawned.txt"
        self.fake.write_text(f"import pathlib\npathlib.Path({str(sentinel)!r}).touch()\n", encoding="utf-8")
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        depleted_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(depleted_payload, "DeepSeek balance is depleted (no positive total_balance)")):
                result, code = self.run_provider()

        self.assertEqual(code, 20)
        self.assertEqual(result["status"], "fallback_required")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertFalse(sentinel.exists(), "CLI process must NOT be spawned when balance is depleted")

        # Balance snapshot records depleted status
        balance_data = runner.read_json(runner.balance_path(Path(self.args.state_dir)))
        self.assertEqual(balance_data["is_available"], False)

    def test_deepseek_preflight_fail_closed_missing_key(self):
        sentinel = self.root / "codex_spawned.txt"
        self.fake.write_text(f"import pathlib\npathlib.Path({str(sentinel)!r}).touch()\n", encoding="utf-8")
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        with mock.patch.dict(os.environ, {}, clear=True):
            result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "balance_check_failed")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertIn("not set or empty", result["error"])
        self.assertFalse(sentinel.exists(), "CLI process must NOT be spawned when API key is missing")

    def test_deepseek_preflight_fail_closed_network_or_500_error(self):
        sentinel = self.root / "codex_spawned.txt"
        self.fake.write_text(f"import pathlib\npathlib.Path({str(sentinel)!r}).touch()\n", encoding="utf-8")
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(None, "DeepSeek balance query failed with HTTP 500: Internal Server Error")):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "balance_check_failed")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertIn("HTTP 500", result["error"])
        self.assertFalse(sentinel.exists(), "CLI process must NOT be spawned when balance query returns HTTP 500")

    def test_deepseek_runtime_402_triggers_luna_fallback(self):
        # Fake codex fails with HTTP 402 Insufficient Balance
        self.fake.write_text(
            "import sys\n"
            "sys.stderr.write('Error: 402 Payment Required: Insufficient balance in account\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "0.50", "granted_balance": "0.00", "topped_up_balance": "0.50"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 20)
        self.assertEqual(result["status"], "fallback_required")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertIn("402", result["error"])

        # Snapshot is marked unavailable
        balance_data = runner.read_json(runner.balance_path(Path(self.args.state_dir)))
        self.assertEqual(balance_data["is_available"], False)

    def test_deepseek_runtime_429_is_not_treated_as_balance_exhaustion(self):
        # Fake codex fails with 429 Rate Limit (NOT 402 balance exhaustion)
        self.fake.write_text(
            "import sys\n"
            "sys.stderr.write('Error: 429 Too Many Requests: Rate limit exceeded for model deepseek-flash\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        # Ensure 429 rate limit did NOT trigger code 20 or mark balance depleted
        self.assertNotEqual(code, 20)

    def test_deepseek_structured_provider_error_preserves_message(self):
        secret = "test-deepseek-secret-token"
        detail = "Authentication failed for the configured DeepSeek profile using "
        self.fake.write_text(
            "import json, os, sys\n"
            f"print(json.dumps({{'type': 'error', 'error': {{'code': 'authentication_error', 'message': {detail!r} + os.environ['DEEPSEEK_API_KEY']}}}}))\n"
            "sys.exit(1)\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": secret}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(result["error"], detail + "[REDACTED]")
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})

    def test_gemini_quota_exhaustion_points_to_deepseek_when_configured(self):
        self.settings["providers"]["deepseek"] = {
            "executable": "codex",
            "profile": "deepseek",
            "model": "deepseek-flash",
        }
        self.fake_result({"error": {"code": "QUOTA_EXHAUSTED", "message": "Daily quota exhausted"}})
        result, code = self.run_provider()
        self.assertEqual(code, 20)
        self.assertEqual(result["fallback"], {"provider": "deepseek", "model": "deepseek-flash"})

    def test_gemini_non_quota_error_routes_to_luna(self):
        self.settings["providers"]["deepseek"] = {
            "executable": "codex",
            "profile": "deepseek",
            "model": "deepseek-flash",
        }
        # Fake exits with code 1 without quota message
        self.fake.write_text("import sys\nsys.stderr.write('SyntaxError in workspace code\\n')\nsys.exit(1)\n", encoding="utf-8")
        result, code = self.run_provider()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})

    def test_shared_ownership_gemini_and_deepseek(self):
        # Pending claim created by Gemini blocks DeepSeek on same path
        state = runner.workspace_state_dir(Path(self.args.state_dir), self.root) / "gemini-pending"
        state.mkdir(parents=True)
        runner.write_json(state / "gemini_run.json", {
            "status": "running",
            "workspace": str(self.root),
            "owned_paths": ["src/service.py"],
            "logs": "old_logs",
            "provider": "gemini",
        })
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "5.00", "granted_balance": "0.00", "topped_up_balance": "5.00"}]
        }

        # Overlapping path is blocked
        self.args.task_file = str(self.task_for("overlap", ["src/service.py"]))
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked_pending_run")
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})

        # Disjoint path runs successfully
        self.fake.write_text(
            "import sys, pathlib, json\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text(json.dumps({'status': 'SUCCESS', 'response': 'Done'}), encoding='utf-8')\n"
            "        break\n"
            "print('{\"status\":\"SUCCESS\"}')\n",
            encoding="utf-8"
        )
        self.args.task_file = str(self.task_for("disjoint", ["src/other.py"]))
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")

    def test_deepseek_status_live_vs_cached(self):
        self.settings["providers"]["deepseek"] = {
            "executable": "codex",
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        runner.write_json(self.config, self.settings)
        state_dir = Path(self.args.state_dir)
        balance_path = runner.balance_path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)

        # 1. Without cache and without --check-live: reports available_to_try, live: False, cached: False
        args = argparse.Namespace(provider="deepseek", config=str(self.config), state_dir=str(state_dir), check_live=False)
        result, code = runner.cli_quota_status(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "available_to_try")
        self.assertFalse(result["live"])
        self.assertFalse(result["cached"])

        # 2. Write cached snapshot
        snapshot = {
            "version": 1,
            "provider": "deepseek",
            "model": "deepseek-flash",
            "observed_at": "2026-09-14T10:00:00Z",
            "is_available": True,
            "total_balance": "14.25",
            "balance_infos": [{"currency": "USD", "total_balance": "14.25", "granted_balance": "0.00", "topped_up_balance": "14.25"}],
        }
        runner.write_json(balance_path, snapshot)

        # Read cached without --check-live
        result, code = runner.cli_quota_status(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "available_to_try")
        self.assertFalse(result["live"])
        self.assertTrue(result["cached"])
        self.assertEqual(result["balance_infos"][0]["total_balance"], "14.25")

        # 3. With --check-live: queries live endpoint, live: True, cached: False
        live_args = argparse.Namespace(provider="deepseek", config=str(self.config), state_dir=str(state_dir), check_live=True)
        live_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "22.50", "granted_balance": "0.00", "topped_up_balance": "22.50"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(live_payload, None)):
                result, code = runner.cli_quota_status(live_args)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "available_to_try")
        self.assertTrue(result["live"])
        self.assertFalse(result["cached"])
        self.assertEqual(result["balance_infos"][0]["total_balance"], "22.50")

        # 4. Verify Gemini and Claude status checks remain local-only even if --check-live is requested
        all_args = argparse.Namespace(provider=None, config=str(self.config), state_dir=str(state_dir), check_live=True)
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(live_payload, None)):
                result, code = runner.cli_quota_status(all_args)
        self.assertEqual(code, 0)
        self.assertFalse(result["providers"]["gemini"].get("live", False))
        self.assertFalse(result["providers"]["claude"].get("live", False))
        self.assertTrue(result["providers"]["deepseek"]["live"])

    def test_deepseek_secret_non_disclosure(self):
        secret = "secret-token-" + uuid.uuid4().hex
        self.fake.write_text(
            "import sys, pathlib, json\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text(json.dumps({'status': 'SUCCESS', 'response': 'Success output'}), encoding='utf-8')\n"
            "        break\n"
            "print(__import__('os').environ.get('SECRET_DS_KEY', ''))\n"
            "print('{\"status\":\"SUCCESS\"}')\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "SECRET_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "9.99", "granted_balance": "0.00", "topped_up_balance": "9.99"}]
        }
        with mock.patch.dict(os.environ, {"SECRET_DS_KEY": secret}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 0)
        # Check result dictionary
        result_str = json.dumps(result)
        self.assertNotIn(secret, result_str)

        # Check logs directory
        log_dir = Path(result["logs"])
        for file in log_dir.glob("*"):
            if file.is_file():
                self.assertNotIn(secret, file.read_text(encoding="utf-8", errors="replace"))

        # Check state directory
        state_dir = Path(self.args.state_dir)
        for file in state_dir.glob("**/*"):
            if file.is_file():
                self.assertNotIn(secret, file.read_text(encoding="utf-8", errors="replace"))

    def test_deepseek_terminal_success_wins_over_intermediate_error(self):
        """Codex JSONL retries and 402 prose cannot redo a completed task in Luna."""
        self.fake.write_text(
            "import sys, pathlib, json\n"
            "out=sys.argv[sys.argv.index('--output-last-message') + 1]\n"
            "pathlib.Path(out).write_text(json.dumps({'status':'SUCCESS','response':'Done'}))\n"
            "print(json.dumps({'type':'error','error':{'code':402,'message':'retry noise'}}))\n"
            "print(json.dumps({'type':'turn.completed'}))\n", encoding="utf-8")
        self.settings["providers"]["deepseek"] = {"executable": [sys.executable, str(self.fake)], "profile": "deepseek",
                                                   "model": "deepseek-flash", "api_key_env": "TEST_DS_KEY"}
        self.args.provider = "deepseek"
        balance = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "1.00", "granted_balance": "0.00", "topped_up_balance": "1.00"}]}
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(balance, None)):
                result, code = self.run_provider()
        self.assertEqual((result["status"], code), ("completed", 0))

    def test_deepseek_command_args_and_output_schema_generation(self):
        """Codex command must be pinned to exec, profile deepseek, model deepseek-flash, ephemeral JSONL, bypass, and output-schema."""
        provider_config = {
            "executable": "codex.exe",
            "profile": "deepseek",
            "model": "deepseek-flash",
        }
        output_dir = self.root / ".llm-output" / "test_cmd"
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = runner.command_for("implement", provider_config, "Prompt text", 600, self.root, provider_name="deepseek", output_dir=output_dir)

        # Verify command flags
        self.assertEqual(cmd[0], "codex.exe")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "deepseek")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek-flash")
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("-s", cmd)
        self.assertFalse(any(arg.startswith("sandbox_mode") for arg in cmd))
        self.assertIn("--approve-for-me", cmd)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "shell_environment_policy.ignore_default_excludes=false")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn("--output-last-message", cmd)

        # Construction must be side-effect free; execute creates the schema once.
        schema_path = Path(cmd[cmd.index("--output-schema") + 1])
        self.assertFalse(schema_path.exists())

    def test_deepseek_rejection_of_arbitrary_non_empty_output_resolves_pending(self):
        """A local normal exit with malformed output is rejected but resolves its ownership claim."""
        self.fake.write_text(
            "import sys, pathlib\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text('Arbitrary non-JSON task success message\\n', encoding='utf-8')\n"
            "        break\n"
            "sys.exit(0)\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        self.assertIn("malformed", result["error"])

        # The local process has exited, so it cannot retain ownership.
        pending_record = runner.read_json(self.pending_path)
        self.assertEqual(pending_record.get("status"), "resolved")

    def test_deepseek_rejection_of_progress_event(self):
        """Progress JSONL events (e.g. turn.completed) must not count as completion."""
        self.fake.write_text(
            "import sys, pathlib\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text('{\"type\": \"turn.completed\"}', encoding='utf-8')\n"
            "        break\n"
            "print('{\"type\": \"item.completed\"}')\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        pending_record = runner.read_json(self.pending_path)
        self.assertEqual(pending_record.get("status"), "resolved")

    def test_deepseek_rejection_of_malformed_final_output(self):
        """Malformed JSON (missing required response field) is rejected as non-success and leaves pending unresolved."""
        self.fake.write_text(
            "import sys, pathlib\n"
            "for flag in ('--output-last-message', '-o'):\n"
            "    if flag in sys.argv:\n"
            "        out_idx = sys.argv.index(flag) + 1\n"
            "        pathlib.Path(sys.argv[out_idx]).write_text('{\"status\": \"SUCCESS\"}', encoding='utf-8')\n"
            "        break\n",
            encoding="utf-8"
        )
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_DS_KEY",
        }
        self.args.provider = "deepseek"
        valid_payload = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
            with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "provider_error")
        pending_record = runner.read_json(self.pending_path)
        self.assertEqual(pending_record.get("status"), "resolved")

    def test_deepseek_explicit_blocked_or_error_status_is_terminal_but_unsuccessful(self):
        """Explicit terminal status (BLOCKED or ERROR) is unsuccessful, but safely resolves pending ownership."""
        for status_val in ("BLOCKED", "ERROR", "FAILED"):
            self.fake.write_text(
                f"import sys, pathlib, json\n"
                f"for flag in ('--output-last-message', '-o'):\n"
                f"    if flag in sys.argv:\n"
                f"        out_idx = sys.argv.index(flag) + 1\n"
                f"        pathlib.Path(sys.argv[out_idx]).write_text(json.dumps({{'status': '{status_val}', 'response': 'Explicit failure details'}}), encoding='utf-8')\n"
                f"        break\n",
                encoding="utf-8"
            )
            self.settings["providers"]["deepseek"] = {
                "executable": [sys.executable, str(self.fake)],
                "profile": "deepseek",
                "model": "deepseek-flash",
                "api_key_env": "TEST_DS_KEY",
            }
            self.args.provider = "deepseek"
            valid_payload = {
                "is_available": True,
                "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]
            }
            with mock.patch.dict(os.environ, {"TEST_DS_KEY": "token-123"}):
                with mock.patch.object(runner, "query_deepseek_balance", return_value=(valid_payload, None)):
                    result, code = self.run_provider()

            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "provider_error")
            self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})

            # Because output was terminal, pending state IS safely resolved
            pending_record = runner.read_json(self.pending_path)
            self.assertEqual(pending_record.get("status"), "resolved")

    def test_deepseek_monetary_exact_decimal_and_field_validation(self):
        """DeepSeek balance parsing must reject booleans, floats, negatives, non-finite values, and missing fields."""
        config = {"providers": {"deepseek": {"api_key_env": "TEST_DS_KEY"}}}

        invalid_cases = [
            # Boolean total_balance
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": True, "granted_balance": "0.00", "topped_up_balance": "0.00"}]},
            # Numeric float instead of string
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": 15.5, "granted_balance": "0.00", "topped_up_balance": "0.00"}]},
            # Negative balance
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "-1.00", "granted_balance": "0.00", "topped_up_balance": "0.00"}]},
            # Non-finite NaN
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "NaN", "granted_balance": "0.00", "topped_up_balance": "0.00"}]},
            # Non-finite Infinity
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "Infinity", "granted_balance": "0.00", "topped_up_balance": "0.00"}]},
            # Missing granted_balance
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "10.00", "topped_up_balance": "10.00"}]},
            # Non-string is_available
            {"is_available": "true", "balance_infos": [{"currency": "USD", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}]},
        ]

        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            for case in invalid_cases:
                with mock.patch("urllib.request.build_opener") as mock_urlopen:
                    mock_resp = mock.MagicMock()
                    mock_resp.read.return_value = json.dumps(case).encode("utf-8")
                    mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                    data, err = runner.query_deepseek_balance(config)
                    self.assertIsNotNone(err, f"Case should have been rejected: {case}")

    def test_deepseek_multi_currency_preservation_and_availability_trust(self):
        """Preserve per-currency entries without treating first currency as aggregate; trust is_available only with positive total."""
        config = {"providers": {"deepseek": {"api_key_env": "TEST_DS_KEY"}}}

        # 1. Multi-currency: CNY is 0.00, USD is 25.00 -> is_available True, total positive
        multi_payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"},
                {"currency": "USD", "total_balance": "25.00", "granted_balance": "5.00", "topped_up_balance": "20.00"},
            ]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps(multi_payload).encode("utf-8")
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNone(err)
                self.assertEqual(len(data["balance_infos"]), 2)
                self.assertEqual(data["balance_infos"][0]["currency"], "CNY")
                self.assertEqual(data["balance_infos"][0]["total_balance"], "0.00")
                self.assertEqual(data["balance_infos"][1]["currency"], "USD")
                self.assertEqual(data["balance_infos"][1]["total_balance"], "25.00")

        # Snapshot records both currencies and does NOT treat CNY 0.00 as aggregate total
        state_dir = Path(self.args.state_dir)
        snap = runner.record_balance_snapshot(state_dir, data)
        self.assertIsNone(snap["total_balance"])  # Not aggregated to single scalar
        self.assertEqual(len(snap["balance_infos"]), 2)
        self.assertTrue(snap["is_available"])

        # 2. is_available is True, but all total balances are 0.00 -> treated as depleted!
        zero_payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"},
                {"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"},
            ]
        }
        with mock.patch.dict(os.environ, {"TEST_DS_KEY": "valid-token"}):
            with mock.patch("urllib.request.build_opener") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps(zero_payload).encode("utf-8")
                mock_urlopen.return_value.open.return_value.__enter__.return_value = mock_resp
                data, err = runner.query_deepseek_balance(config)
                self.assertIsNotNone(err)
                self.assertIn("depleted", err)

    def test_deepseek_balance_snapshot_concurrency_and_ordering(self):
        """Older observation must not overwrite newer snapshot."""
        state_dir = Path(self.args.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)

        newer_snapshot = {
            "version": 1,
            "provider": "deepseek",
            "model": "deepseek-flash",
            "observed_at": "2026-09-14T12:00:00Z",
            "is_available": True,
            "total_balance": "50.00",
            "balance_infos": [{"currency": "USD", "total_balance": "50.00", "granted_balance": "0.00", "topped_up_balance": "50.00"}],
            "error": None
        }
        runner.write_json(runner.balance_path(state_dir), newer_snapshot)

        # Attempt to record older observation (T = 11:00:00Z)
        older_data = {
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": "40.00", "granted_balance": "0.00", "topped_up_balance": "40.00"}]
        }
        older_stamp = dt.datetime.fromisoformat("2026-09-14T11:00:00+00:00").timestamp()
        with mock.patch("time.time", return_value=older_stamp):
            res = runner.record_balance_snapshot(state_dir, older_data)

        # Returned and persisted snapshot must remain the newer one
        self.assertEqual(res["observed_at"], "2026-09-14T12:00:00Z")
        persisted = runner.read_json(runner.balance_path(state_dir))
        self.assertEqual(persisted["observed_at"], "2026-09-14T12:00:00Z")
        self.assertEqual(persisted["total_balance"], "50.00")

    def test_deepseek_malformed_cached_snapshot_returns_state_error(self):
        """Malformed snapshot turns into explicit state_error, available_to_try=0, exit code 1."""
        state_dir = Path(self.args.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.settings["providers"]["deepseek"] = {
            "executable": "codex",
            "profile": "deepseek",
            "model": "deepseek-flash",
        }
        runner.write_json(self.config, self.settings)

        # 1. Invalid balance string (float) in snapshot
        runner.write_json(runner.balance_path(state_dir), {
            "version": 1,
            "is_available": True,
            "balance_infos": [{"currency": "USD", "total_balance": 15.0}]
        })

        args = argparse.Namespace(provider="deepseek", config=str(self.config), state_dir=str(state_dir), check_live=False)
        result, code = runner.cli_quota_status(args)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "state_error")
        self.assertEqual(result["available_to_try"], 0)
        self.assertEqual(result["fallback"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertIn("Malformed or unreadable", result["error"])

    def test_deepseek_secret_non_disclosure_error_paths(self):
        """Guarantee API keys never appear in result/state/log errors even for malformed keys or urllib failures."""
        state_dir = Path(self.args.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)

        # 1. Malformed key containing newline
        secret_malformed = "secret_key\r\ninjected_header: bad"
        config = {"providers": {"deepseek": {"api_key_env": "MALFORMED_KEY_ENV"}}}
        with mock.patch.dict(os.environ, {"MALFORMED_KEY_ENV": secret_malformed}):
            data, err = runner.query_deepseek_balance(config)
            self.assertIsNone(data)
            self.assertNotIn("secret_key", err)
            self.assertNotIn("injected_header", err)
            self.assertIn("invalid key characters", err)

        # 2. HTTP 401 error path
        secret_401 = "secret-token-401-" + uuid.uuid4().hex
        self.settings["providers"]["deepseek"] = {
            "executable": [sys.executable, str(self.fake)],
            "profile": "deepseek",
            "model": "deepseek-flash",
            "api_key_env": "TEST_KEY_401",
        }
        self.args.provider = "deepseek"
        with mock.patch.dict(os.environ, {"TEST_KEY_401": secret_401}):
            with mock.patch("urllib.request.build_opener") as mock_opener:
                mock_opener.return_value.open.side_effect = urllib.error.HTTPError("https://api.deepseek.com/user/balance", 401, f"Unauthorized token {secret_401}", {}, io.BytesIO(b"") )
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertNotIn(secret_401, json.dumps(result))

        # 3. HTTP 500 error path with secret in reason
        secret_500 = "secret-token-500-" + uuid.uuid4().hex
        with mock.patch.dict(os.environ, {"TEST_KEY_401": secret_500}):
            with mock.patch("urllib.request.build_opener") as mock_opener:
                mock_opener.return_value.open.side_effect = urllib.error.HTTPError("https://api.deepseek.com/user/balance", 500, f"Internal Error Bearer {secret_500}", {}, io.BytesIO(b"") )
                result, code = self.run_provider()

        self.assertEqual(code, 1)
        self.assertNotIn(secret_500, json.dumps(result))

    def test_quota_set_disallows_deepseek(self):
        """quota-set must reject deepseek and only permit subscription providers (gemini, claude)."""
        runner.write_json(self.config, self.settings)
        args = argparse.Namespace(
            provider="deepseek",
            config=str(self.config),
            state_dir=str(self.root / "state"),
            reason="Exhausted"
        )
        with self.assertRaisesRegex(ValueError, "quota-set only supports subscription quota providers"):
            runner.cli_quota_set(args)


if __name__ == "__main__":
    unittest.main()
