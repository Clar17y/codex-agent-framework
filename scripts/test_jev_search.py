"""Unit tests for TypeSafe Jev semantic code search helper."""
import errno
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from jev_client import JevClient, JevServerError
from jev_search import (
    DEFAULT_CONFIG,
    RUBRIC_LAYOUT_VERSION,
    CandidateChunk,
    ScoreCache,
    chunk_file_lines,
    contains_literal_secret,
    deduplicate_and_bound_results,
    enumerate_files,
    extract_query_terms,
    is_file_excluded,
    is_remote_authorized,
    is_safe_contained_path,
    is_symlink_or_reparse,
    load_config,
    run_doctor,
    run_inspect,
    run_search,
    scan_candidates,
    validate_config_dict,
    verify_source_integrity,
)


class TestConfigAndAuthorization(unittest.TestCase):
    """Test configuration discovery, loading, bounds validation, and authorization."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_config_when_absent(self):
        cfg, src = load_config(config_path=None, script_dir=self.temp_path)
        self.assertEqual(src, "defaults")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["allowed_roots"], [])
        self.assertEqual(cfg["model"], "jev-1.13.0")
        self.assertEqual(cfg["max_requests"], 8)
        self.assertEqual(cfg["max_candidates"], 96)

    def test_load_framework_capabilities_jev(self):
        cfg_file = self.temp_path / "routing.json"
        cfg_file.write_text(json.dumps({
            "capabilities": {
                "jev": {
                    "enabled": True,
                    "allowed_roots": [str(self.temp_path)],
                    "model": "jev-1.13.0",
                    "max_requests": 5,
                    "timeout_seconds": 15.0,
                }
            }
        }), encoding="utf-8")

        cfg, src = load_config(config_path=str(cfg_file))
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["allowed_roots"], [str(self.temp_path)])
        self.assertEqual(cfg["max_requests"], 5)
        self.assertEqual(cfg["timeout_seconds"], 15.0)

    def test_malformed_config_errors(self):
        cfg_file = self.temp_path / "bad.json"
        cfg_file.write_text("NOT JSON", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            load_config(config_path=str(cfg_file))
        self.assertIn("Malformed config", str(ctx.exception))

    def test_numeric_bounds_and_type_validation(self):
        # enabled must be boolean
        with self.assertRaises(ValueError):
            validate_config_dict({"enabled": "true"})

        # max_requests cannot be bool or <= 0
        with self.assertRaises(ValueError):
            validate_config_dict({"max_requests": True})
        with self.assertRaises(ValueError):
            validate_config_dict({"max_requests": 0})

        # timeout_seconds > 0
        with self.assertRaises(ValueError):
            validate_config_dict({"timeout_seconds": -1.0})

        # allowed_roots must be list of strings
        with self.assertRaises(ValueError):
            validate_config_dict({"allowed_roots": "not_a_list"})

    def test_authorization_checks(self):
        ws = self.temp_path
        # 1. Disabled -> unauthorized
        cfg = {"enabled": False, "allowed_roots": [str(ws)]}
        self.assertFalse(is_remote_authorized(ws, cfg, allow_remote=False))

        # 2. Enabled but workspace not in allowed_roots -> unauthorized
        cfg = {"enabled": True, "allowed_roots": ["C:/other/path"]}
        self.assertFalse(is_remote_authorized(ws, cfg, allow_remote=False))

        # 3. Enabled and workspace in allowed_roots -> authorized
        cfg = {"enabled": True, "allowed_roots": [str(ws)]}
        self.assertTrue(is_remote_authorized(ws, cfg, allow_remote=False))

        # 4. --allow-remote overrides
        cfg = {"enabled": False, "allowed_roots": []}
        self.assertTrue(is_remote_authorized(ws, cfg, allow_remote=True))


class TestCandidatePipelineAndExclusions(unittest.TestCase):
    """Test candidate enumeration, exclusions, literal secrets, binary, and symlink rejection."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_root_escape_rejected(self):
        outside_scope = "../outside"
        files, skipped, reasons = enumerate_files(self.ws, [outside_scope])
        self.assertIn("scope_escape_or_symlink_rejected", reasons)
        self.assertTrue(any(s["reason"] == "scope_escape_or_symlink" for s in skipped))

    def test_excluded_directories(self):
        # Create excluded directory structures
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (self.ws / ".llm-output").mkdir()
        (self.ws / ".llm-output" / "out.txt").write_text("cached data", encoding="utf-8")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "pkg.js").write_text("console.log('pkg')", encoding="utf-8")

        # Valid dir and file
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("def run(): pass\n", encoding="utf-8")

        files, skipped, _ = enumerate_files(self.ws, [])
        rel_files = [str(f.relative_to(self.ws)).replace("\\", "/") for f in files]

        self.assertIn("src/app.py", rel_files)
        self.assertNotIn(".git/HEAD", rel_files)
        self.assertNotIn(".llm-output/out.txt", rel_files)
        self.assertNotIn("node_modules/pkg.js", rel_files)

    def test_excluded_credential_files(self):
        (self.ws / ".env").write_text("SECRET=123", encoding="utf-8")
        (self.ws / ".env.local").write_text("SECRET=123", encoding="utf-8")
        (self.ws / "id_rsa").write_text("KEY", encoding="utf-8")
        (self.ws / "server.key").write_text("KEY", encoding="utf-8")
        (self.ws / "credentials.json").write_text("{}", encoding="utf-8")

        for fname in [".env", ".env.local", "id_rsa", "server.key", "credentials.json"]:
            self.assertTrue(is_file_excluded(fname), f"Expected {fname} to be excluded")

    def test_literal_secret_detection(self):
        # Secret contents
        google_secret = b"KEY = 'AIzaSyDb0123456789abcdefghijklmnopqrstuv'"
        self.assertTrue(contains_literal_secret(google_secret))

        github_secret = b"TOKEN = 'ghp_0123456789abcdefghijklmnopqrstuvwxyz'"
        self.assertTrue(contains_literal_secret(github_secret))

        private_key = b"-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        self.assertTrue(contains_literal_secret(private_key))

        # Safe content
        safe_code = b"def calculate_total(a, b):\n    return a + b\n"
        self.assertFalse(contains_literal_secret(safe_code))

    def test_binary_and_non_utf8_exclusion(self):
        (self.ws / "binary.dat").write_bytes(b"\x00\x01\x02\x03\x04")
        (self.ws / "invalid_utf8.txt").write_bytes(b"\xff\xfe\x00\x00\x80")
        (self.ws / "valid.txt").write_text("hello world", encoding="utf-8")

        candidates, skipped, _ = scan_candidates(self.ws, [], query="hello")
        skipped_reasons = {s["path"]: s["reason"] for s in skipped}

        self.assertIn("binary.dat", skipped_reasons)
        self.assertIn(skipped_reasons["binary.dat"], ("binary_content", "non_utf8"))
        self.assertIn("invalid_utf8.txt", skipped_reasons)
        self.assertIn(skipped_reasons["invalid_utf8.txt"], ("binary_content", "non_utf8"))

    def test_oversized_file_reported(self):
        huge_file = self.ws / "large.py"
        # 600KB file
        huge_file.write_bytes(b"x = 1\n" * 100000)

        candidates, skipped, reasons = scan_candidates(self.ws, [], query="x", max_file_size_bytes=100 * 1024)
        self.assertIn("file_size_exceeded", reasons)
        self.assertTrue(any("oversized_file" in s["reason"] for s in skipped))


class TestChunkingAndExactLines(unittest.TestCase):
    """Test line chunking, exact 1-indexed ranges, and deduplication."""

    def test_exact_lines_and_ranges(self):
        lines = [f"line {i}\n" for i in range(1, 101)]
        chunks = chunk_file_lines("test.py", lines, "file_hash", window_lines=40, overlap_lines=10)

        # Chunk 1: lines 1 to 40
        self.assertEqual(chunks[0].start_line, 1)
        self.assertEqual(chunks[0].end_line, 40)
        self.assertEqual(chunks[0].text, "".join(lines[0:40]))

        # Chunk 2: step = 30 -> lines 31 to 70
        self.assertEqual(chunks[1].start_line, 31)
        self.assertEqual(chunks[1].end_line, 70)
        self.assertEqual(chunks[1].text, "".join(lines[30:70]))

    def test_deduplication_and_output_bound(self):
        # Two overlapping candidates from same file
        c1 = CandidateChunk("a.py", 1, 40, "excerpt1\n", "hash1", "fhash", lexical_score=1.0)
        c1.score = 0.9
        c1.unscored = False

        c2 = CandidateChunk("a.py", 20, 60, "excerpt2\n", "hash2", "fhash", lexical_score=1.0)
        c2.score = 0.7
        c2.unscored = False

        c3 = CandidateChunk("b.py", 1, 20, "excerpt3\n", "hash3", "fhash", lexical_score=1.0)
        c3.score = 0.8
        c3.unscored = False

        # c1 and c2 overlap; c1 has higher score. c3 is distinct.
        results = deduplicate_and_bound_results([c1, c2, c3], top_k=6, max_output_chars=5000)
        paths = [r["path"] for r in results]
        self.assertEqual(paths, ["a.py", "b.py"])
        self.assertEqual(results[0]["score"], 0.9)


class TestScoreCache(unittest.TestCase):
    """Test score-only cache persistence, link-safety, TTL, and corruption handling."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name).resolve()
        self.cache_dir = self.ws / "cache"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_score_only_persistence_no_plaintext(self):
        cache = ScoreCache(self.ws, cache_dir=self.cache_dir)
        key = cache.compute_key("a.py", 1, 10, "content_hash", "secret_query", "rubric", "jev-1.13.0")
        cache.store_scores({key: 0.88})

        # Verify score is retrievable
        self.assertEqual(cache.get_score(key), 0.88)

        # Inspect cache file on disk
        cache_content = (self.cache_dir / "scores.json").read_text(encoding="utf-8")
        self.assertNotIn("secret_query", cache_content)
        self.assertNotIn("content_hash", cache_content)
        self.assertNotIn("a.py", cache_content)
        self.assertIn(key, cache_content)
        self.assertIn("0.88", cache_content)

    def test_changed_query_or_content_invalidates_cache(self):
        cache = ScoreCache(self.ws, cache_dir=self.cache_dir)
        key1 = cache.compute_key("a.py", 1, 10, "hash1", "query1", "rubric", "jev-1.13.0")
        key2 = cache.compute_key("a.py", 1, 10, "hash1", "query2", "rubric", "jev-1.13.0")
        key3 = cache.compute_key("a.py", 1, 10, "hash2", "query1", "rubric", "jev-1.13.0")

        cache.store_scores({key1: 0.75})
        self.assertEqual(cache.get_score(key1), 0.75)
        self.assertIsNone(cache.get_score(key2))  # Different query
        self.assertIsNone(cache.get_score(key3))  # Different content

    def test_corrupt_cache_is_miss(self):
        cache = ScoreCache(self.ws, cache_dir=self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "scores.json").write_text("{corrupt json", encoding="utf-8")

        key = cache.compute_key("a.py", 1, 10, "h", "q", "r", "jev-1.13.0")
        self.assertIsNone(cache.get_score(key))


class TestSearchOperationTransitionsAndFallbacks(unittest.TestCase):
    """Test state transitions, stale source drop, batch failure, and fallback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name).resolve()
        self.src_file = self.ws / "module.py"
        self.src_file.write_text("def auth():\n    return True\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lexical_fallback_when_remote_disabled(self):
        res = run_search(
            workspace=self.ws,
            query="auth",
            allow_remote=False,
            config_path=None,
        )
        self.assertEqual(res["status"], "unavailable")
        self.assertIn("remote_disabled", res["coverage"]["reasons"])
        self.assertGreater(len(res["results"]), 0)
        first = res["results"][0]
        self.assertIsNone(first["score"])
        self.assertTrue(first["unscored"])

    def test_stale_source_dropped_after_scoring(self):
        """If source file on disk changes between candidate generation and evidence return, drop candidate."""
        def fake_transport(payload, headers):
            ans = {}
            for qid in payload["questions"]:
                ans[qid] = {"type": "noul", "noul": 0.99}
            return {
                "model": "jev-1.13.0",
                "answers": ans,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        client = JevClient(transport=fake_transport)

        # Hook into verify_source_integrity or modify file during evaluate_questions
        original_evaluate = client.evaluate_questions
        def modify_file_and_evaluate(*args, **kwargs):
            # Mutate the source file on disk right before scoring finishes
            self.src_file.write_text("def completely_different():\n    pass\n", encoding="utf-8")
            return original_evaluate(*args, **kwargs)

        client.evaluate_questions = modify_file_and_evaluate

        res = run_search(
            workspace=self.ws,
            query="auth",
            allow_remote=True,
            client=client,
        )

        self.assertEqual(res["status"], "partial")
        self.assertIn("stale_source_dropped", res["coverage"]["reasons"])
        # Stale candidate dropped
        self.assertEqual(len(res["results"]), 0)

    def test_batch_success_followed_by_error_yields_partial(self):
        """Batch 1 succeeds, batch 2 encounters error -> yields partial status."""
        # Create two separate files so we get multiple candidates
        (self.ws / "mod1.py").write_text("def func1():\n    return 'one'\n", encoding="utf-8")
        (self.ws / "mod2.py").write_text("def func2():\n    return 'two'\n", encoding="utf-8")

        call_count = 0
        def fake_transport(payload, headers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                ans = {qid: {"type": "noul", "noul": 0.85} for qid in payload["questions"]}
                return {
                    "model": "jev-1.13.0",
                    "answers": ans,
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                }
            raise JevServerError("Simulated server timeout on batch 2", status_code=504)

        client = JevClient(transport=fake_transport)

        # Force batch size of 1 by patching batch_questions
        with patch("jev_client.batch_questions", side_effect=lambda s, q: [{k: v} for k, v in q.items()]):
            res = run_search(
                workspace=self.ws,
                query="func",
                allow_remote=True,
                client=client,
            )

        self.assertEqual(res["status"], "partial")
        self.assertGreater(res["stats"]["candidates_scored"], 0)
        self.assertTrue(any("api_error" in r for r in res["coverage"]["reasons"]))


class TestCLISubcommands(unittest.TestCase):
    """Test CLI commands doctor, inspect, and search."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name).resolve()
        (self.ws / "hello.py").write_text("print('hello world')\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_doctor_never_exposes_key(self):
        secret_key = "super-secret-doctor-key-999"
        old_key = os.environ.get("TYPESAFE_API_KEY")
        try:
            os.environ["TYPESAFE_API_KEY"] = secret_key
            res = run_doctor(self.ws)
            output_str = json.dumps(res)
            self.assertNotIn(secret_key, output_str)
            self.assertTrue(res["api_key_present"])
            self.assertEqual(res["status"], "complete")
        finally:
            if old_key:
                os.environ["TYPESAFE_API_KEY"] = old_key
            else:
                os.environ.pop("TYPESAFE_API_KEY", None)

    def test_inspect_summary_without_raw_dump(self):
        res = run_inspect(self.ws, scopes=[])
        self.assertEqual(res["command"], "inspect")
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["candidate_count"], 1)
        self.assertNotIn("candidates", res)  # No raw candidate dump


if __name__ == "__main__":
    unittest.main()


class TestAdversarialSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_key_literal_excluded(self):
        key = "typesafe-real-looking-key-0123456789"
        (self.ws / "leak.py").write_text("VALUE = " + repr(key))
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": key}):
            result = run_search(self.ws, query="VALUE")
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(result["results"], [])
        self.assertFalse(result["coverage"]["complete"])

    def test_empty_rg_listing_does_not_walk_ignored_files(self):
        (self.ws / ".gitignore").write_text("ignored.py\n")
        (self.ws / "ignored.py").write_text("def target(): return 1\n")
        result = run_search(self.ws, query="target")
        self.assertFalse(any(r["path"] == "ignored.py" for r in result.get("results", [])))

    def test_linked_scope_rejected_before_resolve(self):
        outside = Path(self.tmp.name).parent / "outside-jev.py"
        (self.ws / "real.py").write_text("hello")
        link = self.ws / "alias.py"
        try:
            link.symlink_to(self.ws / "real.py")
        except OSError:
            return
        _, _, reasons = enumerate_files(self.ws, ["alias.py"])
        self.assertIn("scope_escape_or_symlink_rejected", reasons)

    def test_partial_scan_with_zero_candidates(self):
        (self.ws / "large.py").write_bytes(b"x" * 600000)
        result = run_search(self.ws, query="x")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["status"], "partial")

    def test_invalid_query_does_not_echo(self):
        secret = "typesafe-real-looking-key-0123456789"
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": secret}):
            result = run_search(self.ws, query="find " + secret)
        self.assertEqual(result["status"], "error")
        self.assertNotIn(secret, json.dumps(result))

    def test_relative_allowed_root_rejected(self):
        with self.assertRaises(ValueError):
            validate_config_dict({"allowed_roots": ["."]})


class TestReviewedSearchBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_serialized_chunk_cap_and_long_line_omission(self):
        from jev_search import MAX_CHUNK_SERIALIZED_BYTES
        lines = ["\u754c" * 100 + "\n" for _ in range(30)] + ["x" * 9000 + "\n", "tail\n"]
        chunks = chunk_file_lines("a.py", lines, "hash")
        self.assertTrue(chunks)
        self.assertTrue(all(len(json.dumps(c.text, ensure_ascii=True).encode()) <= MAX_CHUNK_SERIALIZED_BYTES for c in chunks))
        self.assertFalse(any(c.start_line <= 31 <= c.end_line for c in chunks))
        (self.ws / "a.py").write_text("".join(lines), encoding="utf-8")
        _, _, reasons = scan_candidates(self.ws, [], "tail")
        self.assertIn("long_source_line_omitted", reasons)

    def test_output_budget_skips_oversized_first_line(self):
        first = CandidateChunk("a.py", 1, 1, "x" * 400, "h", "f")
        second = CandidateChunk("b.py", 1, 1, "short\n", "h2", "f2")
        reasons = []
        result = deduplicate_and_bound_results([first, second], 2, 100, reasons)
        self.assertEqual([item["path"] for item in result], ["b.py"])
        self.assertIn("output_budget_truncated", reasons)

    def test_score_cache_evicts_oldest_and_expired(self):
        cache = ScoreCache(self.ws, cache_dir=self.ws / ".llm-output" / "jev-cache", ttl_seconds=100)
        cache.MAX_CACHE_BYTES = 300
        now = time.time()
        keys = [f"{i:064x}" for i in range(6)]
        cache._loaded = {key: {"noul": 0.5, "timestamp": now - 10 + i} for i, key in enumerate(keys[:-1])}
        cache._loaded[keys[0]]["timestamp"] = now - 1000
        cache.store_scores({keys[-1]: 0.9})
        reloaded = ScoreCache(self.ws, cache_dir=cache.cache_dir, ttl_seconds=100)
        self.assertEqual(reloaded.get_score(keys[-1]), 0.9)
        self.assertIsNone(reloaded.get_score(keys[0]))
        self.assertLessEqual(cache.cache_file.stat().st_size, 300)

    def test_inventory_excludes_framework_dirs_before_small_cap(self):
        (self.ws / ".git").mkdir()
        for i in range(20):
            (self.ws / ".git" / f"object{i}").write_text("many")
        (self.ws / "src.py").write_text("def target(): pass")
        with patch("jev_search.MAX_ENUMERATED_FILES", 2):
            files, _, reasons = enumerate_files(self.ws, [])
        self.assertIn(self.ws / "src.py", files)
        self.assertFalse(any(".git" in str(path) for path in files))

    def test_explicit_file_inventory_depth_one(self):
        (self.ws / "target.py").write_text("target")
        (self.ws / "nested").mkdir()
        for i in range(20):
            (self.ws / "nested" / f"f{i}.py").write_text("noise")
        with patch("jev_search._inventory_command", wraps=__import__("jev_search")._inventory_command) as inventory:
            files, _, _ = enumerate_files(self.ws, ["target.py"])
        self.assertEqual(files, [self.ws / "target.py"])
        self.assertIn("--max-depth", inventory.call_args.args[0])

    def test_new_secret_formats_and_dotfiles(self):
        from jev_search import is_file_excluded
        for name in (".npmrc", ".netrc", ".pypirc", ".git-credentials"):
            self.assertTrue(is_file_excluded(name))
        for secret in ("sk-proj-" + "A" * 25, "sk-ant-api03-" + "B" * 25,
                       "AKIA" + "C" * 16, "github_pat_" + "D" * 25,
                       "glpat-" + "E" * 20, "xoxb-" + "F" * 20):
            self.assertTrue(contains_literal_secret(secret.encode()))

    def test_unsupported_inventory_encoding_preserves_safe_files(self):
        safe = self.ws / "safe.py"
        safe.write_text("def target(): pass\n", encoding="utf-8")
        names = [os.fsencode(str(safe)), b"bad_\xff.py", b"bad_\xed\xb3\xbf.py"]
        with patch("jev_search.shutil.which", return_value="rg"), \
             patch("jev_search._inventory_command", return_value=(names, False, True)):
            result = run_inspect(self.ws, [])
            search = run_search(self.ws, "target")
        self.assertEqual(result["status"], "partial")
        self.assertIn("unsupported_path_encoding", result["coverage"]["reasons"])
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(search["status"], "unavailable")
        self.assertFalse(search["coverage"]["complete"])
        self.assertIn("unsupported_path_encoding", search["coverage"]["reasons"])
        self.assertEqual([item["path"] for item in search["results"]], ["safe.py"])

    @unittest.skipUnless(os.name == "posix" and shutil.which("rg"), "POSIX byte filenames and ripgrep required")
    def test_real_non_utf8_filename_is_skipped(self):
        (self.ws / "safe.py").write_text("def target(): pass\n", encoding="utf-8")
        try:
            with open(os.fsencode(self.ws) + b"/bad_\xff.py", "wb") as handle:
                handle.write(b"def unsupported(): pass\n")
        except OSError as exc:
            if exc.errno == errno.EILSEQ:
                self.skipTest("Filesystem requires valid UTF-8 filenames")
            raise
        result = run_search(self.ws, "target")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("unsupported_path_encoding", result["coverage"]["reasons"])
        self.assertEqual([item["path"] for item in result["results"]], ["safe.py"])

    @unittest.skipUnless(shutil.which("git"), "Git required for inventory fallback")
    def test_git_fallback_file_and_directory_scopes_are_literal_and_ignore_aware(self):
        import subprocess
        git = shutil.which("git")
        subprocess.run([git, "init", "--quiet", str(self.ws)], check=True)
        (self.ws / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        for directory in ("src[1]", "src1"):
            (self.ws / directory).mkdir()
        for name in ("tracked.py", "untracked.py", "file[1].py", "file1.py", "ignored.py", ".env"):
            (self.ws / "src[1]" / name).write_text("def target(): pass\n", encoding="utf-8")
        (self.ws / "src1" / "outside_scope.py").write_text("def other(): pass\n", encoding="utf-8")
        subprocess.run([git, "--literal-pathspecs", "-C", str(self.ws), "add", "--", "src[1]/tracked.py"], check=True)
        with patch("jev_search.shutil.which", side_effect=lambda name: git if name == "git" else None):
            for name in ("tracked.py", "untracked.py", "file[1].py"):
                with self.subTest(file=name):
                    files, _, reasons = enumerate_files(self.ws, ["src[1]/" + name])
                    self.assertEqual(files, [self.ws / "src[1]" / name])
                    self.assertEqual(reasons, [])
            files, _, reasons = enumerate_files(self.ws, ["src[1]"])
            self.assertEqual({p.name for p in files}, {"tracked.py", "untracked.py", "file[1].py", "file1.py"})
            self.assertEqual(reasons, [])
            files, _, reasons = enumerate_files(self.ws, ["src[1]/ignored.py"])
            self.assertEqual(files, [])
            self.assertIn("scope_file_ignored_or_unavailable", reasons)
            files, _, reasons = enumerate_files(self.ws, ["src[1]/.env"])
            self.assertEqual(files, [])
            self.assertIn("excluded_scope", reasons)


class TestInventoryAndChunkRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / ".llm-output" / "jev-smoke"
        self.ws.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(shutil.which("rg"), "ripgrep required for real inventory regression")
    def test_nested_excluded_ancestor_uses_workspace_cwd(self):
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "HEAD").write_text("secret")
        (self.ws / "src").mkdir()
        for name in ("a.py", "b.py", "c.py"):
            (self.ws / "src" / name).write_text("def ownership(): pass\n")
        (self.ws / "src" / ".env").write_text("password=plain")
        (self.ws / ".gitignore").write_text("ignored.py\n")
        (self.ws / "src" / "ignored.py").write_text("ignored")
        directory, _, _ = enumerate_files(self.ws, ["src"])
        self.assertEqual({p.name for p in directory}, {"a.py", "b.py", "c.py"})
        explicit, _, _ = enumerate_files(self.ws, ["src/a.py"])
        self.assertEqual(explicit, [self.ws / "src" / "a.py"])
        result = run_search(self.ws, "ownership")
        self.assertTrue(any(item["path"] == "src/a.py" for item in result["results"]))

    def test_rg_sort_path_and_single_deadline(self):
        (self.ws / "one").mkdir()
        (self.ws / "two").mkdir()
        (self.ws / "one" / "a.py").write_text("a")
        (self.ws / "two" / "b.py").write_text("b")
        calls = []
        from jev_search import _inventory_command
        def inventory(cmd, *args, **kwargs):
            calls.append((cmd, kwargs["deadline"], kwargs["cwd"]))
            return _inventory_command(cmd, *args, **kwargs)
        with patch("jev_search._inventory_command", side_effect=inventory):
            enumerate_files(self.ws, ["one", "two"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[0][2], self.ws)
        self.assertIn("--sort", calls[0][0])
        self.assertIn("path", calls[0][0])

    def test_shared_inventory_deadline_stops_later_scope(self):
        (self.ws / "one").mkdir()
        (self.ws / "two").mkdir()
        calls = []
        def inventory(cmd, *args, **kwargs):
            calls.append(cmd)
            time.sleep(0.02)
            return [], False, True
        with patch("jev_search.INVENTORY_DEADLINE_SECONDS", 0.01), \
             patch("jev_search._inventory_command", side_effect=inventory):
            _, _, reasons = enumerate_files(self.ws, ["one", "two"])
        self.assertEqual(len(calls), 1)
        self.assertIn("inventory_incomplete", reasons)

    def test_kill_wait_second_timeout_is_bounded(self):
        import subprocess
        import threading
        from jev_search import _inventory_command
        event = threading.Event()
        class Stream:
            def read(self, size):
                event.wait(0.1)
                return b""
            def close(self): pass
        class Process:
            stdout = Stream()
            returncode = None
            def poll(self): return None
            def kill(self): event.set()
            def wait(self, timeout): raise subprocess.TimeoutExpired("rg", timeout)
        with patch("jev_search.subprocess.Popen", return_value=Process()):
            paths, incomplete, valid = _inventory_command(["rg"], deadline=time.monotonic() + 0.01)
        self.assertEqual(paths, [])
        self.assertTrue(incomplete)
        self.assertFalse(valid)

    def test_scaled_overlap_preserves_other_candidates(self):
        from jev_search import MAX_CHUNK_SERIALIZED_BYTES
        lines = ["x" * 480 + "\n" for _ in range(300)]
        chunks = chunk_file_lines("long.md", lines, "filehash")
        self.assertLess(len(chunks), 40)
        self.assertTrue(all(len(json.dumps(c.text).encode()) <= MAX_CHUNK_SERIALIZED_BYTES for c in chunks))
        (self.ws / "long.md").write_text("".join(lines))
        (self.ws / "small.py").write_text("def ownership(): pass\n")
        candidates, _, reasons = scan_candidates(self.ws, [], "ownership", max_candidates=96)
        self.assertTrue(any(c.relpath == "small.py" for c in candidates))
        self.assertNotIn("max_candidates_reached", reasons)

    def test_timed_out_reader_cannot_mutate_returned_inventory(self):
        import threading
        from jev_search import _inventory_command
        release = threading.Event()
        closed = threading.Event()
        class Stream:
            def read(self, size):
                release.wait(5)
                return b"late.py\0"
            def close(self): closed.set()
        class Process:
            stdout = Stream()
            def kill(self): pass
            def wait(self, timeout): return 0
        try:
            started = time.monotonic()
            with patch("jev_search.subprocess.Popen", return_value=Process()), \
                 patch("jev_search.INVENTORY_CLEANUP_SECONDS", 0.03):
                paths, incomplete, valid = _inventory_command(["rg"], deadline=started + 0.01)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(paths, [])
            self.assertTrue(incomplete)
            self.assertFalse(valid)
        finally:
            release.set()
        self.assertTrue(closed.wait(1))
        self.assertEqual(paths, [])


class TestSearchCliGenericError(unittest.TestCase):
    def test_generic_error_has_command_and_nonzero(self):
        import io
        from contextlib import redirect_stdout
        from jev_search import main
        output = io.StringIO()
        with patch("sys.argv", ["jev_search", "search", "--workspace", ".", "--query", "x"]), \
             patch("jev_search._dispatch", side_effect=RuntimeError("secret internals")), redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                main()
        self.assertEqual(result.exception.code, 2)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["command"], "search")
        self.assertNotIn("secret internals", output.getvalue())
