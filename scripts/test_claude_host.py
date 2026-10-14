#!/usr/bin/env python3
"""Installer and routing checks for the Claude Code host."""

import json
import os
from pathlib import Path
import re
import shutil
import sys
import unittest
import uuid
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from install import (  # noqa: E402
    CLAUDE_BEGIN_MARKER,
    CLAUDE_END_MARKER,
    CLAUDE_SKILLS,
    PreflightError,
    ROLE_FILES,
    SHARED_SKILLS,
    install_framework,
    main,
)
from scripts.provider_runner import activity_routes  # noqa: E402

SCRATCH_BASE = REPO_ROOT / ".llm-output" / "scratch_test_claude_host"
FRONTMATTER = re.compile(r"\A---\nname: ([a-z-]+)\ndescription: (.+)\ntools: (.+)\nmodel: (haiku|sonnet|opus)\n---\n\n\S")


class ClaudeHostInstallTests(unittest.TestCase):
    def setUp(self):
        SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
        self.test_dir = SCRATCH_BASE / uuid.uuid4().hex[:12]
        self.test_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.test_dir, True)
        self.fake_home = (self.test_dir / "user_home").resolve()
        self.fake_home.mkdir()
        home_patch = mock.patch.object(Path, "home", return_value=self.fake_home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.root = self.test_dir / "claude_home"
        self.source = self.test_dir / "source"
        self.source.mkdir()
        for name in ("install.py", "GLOBAL_POLICY.md", "README.md", "routing.example.json", "task-template.json"):
            shutil.copy2(REPO_ROOT / name, self.source / name)
        for name in ("agents", "skills", "scripts", "docs", "claude"):
            shutil.copytree(REPO_ROOT / name, self.source / name, ignore=shutil.ignore_patterns("__pycache__"))

    def install(self, **kwargs):
        with mock.patch("builtins.print"):
            install_framework(host="claude", claude_home=str(self.root), source=str(self.source), **kwargs)

    def installed_text_files(self):
        return [self.root / "CLAUDE.md", *(self.root / "agents").glob("*"), *(self.root / "skills").rglob("*.md")]

    def test_fresh_install_writes_claude_layout_with_rendered_paths(self):
        self.install()
        root = self.root.resolve().as_posix()
        claude_md = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(claude_md.count(CLAUDE_BEGIN_MARKER), 1)
        self.assertEqual(claude_md.count(CLAUDE_END_MARKER), 1)
        self.assertIn(f"`{root}/agent-framework/scripts/provider_runner.py`", claude_md)
        self.assertEqual(sorted(p.stem for p in (self.root / "agents").iterdir()), sorted(ROLE_FILES))
        for name in ROLE_FILES:
            text = (self.root / "agents" / f"{name}.md").read_text(encoding="utf-8")
            match = FRONTMATTER.match(text)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.group(1), name)
            if name in ("explorer", "docs-researcher", "planner", "refactor-auditor", "security-reviewer",
                        "quality-gate-max", "reviewer", "correctness-gate"):
                self.assertNotIn("Write", match.group(3), name)
                self.assertNotIn("Edit", match.group(3), name)
        self.assertEqual(sorted(p.name for p in (self.root / "skills").iterdir()),
                         sorted((*CLAUDE_SKILLS, *SHARED_SKILLS)))
        for path in self.installed_text_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("{{", text, path)
            self.assertNotRegex(text, r"\bgpt-6|\bLuna medium|ask-claude", path)
        skill = (self.root / "skills/jev-search/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"{root}/agent-framework/scripts/jev_search.py", skill)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse(list((self.root / "agents").glob("*.toml")))
        self.assertTrue((self.root / "agent-framework/scripts/provider_runner.py").is_file())
        self.assertTrue((self.root / "agent-framework/docs/CLAUDE_HOST.md").is_file())
        self.assertTrue((self.fake_home / ".gemini/config/agents/framework-reader.md").is_file())
        routing = json.loads((self.root / "agent-framework/routing.json").read_text(encoding="utf-8"))
        example = json.loads((REPO_ROOT / "claude/routing.example.json").read_text(encoding="utf-8"))
        self.assertEqual(routing["activities"], example["activities"])

    def test_existing_instructions_are_preserved_and_reinstall_is_idempotent(self):
        self.root.mkdir()
        personal = "# Personal rules\r\n\r\nUse British English.\r\n"
        (self.root / "CLAUDE.md").write_bytes(personal.encode("utf-8"))
        self.install()
        first = (self.root / "CLAUDE.md").read_bytes()
        self.assertTrue(first.startswith(personal.encode("utf-8")))
        self.install()
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), first)

    def test_unowned_agent_or_skill_collision_stops_before_writing(self):
        for relative in ("agents/explorer.md", "skills/ask-codex/SKILL.md"):
            with self.subTest(relative=relative):
                shutil.rmtree(self.root, ignore_errors=True)
                target = self.root / relative
                target.parent.mkdir(parents=True)
                target.write_text("user-owned definition", encoding="utf-8")
                with self.assertRaisesRegex(PreflightError, "not owned by this framework"):
                    self.install()
                self.assertEqual(target.read_text(encoding="utf-8"), "user-owned definition")
                self.assertFalse((self.root / "CLAUDE.md").exists())
                self.assertFalse((self.root / "agent-framework").exists())

    def test_owned_files_upgrade_but_operator_edits_are_protected(self):
        self.install()
        source_role = self.source / "claude/agents/explorer.md"
        source_role.write_text(source_role.read_text(encoding="utf-8") + "Upgrade marker.\n", encoding="utf-8")
        self.install()
        installed = self.root / "agents/explorer.md"
        self.assertTrue(installed.read_text(encoding="utf-8").endswith("Upgrade marker.\n"))
        installed.write_text("operator edit", encoding="utf-8")
        before = (self.root / "agent-framework/install-manifest.json").read_bytes()
        with self.assertRaisesRegex(PreflightError, "explorer.md"):
            self.install()
        self.assertEqual(installed.read_text(encoding="utf-8"), "operator edit")
        self.assertEqual((self.root / "agent-framework/install-manifest.json").read_bytes(), before)

    def test_routing_edits_survive_reinstall(self):
        self.install()
        path = self.root / "agent-framework/routing.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["activities"]["implementation"] = ["muse-wonder", "gemini-flash"]
        path.write_text(json.dumps(config), encoding="utf-8")
        self.install()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["activities"]["implementation"],
                         ["muse-wonder", "gemini-flash"])

    def test_config_dir_environment_and_mismatched_home_flags(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}), mock.patch("builtins.print"):
            install_framework(host="claude", source=str(self.source))
        self.assertTrue((self.root / "CLAUDE.md").is_file())
        with self.assertRaisesRegex(PreflightError, "--codex-home does not apply"):
            install_framework(host="claude", codex_home=str(self.root), source=str(self.source))
        with self.assertRaisesRegex(PreflightError, "--claude-home does not apply"):
            install_framework(host="codex", claude_home=str(self.root), source=str(self.source))

    def test_cli_dry_run_makes_no_changes(self):
        with mock.patch("builtins.print"):
            code = main(["--host", "claude", "--claude-home", str(self.root), "--source", str(self.source), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self.root.exists())

    def test_codex_install_from_same_source_ignores_claude_files(self):
        codex_root = self.test_dir / "codex_home"
        with mock.patch("builtins.print"):
            install_framework(str(codex_root), source=str(self.source))
        self.assertFalse((codex_root / "CLAUDE.md").exists())
        self.assertFalse((codex_root / "agent-framework/claude").exists())
        self.assertFalse(list((codex_root / "agents").glob("*.md")))
        self.assertTrue((codex_root / "AGENTS.md").is_file())

    def test_shared_reader_upgrade_accepts_either_hosts_manifest(self):
        codex_root = self.fake_home / ".codex"
        claude_root = self.fake_home / ".claude"
        reader_source = self.source / "agents/framework-reader.md"
        reader = self.fake_home / ".gemini/config/agents/framework-reader.md"
        original = reader_source.read_text(encoding="utf-8")
        with mock.patch.dict(os.environ), mock.patch("builtins.print"):
            # Default roots under the fake home, as on a normal machine.
            os.environ.pop("CODEX_HOME", None)
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            install_framework(source=str(self.source))  # codex host, release A
            reader_source.write_text(original + "Release B.\n", encoding="utf-8")
            install_framework(host="claude", source=str(self.source))  # owned via the Codex manifest
            self.assertTrue(reader.read_text(encoding="utf-8").endswith("Release B.\n"))
            reader_source.write_text(original + "Release C.\n", encoding="utf-8")
            install_framework(source=str(self.source))  # owned via the Claude manifest
            self.assertTrue(reader.read_text(encoding="utf-8").endswith("Release C.\n"))
            reader.write_text("operator edit", encoding="utf-8")
            with self.assertRaisesRegex(PreflightError, "Gemini reader agent differs"):
                install_framework(host="claude", source=str(self.source))
        self.assertTrue((codex_root / "AGENTS.md").is_file())
        self.assertTrue((claude_root / "CLAUDE.md").is_file())


class ClaudeRoutingDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((REPO_ROOT / "claude/routing.example.json").read_text(encoding="utf-8"))
        self.codex = json.loads((REPO_ROOT / "routing.example.json").read_text(encoding="utf-8"))

    def test_only_activity_chains_differ_from_codex_defaults(self):
        for key in set(self.config) | set(self.codex):
            if key != "activities":
                self.assertEqual(self.config.get(key), self.codex.get(key), key)
        self.assertEqual(set(self.config["activities"]), set(self.codex["activities"]))

    def test_every_chain_is_valid_and_spans_all_providers(self):
        for activity in self.config["activities"]:
            chain = activity_routes(self.config, activity)
            providers = [self.config["profiles"][name]["provider"] for name in chain]
            self.assertEqual(set(providers), {"openai", "claude", "gemini", "muse"}, activity)

    def test_reviews_start_outside_claude_and_routine_work_starts_on_gemini(self):
        def first_provider(activity):
            return self.config["profiles"][self.config["activities"][activity][0]]["provider"]
        for activity in ("independent_review", "security_review"):
            self.assertNotEqual(first_provider(activity), "claude", activity)
        for activity in ("exploration", "implementation", "testing", "verification", "documentation",
                         "docs_research", "refactor_audit"):
            self.assertEqual(first_provider(activity), "gemini", activity)


if __name__ == "__main__":
    unittest.main()
