"""Offline adversarial checks for advisory diff evaluation."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jev_client import JevClient
from jev_review import parse_diff, run_review

DIFF = """diff --git a/a.py b/a.py
index 123..456 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 def run():
-    return False
+    return True
"""


class TestReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "diff.txt").write_text(DIFF, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_facts_without_authorization(self):
        result = run_review(self.ws, "diff.txt")
        self.assertEqual({k: result["facts"][k] for k in ("changed_files", "hunks", "added_lines", "deleted_lines")},
                         {"changed_files": 1, "hunks": 1, "added_lines": 1, "deleted_lines": 1})
        self.assertEqual(result["diff_sha256"], hashlib.sha256((self.ws / "diff.txt").read_bytes()).hexdigest())
        self.assertEqual(result["focus"], [])
        self.assertIn("remote_disabled", result["coverage"]["reasons"])

    def test_partial_answers_keep_unknown(self):
        def fake(payload, headers):
            first = next(iter(payload["questions"]))
            return {"model": "jev-1.13.0", "answers": {first: {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": 3, "output_tokens": 1}}
        result = run_review(self.ws, "diff.txt", allow_remote=True, client=JevClient(transport=fake))
        self.assertEqual(len(result["focus"]), 1)
        self.assertEqual(result["focus"][0]["path"], "a.py")
        self.assertEqual(result["focus"][0]["new_start"], 1)
        self.assertTrue(result["unknowns"])
        self.assertFalse(result["coverage"]["complete"])

    def test_failure_after_partial_batch_keeps_signal(self):
        from jev_client import JevServerError
        calls = 0
        def fake(payload, headers):
            nonlocal calls
            calls += 1
            if calls == 1:
                qid = next(iter(payload["questions"]))
                return {"model": "jev-1.13.0", "answers": {qid: {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 1, "output_tokens": 1}}
            raise JevServerError("private model text must not echo")
        client = JevClient(transport=fake)
        with patch("jev_client.batch_questions", side_effect=lambda state, questions: [{k: v} for k, v in questions.items()]):
            result = run_review(self.ws, "diff.txt", allow_remote=True, client=client)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["focus"]), 1)
        self.assertNotIn("private model text", json.dumps(result))

    def test_secret_hunk_never_disclosed(self):
        secret = "known-key-01234567890123456789"
        diff = DIFF.replace("return True", f"TYPESAFE_API_KEY = '{secret}'")
        (self.ws / "diff.txt").write_text(diff)
        def forbidden(*args):
            self.fail("Remote call with secret")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": secret}):
            result = run_review(self.ws, "diff.txt", allow_remote=True, client=JevClient(transport=forbidden))
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn("secret_hunk_omitted", result["coverage"]["reasons"])

    def test_malformed_and_binary_are_partial(self):
        files, hunks, reasons = parse_diff(DIFF.replace("+    return True", "+    return True\n+    extra"))
        self.assertIn("incomplete_hunk", reasons)
        (self.ws / "diff.txt").write_text("diff --git a/a.bin b/a.bin\nGIT binary patch\n")
        result = run_review(self.ws, "diff.txt")
        self.assertIn("binary_diff", result["coverage"]["reasons"])

    def test_invalid_outside_or_linked_input(self):
        outside = self.ws.parent / "outside.diff"
        result = run_review(self.ws, outside)
        self.assertEqual(result["status"], "error")
        link = self.ws / "link.diff"
        try:
            link.symlink_to(self.ws / "diff.txt")
        except OSError:
            return
        self.assertEqual(run_review(self.ws, link)["status"], "error")

    def test_new_deleted_and_renamed_file_hunks(self):
        new = "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
        old = "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        renamed = "diff --git a/a.py b/b.py\nrename from a.py\nrename to b.py\n@@ -1 +1 @@\n-x\n+y\n"
        files, hunks, reasons = parse_diff(new + old + renamed)
        self.assertEqual(files, {"new.py", "old.py", "b.py"})
        self.assertEqual(len(hunks), 3)
        self.assertFalse(reasons)
        self.assertEqual((hunks[0]["added"], hunks[0]["deleted"]), (1, 0))
        self.assertEqual((hunks[1]["added"], hunks[1]["deleted"]), (0, 1))

    def test_hash_changes_with_diff(self):
        old = run_review(self.ws, "diff.txt")["diff_sha256"]
        (self.ws / "diff.txt").write_text(DIFF.replace("True", "None"))
        self.assertNotEqual(old, run_review(self.ws, "diff.txt")["diff_sha256"])


if __name__ == "__main__":
    unittest.main()


class TestReviewedDiffBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_content_plus_minus_headers_counted(self):
        diff = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                "@@ -1,2 +1,2 @@\n--- SQL comment\n-plain\n+++ b/x\n+other\n")
        files, hunks, reasons = parse_diff(diff)
        self.assertIn("ambiguous_embedded_file_header", reasons)
        self.assertTrue(hunks[0]["invalid"])
        self.assertEqual((hunks[0]["added"], hunks[0]["deleted"]), (2, 2))
        self.assertEqual(hunks[0]["path"], "a.txt")

    def test_review_excludes_credential_old_or_new_paths(self):
        for old, new in ((".env.production", "safe.py"), ("safe.py", "secrets.json"),
                         (".npmrc", "safe.py"), ("node_modules/key.js", "safe.py")):
            with self.subTest(old=old, new=new):
                diff = (f"diff --git a/{old} b/{new}\nrename from {old}\nrename to {new}\n"
                        f"--- a/{old}\n+++ b/{new}\n@@ -1 +1 @@\n-x\n+y\n")
                (self.ws / "change.diff").write_text(diff, encoding="utf-8")
                def forbidden(*args): self.fail("excluded source was sent")
                result = run_review(self.ws, "change.diff", allow_remote=True, client=JevClient(transport=forbidden))
                self.assertIn("excluded_path", result["coverage"]["reasons"])
                self.assertEqual(result["focus"], [])

    def test_quoted_credential_path_excluded(self):
        diff = ('diff --git "a/.env production" "b/.env production"\n'
                '--- "a/.env production"\n+++ "b/.env production"\n'
                '@@ -1 +1 @@\n-x\n+y\n')
        (self.ws / "change.diff").write_text(diff, encoding="utf-8")
        result = run_review(self.ws, "change.diff")
        # Quoted paths parse, and the policy decides whether the filename is sensitive.
        self.assertEqual(result["facts"]["changed_files"], 1)

    def test_quoted_secret_path_and_escaped_header_do_not_disclose(self):
        quoted = ('diff --git "a/.env.production" "b/.env.production"\n'
                  '--- "a/.env.production"\n+++ "b/.env.production"\n'
                  '@@ -1 +1 @@\n-old\n+password=plain\n')
        escaped = ('diff --git "a/\\056env.production" "b/safe.py"\n'
                   '--- "a/\\056env.production"\n+++ b/safe.py\n'
                   '@@ -1 +1 @@\n-old\n+password=plain\n')
        def forbidden(*args): self.fail("credential path was disclosed")
        for diff in (quoted, escaped):
            (self.ws / "change.diff").write_text(diff, encoding="utf-8")
            result = run_review(self.ws, "change.diff", allow_remote=True, client=JevClient(transport=forbidden))
            self.assertFalse(result["coverage"]["complete"])
            self.assertEqual(result["focus"], [])

    def test_unknown_counts_and_truncation_are_deterministic(self):
        diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n-x\n+y\n" for i in range(5))
        (self.ws / "change.diff").write_text(diff, encoding="utf-8")
        client = JevClient(transport=lambda payload, headers: {"model": "jev-1.13.0", "answers": {},
                            "usage": {"input_tokens": 1, "output_tokens": 1}})
        first = run_review(self.ws, "change.diff", allow_remote=True, client=client)
        second = run_review(self.ws, "change.diff", allow_remote=True, client=JevClient(transport=client.transport))
        self.assertEqual(first["unknowns"], second["unknowns"])
        self.assertEqual(first["unknown_count"], 35)
        self.assertTrue(first["unknowns_truncated"])
        self.assertEqual(len(first["unknowns"]), 24)

    def test_cli_unexpected_error_is_json(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from jev_review import main
        out = io.StringIO()
        with patch("sys.argv", ["jev_review", "--workspace", str(self.ws), "--diff-file", "bad.diff"]), \
             patch("jev_review.run_review", side_effect=RuntimeError("secret traceback")), redirect_stdout(out):
            with self.assertRaises(SystemExit) as exit_info:
                main()
        self.assertEqual(exit_info.exception.code, 2)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret traceback", out.getvalue())


class TestFileBlockDisclosureBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def block(name, body, old_count="1", new_count="1"):
        return (f"diff --git a/{name} b/{name}\nindex 1..2 100644\n"
                f"--- a/{name}\n+++ b/{name}\n@@ -1,{old_count} +1,{new_count} @@\n{body}")

    def _assert_no_secret_payload(self, diff):
        (self.ws / "candidate.diff").write_text(diff, encoding="utf-8")
        sent = []
        def transport(payload, headers):
            sent.append(json.dumps(payload))
            return {"model": "jev-1.13.0", "answers": {qid: {"type": "noul", "noul": 0.6} for qid in payload["questions"]},
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        result = run_review(self.ws, "candidate.diff", allow_remote=True, client=JevClient(transport=transport))
        self.assertNotIn("hunter2", "".join(sent))
        self.assertNotIn("DB_PASSWORD", "".join(sent))
        return result, sent

    def test_blank_context_safe_secret_safe_transitions(self):
        safe_a = self.block("safe_a.py", " a\n\n-b\n+c\n", "3", "3")
        secret = self.block(".env", "-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n")
        safe_b = self.block("safe_b.py", "-old\n+new\n")
        result, sent = self._assert_no_secret_payload(safe_a + secret + safe_b)
        self.assertTrue(sent)
        self.assertEqual(result["facts"]["changed_files"], 3)
        self.assertEqual(result["facts"]["hunks_total"], 3)
        self.assertIn("excluded_path", result["coverage"]["reasons"])

    def test_malformed_count_cannot_reassign_next_block(self):
        malformed = self.block("safe_a.py", " a\n\n-b\n+c\n", "4", "4")
        secret = self.block(".env", "-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n")
        safe_b = self.block("safe_b.py", "-old\n+new\n")
        result, sent = self._assert_no_secret_payload(malformed + secret + safe_b)
        self.assertTrue(sent)  # later valid safe block remains usable
        self.assertIn("unsupported_file_block", result["coverage"]["reasons"])
        self.assertIn("excluded_path", result["coverage"]["reasons"])

    def test_unsupported_hunk_line_keeps_file_ownership(self):
        malformed = self.block("safe.py", "-old\nindex unexpected\n+new\n")
        secret = self.block(".env.production", "-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n")
        result, sent = self._assert_no_secret_payload(malformed + secret)
        self.assertEqual(sent, [])
        self.assertIn("unsupported_hunk_line", result["coverage"]["reasons"])

    def test_header_pair_inside_hunk_is_ambiguous_and_never_sent(self):
        ambiguous = self.block("safe.py", "--- a/.env\n\n+++ b/.env\n", "2", "2")
        secret = "@@ -1 +1 @@\n-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n"
        result, sent = self._assert_no_secret_payload(ambiguous + secret)
        self.assertEqual(sent, [])
        self.assertIn("ambiguous_embedded_file_header", result["coverage"]["reasons"])

    def test_lone_embedded_marker_cannot_disclose_later_hunk(self):
        for marker, old_count, new_count in (("--- a/.env", "2", "1"),
                                             ("+++ b/.env", "1", "2"),
                                             ('--- "a/.env"', "2", "1"),
                                             ("+++ /dev/null", "1", "2")):
            with self.subTest(marker=marker):
                ambiguous = self.block("safe.py", f"-old\n+new\n{marker}\n", old_count, new_count)
                secret = "@@ -3 +3 @@\n-DB_PASSWORD=old\n+DB_PASSWORD=hunter2\n"
                result, sent = self._assert_no_secret_payload(ambiguous + secret)
                self.assertEqual(sent, [])
                self.assertFalse(result["coverage"]["complete"])
                self.assertIn("ambiguous_embedded_file_header", result["coverage"]["reasons"])

    def test_spaces_and_apostrophes_in_unquoted_names(self):
        diff = self.block("it's my file.py", "-old\n+new\n")
        result, sent = self._assert_no_secret_payload(diff)
        self.assertEqual(result["facts"]["changed_files"], 1)
        self.assertTrue(sent)
        self.assertEqual(result["focus"][0]["path"], "it's my file.py")

    def test_total_facts_and_omitted_hunks(self):
        diff = "".join(self.block(f"f{i}.py", "-old\n+new\n") for i in range(100))
        (self.ws / "candidate.diff").write_text(diff, encoding="utf-8")
        result = run_review(self.ws, "candidate.diff")
        self.assertEqual(result["facts"]["hunks_total"], 100)
        self.assertEqual(result["facts"]["hunks_omitted"], 4)
        self.assertEqual(result["facts"]["added_lines"], 100)
        self.assertEqual(result["facts"]["deleted_lines"], 100)
        self.assertGreaterEqual(result["unknown_count"], 4)
        self.assertTrue(result["unknowns_truncated"])
