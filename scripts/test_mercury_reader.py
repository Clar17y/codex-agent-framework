#!/usr/bin/env python3
"""Tests for bounded Mercury 3 corpus reader."""

import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from scripts.mercury_reader import (
    canonicalize_root,
    is_path_excluded,
    is_credential_path,
    is_binary_file,
    is_symlink_or_reparse,
    list_files,
    read_file,
    resolve_confined_path,
    search,
    main,
    LISTINGS_MAX_PATHS,
    SEARCH_MAX_MATCHES,
    SEARCH_MAX_CHARS,
    READ_MAX_LINES,
    READ_MAX_CHARS,
)


class TestMercuryReader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

        # Regular tree
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print('hello world')\nfoo = 1\n", encoding="utf-8")
        (self.root / "src" / "utils.py").write_text("def helper():\n    return 'HELP'\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Project\nSample repository\n", encoding="utf-8")

        # Excluded directories (.git, .llm-output, output, vendor)
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("git config content", encoding="utf-8")
        (self.root / ".llm-output").mkdir()
        (self.root / ".llm-output" / "trace.json").write_text("should be hidden", encoding="utf-8")
        (self.root / "output").mkdir()
        (self.root / "output" / "build.log").write_text("build log content", encoding="utf-8")
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "dep.py").write_text("vendor content", encoding="utf-8")
        (self.root / ".worktrees").mkdir()
        (self.root / ".worktrees" / "branch.txt").write_text("worktree content", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("node_modules content", encoding="utf-8")

        # Credential files
        (self.root / ".env").write_text("SECRET=123", encoding="utf-8")
        (self.root / ".env.local").write_text("LOCAL_SECRET=abc", encoding="utf-8")
        (self.root / "id_rsa").write_text("ssh-rsa ...", encoding="utf-8")
        (self.root / "server.key").write_text("private key", encoding="utf-8")
        (self.root / "cert.pem").write_text("cert", encoding="utf-8")
        (self.root / "secrets.json").write_text('{"token": "xyz"}', encoding="utf-8")

        # Binary file
        (self.root / "binary.dat").write_bytes(b"\x00\x01\x02\x03hello\x00world")
        (self.root / "app.exe").write_bytes(b"MZ\x90\x00fake_binary")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_files_basic_and_filters_exclusions(self):
        result = list_files(self.root)
        paths = result["paths"]
        self.assertIn("README.md", paths)
        self.assertIn("src/main.py", paths)
        self.assertIn("src/utils.py", paths)

        # Excluded directories must NOT appear
        for p in paths:
            parts = p.split("/")
            self.assertNotIn(".git", parts)
            self.assertNotIn(".llm-output", parts)
            self.assertNotIn("output", parts)
            self.assertNotIn("vendor", parts)
            self.assertNotIn(".worktrees", parts)
            self.assertNotIn("node_modules", parts)

        # Credential files must NOT appear
        self.assertNotIn(".env", paths)
        self.assertNotIn(".env.local", paths)
        self.assertNotIn("id_rsa", paths)
        self.assertNotIn("server.key", paths)
        self.assertNotIn("cert.pem", paths)
        self.assertNotIn("secrets.json", paths)

    def test_list_files_prefix_filter(self):
        p_res = list_files(self.root, prefix="src/")
        self.assertEqual(p_res["paths"], ["src/main.py", "src/utils.py"])

    def test_list_files_limit_and_disclosure(self):
        big_dir = self.root / "many"
        big_dir.mkdir()
        for i in range(160):
            (big_dir / f"file_{i:03d}.txt").write_text("content", encoding="utf-8")

        result = list_files(self.root, prefix="many/")
        self.assertEqual(len(result["paths"]), LISTINGS_MAX_PATHS)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["total"])
        self.assertIn("total unknown", result["text"])

    def test_search_case_insensitive_and_glob(self):
        res1 = search(self.root, "HELLO")
        self.assertEqual(res1["count"], 1)
        self.assertEqual(res1["matches"][0]["path"], "src/main.py")

        res2 = search(self.root, "sample", glob_pattern="*.md")
        self.assertEqual(res2["count"], 1)
        self.assertEqual(res2["matches"][0]["path"], "README.md")

        res3 = search(self.root, "sample", glob_pattern="*.py")
        self.assertEqual(res3["count"], 0)

    def test_search_skips_binary_and_credentials_and_exclusions(self):
        # Search for content in binary, credential, or excluded dirs
        res_secret = search(self.root, "SECRET")
        self.assertEqual(res_secret["count"], 0)

        res_bin = search(self.root, "hello")
        paths = [m["path"] for m in res_bin["matches"]]
        self.assertIn("src/main.py", paths)
        self.assertNotIn("binary.dat", paths)

        res_vendor = search(self.root, "vendor")
        self.assertEqual(res_vendor["count"], 0)
        res_worktrees = search(self.root, "worktree")
        self.assertEqual(res_worktrees["count"], 0)
        res_nm = search(self.root, "node_modules")
        self.assertEqual(res_nm["count"], 0)

    def test_search_match_limit_and_disclosure(self):
        match_file = self.root / "matches.txt"
        match_file.write_text("\n".join(f"needle line {i}" for i in range(100)), encoding="utf-8")

        res = search(self.root, "needle", limit=SEARCH_MAX_MATCHES)
        self.assertEqual(res["count"], SEARCH_MAX_MATCHES)
        self.assertTrue(res["truncated"])
        self.assertIn("Truncated", res["text"])

    def test_search_char_limit(self):
        match_file = self.root / "wide.txt"
        wide_line = "needle " + ("X" * 1000)
        match_file.write_text("\n".join([wide_line] * 30), encoding="utf-8")

        res = search(self.root, "needle")
        self.assertTrue(res["truncated"])
        self.assertLess(res["count"], 30)
        self.assertIn(f"character limit of {SEARCH_MAX_CHARS} chars", res["text"])

    def test_search_empty_query_raises(self):
        with self.assertRaises(ValueError):
            search(self.root, "")

    def test_read_file_line_numbering_and_ranges(self):
        content = "\n".join(f"line {i}" for i in range(1, 51))
        (self.root / "lines.txt").write_text(content, encoding="utf-8")

        res = read_file(self.root, "lines.txt", start=5, end=10)
        self.assertEqual(res["start"], 5)
        self.assertEqual(res["end"], 10)
        self.assertFalse(res["truncated"])
        self.assertIn("    5 | line 5", res["text"])
        self.assertIn("   10 | line 10", res["text"])

    def test_read_file_empty(self):
        (self.root / "empty.txt").write_text("", encoding="utf-8")
        res = read_file(self.root, "empty.txt")
        self.assertEqual(res["total_lines"], 0)
        self.assertEqual(res["text"], "[Empty file]")

    def test_read_file_invalid_ranges(self):
        (self.root / "small.txt").write_text("a\nb\nc\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_file(self.root, "small.txt", start=10, end=12)
        with self.assertRaises(ValueError):
            read_file(self.root, "small.txt", start=5, end=2)

    def test_read_file_max_lines_limit(self):
        content = "\n".join(f"line {i}" for i in range(1, 300))
        (self.root / "long.txt").write_text(content, encoding="utf-8")

        res = read_file(self.root, "long.txt", start=1, end=250)
        self.assertEqual(res["start"], 1)
        self.assertEqual(res["end"], READ_MAX_LINES)
        self.assertTrue(res["truncated"])
        self.assertTrue(res["line_truncated"])
        self.assertIsNone(res["total_lines"])
        self.assertIn("total unknown", res["text"])

    def test_read_file_max_chars_limit(self):
        content = "\n".join([f"line_{i:02d}_" + ("A" * 600) for i in range(1, 51)])
        (self.root / "fat.txt").write_text(content, encoding="utf-8")

        res = read_file(self.root, "fat.txt", start=1, end=50)
        self.assertTrue(res["truncated"])
        self.assertTrue(res["char_truncated"])
        self.assertIn("character limit", res["text"])

    def test_read_file_rejects_binary_files(self):
        with self.assertRaises(ValueError) as ctx:
            read_file(self.root, "binary.dat")
        self.assertIn("binary file", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx:
            read_file(self.root, "app.exe")
        self.assertIn("binary file", str(ctx.exception).lower())

    def test_read_file_rejects_credential_files(self):
        for cred_file in (".env", ".env.local", "id_rsa", "server.key", "cert.pem", "secrets.json"):
            with self.subTest(file=cred_file):
                with self.assertRaises(ValueError) as ctx:
                    read_file(self.root, cred_file)
                self.assertIn("credential", str(ctx.exception).lower())

    def test_read_file_rejects_excluded_directories(self):
        for ex_path in (".git/config", ".llm-output/trace.json", "output/build.log", "vendor/dep.py", ".worktrees/branch.txt", "node_modules/dep.js"):
            with self.subTest(path=ex_path):
                with self.assertRaises(ValueError) as ctx:
                    read_file(self.root, ex_path)
                self.assertIn("excluded directory", str(ctx.exception).lower())

    def test_path_escapes_rejected(self):
        with self.assertRaises(ValueError):
            resolve_confined_path(self.root, "../outside.txt")
        with self.assertRaises(ValueError):
            resolve_confined_path(self.root, "src/../../outside.txt")

    def test_symlink_escape_rejected(self):
        outside_dir = tempfile.TemporaryDirectory()
        try:
            outside_file = Path(outside_dir.name) / "secret.txt"
            outside_file.write_text("external confidential", encoding="utf-8")

            link_path = self.root / "leak_link.txt"
            try:
                os.symlink(outside_file, link_path)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not permitted in this environment")

            files = list_files(self.root)["paths"]
            self.assertNotIn("leak_link.txt", files)

            with self.assertRaises(ValueError):
                read_file(self.root, "leak_link.txt")
        finally:
            outside_dir.cleanup()

    def test_hardlink_escape_rejected(self):
        # Create a hardlink within root
        source_file = self.root / "src" / "main.py"
        hardlink_file = self.root / "main_link.py"
        try:
            os.link(source_file, hardlink_file)
        except (OSError, NotImplementedError):
            self.skipTest("Hardlinks not supported in this environment")

        # Multi-link file must be blocked
        files = list_files(self.root)["paths"]
        self.assertNotIn("main_link.py", files)
        self.assertNotIn("src/main.py", files)

        with self.assertRaises(ValueError) as ctx:
            read_file(self.root, "main_link.py")
        self.assertIn("Hardlink", str(ctx.exception))

    def test_cli_execution(self):
        saved_stdout = sys.stdout
        try:
            out = io.StringIO()
            sys.stdout = out
            code = main(["--root", str(self.root), "list_files"])
            self.assertEqual(code, 0)
            self.assertIn("README.md", out.getvalue())

            out = io.StringIO()
            sys.stdout = out
            code = main(["--root", str(self.root), "search", "hello"])
            self.assertEqual(code, 0)
            self.assertIn("src/main.py:1:", out.getvalue())

            out = io.StringIO()
            sys.stdout = out
            code = main(["--root", str(self.root), "read_file", "README.md", "1", "2"])
            self.assertEqual(code, 0)
            self.assertIn("    1 | # Project", out.getvalue())
        finally:
            sys.stdout = saved_stdout

    def test_multiline_credentials_filtered_before_range_and_search(self):
        from scripts.mercury_reader import redact_text
        cases = [
            'api_key: |\n  PRIVATE_FIXTURE_FIRST\n  PRIVATE_FIXTURE_SECOND\npublic: visible\n',
            '  access_token: >-\n    PRIVATE_FIXTURE_FIRST\n    PRIVATE_FIXTURE_SECOND\n  public: visible\n',
            'api_key = """PRIVATE_FIXTURE_FIRST\nPRIVATE_FIXTURE_SECOND\n"""\npublic = "visible"\n',
            'api_key = """PRIVATE_FIXTURE_FIRST\\"""\nPRIVATE_FIXTURE_SECOND\n"""\npublic = "visible"\n',
            "api_key = r'''PRIVATE_FIXTURE_FIRST\nPRIVATE_FIXTURE_SECOND\n'''\npublic = 'visible'\n",
            'api_key = """PRIVATE_FIXTURE_FIRST\nPRIVATE_FIXTURE_SECOND\n',
        ]
        for source in cases:
            with self.subTest(source=source.splitlines()[0]):
                path = self.root / 'config.yaml'
                path.write_text(source, encoding='utf8')
                sanitized = redact_text(source)
                self.assertNotIn('PRIVATE_FIXTURE', sanitized)
                self.assertEqual(sanitized.count('\n'), source.count('\n'))
                partial = read_file(self.root, 'config.yaml', start=2, end=2)
                self.assertNotIn('PRIVATE_FIXTURE', partial['text'])
                self.assertIn('2 |', partial['text'])
                self.assertEqual(search(self.root, 'PRIVATE_FIXTURE')['count'], 0)
                if 'visible' in source:
                    self.assertIn('visible', read_file(self.root, 'config.yaml')['text'])

    def test_next_line_and_continued_quoted_credentials_before_selection(self):
        from scripts.mercury_reader import redact_text
        cases = [
            'password: /* comment\n */\n \"OPAQUE_FIRST\"\npublic: visible\n',
            'password:\n /* comment\n */ \"OPAQUE_FIRST\"\npublic: visible\n',
            '\"password\":\n  \"OPAQUE_FIRST\",\npublic: visible\n',
            'password: # value follows\n\n# comment\n  \"OPAQUE_FIRST\"\npublic: visible\n',
            'password = \"OPAQUE_FIRST\\\nOPAQUE_SECOND\"; public = \"visible\"\n',
            "password = 'OPAQUE_FIRST\\\nOPAQUE_SECOND'; public = 'visible'\n",
            'password:\n  \"OPAQUE_FIRST\\\"still-hidden\\\nOPAQUE_SECOND\"\npublic: visible\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                (self.root / 'config.txt').write_text(source, encoding='utf8')
                sanitized = redact_text(source)
                self.assertNotIn('OPAQUE_', sanitized)
                self.assertEqual(sanitized.count('\n'), source.count('\n'))
                for number in range(1, len(source.splitlines()) + 1):
                    partial = read_file(self.root, 'config.txt', start=number, end=number)
                    self.assertNotIn('OPAQUE_', partial['text'])
                    self.assertIn(f'{number} |', partial['text'])
                self.assertEqual(search(self.root, 'OPAQUE_', 'config.txt')['count'], 0)
                self.assertIn('visible', read_file(self.root, 'config.txt')['text'])
        for source in ('password:\npublic: visible\n', 'password:\n}\npublic: visible\n'):
            self.assertIn('visible', redact_text(source))

    def test_quoted_credential_logical_value_boundaries(self):
        from scripts.mercury_reader import redact_text
        cases = [
            "password: 'prefix''\n  OPAQUE_VALUE'\npublic: visible\n",
            "password:\n  'prefix''''\n  OPAQUE_VALUE'''\npublic: visible\n",
            "password: '''prefix\n  OPAQUE_VALUE'\npublic: visible\n",
            "password: 'OPAQUE_VALUE\\'\npublic: visible\n",
            "password: \"prefix\\\"\n  OPAQUE_VALUE\"\npublic: visible\n",
            "password = 'prefix' 'OPAQUE_VALUE'\npublic = 'visible'\n",
            "password = 'prefix' \\\n  r'OPAQUE_VALUE'\npublic = 'visible'\n",
            "password = r'prefix\\'still hidden\nOPAQUE_VALUE'\npublic = 'visible'\n",
            "password: [\n 'prefix'''' ] /* not a comment */\n OPAQUE_VALUE',\n 'tail'\n]\npublic: visible\n",
            "password = (\n 'prefix' # comment with quote '\n r'OPAQUE_VALUE'\n)\npublic = 'visible'\n",
        ]
        cases.append("password = 'prefix'" + " 'OPAQUE_VALUE'" * 800 + "\npublic = 'visible'\n")
        for pairs in range(1, 5):
            escaped = "''" * pairs
            cases.append("password: 'prefix" + escaped + "\n  OPAQUE_VALUE" + escaped + "'\npublic: visible\n")
            cases.append("password:\n  'prefix" + escaped + "\n  OPAQUE_VALUE" + escaped + "'\npublic: visible\n")
        for source in cases:
            with self.subTest(source=source):
                (self.root / 'config.yaml').write_text(source, encoding='utf-8')
                sanitized = redact_text(source)
                self.assertNotIn('OPAQUE_VALUE', sanitized)
                self.assertEqual(sanitized.count('\n'), source.count('\n'))
                for number in range(1, len(source.splitlines()) + 1):
                    partial = read_file(self.root, 'config.yaml', start=number, end=number)['text']
                    self.assertNotIn('OPAQUE_VALUE', partial)
                    self.assertIn(f'{number} |', partial)
                self.assertEqual(search(self.root, 'OPAQUE_VALUE', 'config.yaml')['count'], 0)
                self.assertIn('visible', read_file(self.root, 'config.yaml')['text'])

    def test_detached_plain_credential_logical_boundaries(self):
        from scripts.mercury_reader import redact_text
        cases = [
            'database_password:\n  lantern-7431\npublic: visible\napi_token:\n  \"kite-8294\"\nnext: usable\n',
            '- database_password:\n    lantern-7431\n    kite-8294\n- public: visible\n',
            'database_password: lantern-7431\n  kite-8294\npublic: visible\n',
            '- database_password:\n    lantern-7431\n    kite-8294\n  public: visible\n',
            'database_password:\n  proto://lantern-7431@host/db\npublic: visible\n',
            'database_password:\n  lantern-7431\n  kite-8294\npublic: visible\n',
            'database_password: # pending\n\n# comment\n  lantern-7431:tail=value\n\n# another comment\n  kite-8294=value:tail\npublic: visible\n',
            'database_password:\n  nested:\n    lantern-7431\n    kite-8294\npublic: visible\n',
            'service:\n  database_password:\n    nested: lantern-7431\n    another: kite-8294\n  public: visible\nouter: usable\n',
            'service:\n  database_password:\n    proto://lantern-7431@host/db\n    kite-8294=value\n  public: visible\nouter: usable\n',
            'database_password: /* comment\n */\n  lantern-7431\n  kite-8294\npublic: visible\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                (self.root / 'config.yaml').write_text(source)
                sanitized = redact_text(source)
                self.assertEqual(sanitized.count('\n'), source.count('\n'))
                for token in ('lantern-7431', 'kite-8294'):
                    self.assertNotIn(token, sanitized)
                    self.assertEqual(search(self.root, token, 'config.yaml')['count'], 0)
                for number in range(1, len(source.splitlines()) + 1):
                    partial = read_file(self.root, 'config.yaml', start=number, end=number)['text']
                    self.assertIn(f'{number} |', partial)
                    self.assertNotIn('lantern-7431', partial)
                    self.assertNotIn('kite-8294', partial)
                self.assertIn('public: visible', read_file(self.root, 'config.yaml')['text'])
                if 'outer: usable' in source:
                    self.assertIn('outer: usable', read_file(self.root, 'config.yaml')['text'])
        for source in ('database_password:\npublic: visible\n',
                       'service:\n  database_password:\n  public: visible\n',
                       'database_password:\n  lantern-7431\n---\npublic: visible\n'):
            self.assertIn('public: visible', redact_text(source))

    def test_compound_credential_values_before_selection(self):
        from scripts.mercury_reader import redact_text
        for source in (
            'password:\n  |\n    OPAQUE_FIRST\n    OPAQUE_SECOND\npublic: visible\n',
            'password:\n[\n "OPAQUE_FIRST",\n {"nested": "OPAQUE_SECOND]"} // ]\n]\npublic: visible\n',
            'password = {\n/* } comment\n */ "nested": [\n "OPAQUE_FIRST", "OPAQUE_SECOND"\n]\n}; public = "visible"\n',
            'password = (\n "OPAQUE_FIRST" # )\n "OPAQUE_SECOND"\n)\npublic = "visible"\n',
            'password:\n "OPAQUE_FIRST:password=OPAQUE_SECOND"\npublic: visible\n',
        ):
            with self.subTest(source=source):
                (self.root / 'config.txt').write_text(source)
                self.assertNotIn('OPAQUE_', redact_text(source))
                self.assertEqual(redact_text(source).count('\n'), source.count('\n'))
                for number in range(1, len(source.splitlines()) + 1):
                    self.assertNotIn('OPAQUE_', read_file(self.root, 'config.txt', start=number, end=number)['text'])
                self.assertEqual(search(self.root, 'OPAQUE_', 'config.txt')['count'], 0)
                self.assertIn('visible', read_file(self.root, 'config.txt')['text'])
        (self.root / 'config.txt').write_text('password = ' + '[' * 65 + '\nOPAQUE_FIRST')
        with self.assertRaisesRegex(ValueError, 'nesting ceiling'):
            read_file(self.root, 'config.txt')

    def test_backslash_paths_normalize_before_confinement(self):
        expected = read_file(self.root, 'src/main.py')['text']
        self.assertEqual(read_file(self.root, 'src\\main.py')['text'], expected)
        for path in ('..\\outside.py', 'src\\..\\README.md', 'node_modules\\dep.js'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_file(self.root, path)

    def test_nofollow_errors_are_controlled_path_rejections(self):
        import errno
        from unittest.mock import patch
        import scripts.mercury_reader as reader
        for error in (errno.ELOOP, errno.ENOTDIR):
            with self.subTest(error=error), patch.object(reader.os, 'open', side_effect=OSError(error, 'blocked')):
                with self.assertRaisesRegex(ValueError, 'Symlink|directory'):
                    reader._nofollow_open('fixture', 0)
        with patch.object(reader.os, 'open', side_effect=PermissionError(errno.EACCES, 'denied')):
            with self.assertRaises(PermissionError):
                reader._nofollow_open('fixture', 0)

    def test_resource_caps_before_large_reads_and_incremental_stop(self):
        from unittest.mock import patch
        import scripts.mercury_reader as reader
        huge = self.root / 'huge.txt'
        huge.write_bytes(b'x' * 1024)
        with patch.object(reader, 'FILE_MAX_BYTES', 100):
            with self.assertRaisesRegex(reader.ReaderLimitError, 'File size'):
                read_file(self.root, 'huge.txt')
        huge.unlink()
        (self.root / 'wide.txt').write_text('x' * 100)
        with patch.object(reader, 'LINE_MAX_BYTES', 20):
            with self.assertRaisesRegex(reader.ReaderLimitError, 'Line size'):
                read_file(self.root, 'wide.txt')
        with patch.object(reader, 'SCAN_MAX_ENTRIES', 1):
            with self.assertRaises(reader.ReaderLimitError):
                list_files(self.root)
        with patch.object(reader, 'SCAN_MAX_BYTES', 1):
            with self.assertRaises(reader.ReaderLimitError):
                search(self.root, 'missing')
        with patch.object(reader, 'SCAN_MAX_LINES', 1):
            with self.assertRaises(reader.ReaderLimitError):
                read_file(self.root, 'src/main.py', start=2)
        # The walker fails if asked for a second file: output caps stop traversal.
        def candidates(*args):
            yield self.root / 'src/main.py', Path('src/main.py')
            raise AssertionError('Scanned after match cap')
        with patch.object(reader, '_safe_walk', candidates):
            self.assertEqual(search(self.root, 'hello', limit=1)['count'], 1)
        with patch.object(reader.time, 'monotonic', return_value=10):
            for action in (lambda: list_files(self.root, deadline=9),
                           lambda: search(self.root, 'a', deadline=9),
                           lambda: read_file(self.root, 'README.md', deadline=9)):
                with self.assertRaises(TimeoutError):
                    action()

    def test_special_windows_paths_rejected_before_open(self):
        from unittest.mock import patch
        import scripts.mercury_reader as reader
        with patch.object(reader, '_open_file') as opened:
            for path in ('src/main.py:stream', 'C:relative.txt', 'C:/outside.txt',
                         '//server/share/file', '\\\\?\\C:\\data', 'NUL', 'aux.txt',
                         'COM1.txt', 'COM\u00b9.txt', 'CON .txt', 'src/main.py.', 'src/main.py '):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    read_file(self.root, path)
            opened.assert_not_called()

    def test_open_descriptor_hardlink_metadata_rejected_before_read(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import scripts.mercury_reader as reader
        original = os.fstat
        def linked(fd):
            st = original(fd)
            return SimpleNamespace(st_mode=st.st_mode, st_nlink=2, st_size=st.st_size)
        with patch.object(reader.os, 'fstat', side_effect=linked):
            for action in (lambda: read_file(self.root, 'src/main.py'),
                           lambda: reader.resolve_confined_path(self.root, 'src/main.py')):
                with self.assertRaisesRegex(ValueError, 'Hardlink'):
                    action()
            self.assertEqual(search(self.root, 'hello')['count'], 0)
            self.assertEqual(list_files(self.root)['paths'], [])

    def test_combined_symlink_hardlink_escape_blocked(self):
        outside = self.root.parent / (self.root.name + '-outside.txt')
        outside.write_text('outside boundary marker')
        self.addCleanup(outside.unlink, missing_ok=True)
        inside = self.root / 'inside.txt'
        try:
            os.link(outside, inside)
        except (OSError, NotImplementedError):
            self.skipTest('Hardlink creation denied by host; descriptor checks tested separately')
        alias = self.root / 'alias.txt'
        try:
            os.symlink(inside, alias)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink privilege unavailable; hardlinks tested separately')
        for path in ('alias.txt', 'inside.txt'):
            with self.assertRaises((ValueError, OSError)):
                read_file(self.root, path)
            self.assertNotIn(path, list_files(self.root)['paths'])
        self.assertEqual(search(self.root, 'outside boundary marker')['count'], 0)

    def test_parent_replacement_is_rejected_or_keeps_opened_parent(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import scripts.mercury_reader as reader
        outside = self.root / 'other'
        outside.mkdir()
        (outside / 'main.py').write_text('replacement marker')
        # Windows junctions do not need symbolic-link privilege.
        if os.name == 'nt':
            import subprocess
            junction = self.root / 'junction'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                                    capture_output=True)
            if result.returncode:
                self.skipTest('Junction creation unavailable')
            with self.assertRaises((ValueError, OSError)):
                read_file(self.root, 'junction/main.py')
            original = reader._windows_handle
            attempts = []
            @contextmanager
            def replace_after_lock(path, directory=False):
                with original(path, directory) as handle:
                    if Path(path) == self.root / 'src' and directory:
                        try:
                            (self.root / 'src').rename(self.root / 'moved')
                        except OSError:
                            attempts.append('locked')
                        else:
                            self.fail('Locked parent was replaceable')
                    yield handle
            with patch.object(reader, '_windows_handle', replace_after_lock):
                self.assertIn('hello', read_file(self.root, 'src/main.py')['text'])
            self.assertEqual(attempts, ['locked'])
        else:
            original = reader._directory
            @contextmanager
            def replace_after_open(root, parts=()):
                with original(root, parts) as fd:
                    if tuple(parts) == ('src',):
                        (self.root / 'src').rename(self.root / 'moved')
                        os.symlink(outside, self.root / 'src', target_is_directory=True)
                    yield fd
            with patch.object(reader, '_directory', replace_after_open):
                result = read_file(self.root, 'src/main.py')
            self.assertIn('hello', result['text'])
            self.assertNotIn('replacement marker', result['text'])


    def test_excluded_worktrees_and_node_modules_nested_and_case_semantics(self):
        # Nested excluded directories inside source
        (self.root / "src" / ".worktrees").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / ".worktrees" / "nested.txt").write_text("nested worktree secret", encoding="utf-8")
        (self.root / "src" / "node_modules").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "node_modules" / "dep.js").write_text("nested node_modules dep", encoding="utf-8")

        # Must not appear in list_files
        paths = list_files(self.root)["paths"]
        for p in paths:
            parts = p.split("/")
            self.assertNotIn(".worktrees", parts)
            self.assertNotIn("node_modules", parts)

        # Must not be matched in search
        self.assertEqual(search(self.root, "nested worktree")["count"], 0)
        self.assertEqual(search(self.root, "nested node_modules")["count"], 0)

        # Direct reads must be rejected with ValueError
        for ex in (
            ".worktrees/branch.txt",
            "node_modules/dep.js",
            "src/.worktrees/nested.txt",
            "src/node_modules/dep.js",
            "NODE_MODULES/dep.js",
            ".WORKTREES/branch.txt",
            "node_modules\\dep.js",
        ):
            with self.subTest(path=ex):
                with self.assertRaises(ValueError) as ctx:
                    read_file(self.root, ex)
                self.assertIn("excluded directory", str(ctx.exception).lower())

        with self.assertRaises(ValueError):
            resolve_confined_path(self.root, "src/node_modules/dep.js")

    def test_narrow_scopes_prune_unrelated_subtrees_under_small_budget(self):
        from unittest.mock import patch
        import scripts.mercury_reader as reader

        # Create a large unrelated sibling tree (40 files)
        other_dir = self.root / "other_tree"
        other_dir.mkdir(parents=True, exist_ok=True)
        for i in range(40):
            (other_dir / f"file_{i:03d}.txt").write_text(f"unrelated sibling {i}", encoding="utf-8")

        # Create target subtrees
        target_src = self.root / "apps" / "api" / "src"
        target_src.mkdir(parents=True, exist_ok=True)
        (target_src / "index.ts").write_text("export const target_needle = 1;\n", encoding="utf-8")
        (target_src / "server.ts").write_text("import './index';\n", encoding="utf-8")
        (target_src / "util.ts").write_text("export function help() {}\n", encoding="utf-8")
        (self.root / "apps" / "api" / "api.json").write_text('{"name": "api"}', encoding="utf-8")

        # Deliberately small scan budget of 25 entries
        with patch.object(reader, 'SCAN_MAX_ENTRIES', 25):
            # Broad listing of entire root must fail due to >40 entries in other_tree
            with self.assertRaises(reader.ReaderLimitError):
                list_files(self.root)

            # Directory prefix: only scans apps/api/src
            res_dir = list_files(self.root, prefix="apps/api/src")
            self.assertEqual(res_dir["paths"], [
                "apps/api/src/index.ts",
                "apps/api/src/server.ts",
                "apps/api/src/util.ts",
            ])
            self.assertFalse(res_dir["truncated"])

            # Partial prefix: matches apps/api/api.json and apps/api/src/...
            res_partial = list_files(self.root, prefix="apps/ap")
            self.assertEqual(res_partial["paths"], [
                "apps/api/api.json",
                "apps/api/src/index.ts",
                "apps/api/src/server.ts",
                "apps/api/src/util.ts",
            ])

            # Exact file prefix
            res_exact = list_files(self.root, prefix="apps/api/src/index.ts")
            self.assertEqual(res_exact["paths"], ["apps/api/src/index.ts"])

            # Slash/backslash normalization
            res_bs = list_files(self.root, prefix="apps\\api\\src")
            self.assertEqual(res_bs["paths"], [
                "apps/api/src/index.ts",
                "apps/api/src/server.ts",
                "apps/api/src/util.ts",
            ])

            # Specific glob search
            res_search = search(self.root, query="target_needle", glob_pattern="apps/api/**")
            self.assertEqual(res_search["count"], 1)
            self.assertEqual(res_search["matches"][0]["path"], "apps/api/src/index.ts")

            # Literal glob search
            res_lit = search(self.root, query="target_needle", glob_pattern="apps/api/src/index.ts")
            self.assertEqual(res_lit["count"], 1)

            # Wildcard directory glob search
            res_wild = search(self.root, query="target_needle", glob_pattern="apps/*/src/**")
            self.assertEqual(res_wild["count"], 1)

        # Continued relevant-corpus budget enforcement:
        # If the target subtree itself exceeds SCAN_MAX_ENTRIES, it must still raise ReaderLimitError
        with patch.object(reader, 'SCAN_MAX_ENTRIES', 2):
            with self.assertRaises(reader.ReaderLimitError):
                list_files(self.root, prefix="apps/api/src")

    def test_invalid_outside_and_excluded_scopes_fail_safely(self):
        # Invalid prefix for list_files
        for bad_prefix in (
            "../outside",
            "src/../../outside",
            ".git/",
            ".git/config",
            "node_modules/",
            "node_modules/dep",
            ".worktrees/",
            ".worktrees/branch",
            "src/node_modules/",
            "CON/",
            "//server/share",
        ):
            with self.subTest(prefix=bad_prefix):
                with self.assertRaises(ValueError):
                    list_files(self.root, prefix=bad_prefix)

        # Invalid glob for search
        for bad_glob in (
            "../outside/*",
            ".git/**",
            "node_modules/**",
            ".worktrees/**",
            "//server/share/*",
        ):
            with self.subTest(glob=bad_glob):
                with self.assertRaises(ValueError):
                    search(self.root, query="test", glob_pattern=bad_glob)

    def test_partial_prefix_preserves_hidden_and_excluded_name_siblings(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "workflow.yml").write_text("workflow", encoding="utf-8")
        (self.root / ".gitignore").write_text("ignored", encoding="utf-8")
        (self.root / "node_modules_helpers.py").write_text("helper", encoding="utf-8")
        (self.root / "src" / "node_modules_adapter.py").write_text("adapter", encoding="utf-8")

        for prefix, expected in (
            (".", [".github/workflow.yml", ".gitignore"]),
            (".git", [".github/workflow.yml", ".gitignore"]),
            ("node_modules", ["node_modules_helpers.py"]),
            ("src/node_modules", ["src/node_modules_adapter.py"]),
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(list_files(self.root, prefix)["paths"], expected)

    def test_scoped_glob_pruning_preserves_platform_case_matching(self):
        expected = ["src/main.py"] if os.name == "nt" else []
        for pattern in ("SRC/main.py", "SRC/*.py", "S*/main.py", "src/MAIN.*"):
            with self.subTest(pattern=pattern):
                matches = search(self.root, "hello world", pattern)["matches"]
                self.assertCountEqual([match["path"] for match in matches], expected)

        # Listing prefixes have always used literal, case-sensitive matching.
        self.assertEqual(list_files(self.root, "SRC")["paths"], [])

    def test_glob_whitespace_remains_significant(self):
        (self.root / " main.py").write_text("hello world", encoding="utf-8")
        (self.root / " src").mkdir()
        (self.root / " src" / "main.py").write_text("hello world", encoding="utf-8")

        for pattern, expected in (
            ("   ", []),
            ("src/main.py ", []),
            (" *.py", [" main.py", " src/main.py"]),
            (" src/*.py", [" src/main.py"]),
        ):
            with self.subTest(pattern=pattern):
                matches = search(self.root, "hello world", pattern)["matches"]
                self.assertCountEqual([match["path"] for match in matches], expected)

if __name__ == "__main__":
    unittest.main()
