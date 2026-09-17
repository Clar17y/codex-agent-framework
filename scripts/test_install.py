#!/usr/bin/env python3
"""Comprehensive test suite for the Codex agent framework installer."""

import uuid
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

# Ensure install.py from repository root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from install import (
    InstallLockError,
    PreflightError,
    install_framework,
    BEGIN_MARKER,
    END_MARKER,
)

SCRATCH_BASE = REPO_ROOT / ".llm-output" / "scratch_test_install"


class TestInstallFramework(unittest.TestCase):
    """Test suite covering all installer contracts and acceptance criteria."""

    def setUp(self):
        """Prepare retained scratch directory under repo .llm-output."""
        SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
        self.test_dir = SCRATCH_BASE / uuid.uuid4().hex[:12]
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.dest_root = self.test_dir / "target_codex"
        self.source_root = self.test_dir / "source"
        self.source_root.mkdir()
        for name in ('install.py', 'GLOBAL_POLICY.md', 'README.md', 'routing.example.json', 'task-template.json'):
            shutil.copy2(REPO_ROOT / name, self.source_root / name)
        for name in ('agents', 'skills', 'scripts', 'docs'):
            shutil.copytree(REPO_ROOT / name, self.source_root / name,
                            ignore=shutil.ignore_patterns('__pycache__'))

    def test_fresh_install(self):
        """Exercise fresh install into clean target codex root."""
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        # Check framework files in agent-framework
        af_dir = self.dest_root / "agent-framework"
        self.assertTrue((af_dir / "install-manifest.json").exists())
        self.assertTrue((af_dir / "routing.json").exists())
        self.assertTrue((af_dir / "GLOBAL_POLICY.md").exists())
        self.assertTrue((af_dir / "task-template.json").exists())
        self.assertTrue((af_dir / "README.md").exists())
        self.assertTrue((af_dir / "routing.example.json").exists())
        self.assertTrue((af_dir / "install.py").exists())
        self.assertTrue((af_dir / "scripts" / "provider_runner.py").exists())
        self.assertTrue((af_dir / "docs" / "FRAMEWORK.md").exists())

        # Check agents
        agents_dir = self.dest_root / "agents"
        self.assertTrue((agents_dir / "implementer.toml").exists())
        self.assertTrue((agents_dir / "reviewer.toml").exists())

        # Check skills
        skills_dir = self.dest_root / "skills"
        gemini_skill = skills_dir / "ask-gemini" / "SKILL.md"
        claude_skill = skills_dir / "ask-claude" / "SKILL.md"
        self.assertTrue(gemini_skill.exists())
        self.assertTrue(claude_skill.exists())
        simplify = skills_dir / 'simplify/SKILL.md'
        self.assertTrue(simplify.exists())
        simplify_text = simplify.read_text(encoding='utf-8')
        self.assertNotIn('{{CODEX_ROOT}}', simplify_text)
        self.assertIn(self.dest_root.resolve().as_posix() + '/agent-framework/docs/FRAMEWORK.md', simplify_text)

        # Check AGENTS.md
        agents_md = self.dest_root / "AGENTS.md"
        self.assertTrue(agents_md.exists())

        # Verify template substitution with POSIX path
        dest_posix = self.dest_root.resolve().as_posix()
        policy_content = (af_dir / "GLOBAL_POLICY.md").read_text(encoding="utf-8")
        self.assertIn(dest_posix, policy_content)
        self.assertNotIn("{{CODEX_ROOT}}", policy_content)

        gemini_content = gemini_skill.read_text(encoding="utf-8")
        self.assertIn(dest_posix, gemini_content)
        self.assertNotIn("{{CODEX_ROOT}}", gemini_content)

        claude_content = claude_skill.read_text(encoding="utf-8")
        self.assertIn(dest_posix, claude_content)
        self.assertNotIn("{{CODEX_ROOT}}", claude_content)

        agents_content = agents_md.read_text(encoding="utf-8")
        self.assertIn(BEGIN_MARKER, agents_content)
        self.assertIn(END_MARKER, agents_content)
        self.assertIn(dest_posix, agents_content)

        # Check manifest contents
        manifest = json.loads((af_dir / "install-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("version"), 5)
        self.assertIn("installed_at", manifest)
        self.assertIsInstance(manifest.get("files"), list)
        self.assertGreater(len(manifest["files"]), 15)

        # In fresh install, backups should all be None
        for entry in manifest["files"]:
            self.assertIn("path", entry)
            self.assertIn("sha256", entry)
            self.assertTrue(entry["sha256"].isupper())
            self.assertIsNone(entry["backup"])

        # Check routing.json
        routing = json.loads((af_dir / "routing.json").read_text(encoding="utf-8"))
        self.assertEqual(routing.get("version"), 7)
        self.assertIn("providers", routing)
        self.assertIn("gemini", routing["providers"])
        self.assertIn("deepseek", routing["providers"])
        self.assertIn("claude", routing["providers"])
        self.assertEqual(routing["providers"]["gemini"]["timeout_seconds"], 3600)
        self.assertEqual(routing["providers"]["deepseek"]["model"], "deepseek-flash")
        self.assertEqual(routing["providers"]["deepseek"]["profile"], "deepseek")

        # Check lock released
        self.assertFalse((self.dest_root / ".agent-framework-install.lock").exists())

    def test_repeated_install_preserving_config_and_state(self):
        """Repeated install must preserve unrelated config, runtime state, and custom routing."""
        # 1. Initial install
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        # 2. Add unrelated configuration and runtime state
        unrelated_config = self.dest_root / "config.toml"
        unrelated_config.write_text('theme = "custom-dark"\nauth_mode = "manual"\n', encoding="utf-8")

        unrelated_state = self.dest_root / "state_5.sqlite"
        unrelated_state.write_bytes(b"dummy sqlite binary content")

        runtime_state_dir = self.dest_root / "agent-framework" / "state" / "workspaces"
        runtime_state_dir.mkdir(parents=True, exist_ok=True)
        runtime_state_file = runtime_state_dir / "session.json"
        runtime_state_file.write_text('{"active_run": "run-123"}', encoding="utf-8")

        custom_agent = self.dest_root / "agents" / "my-custom-analyst.toml"
        custom_agent.write_text('name = "custom-analyst"\nmodel = "gpt-5.6-luna"\n', encoding="utf-8")

        # Customize routing
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        routing_data = json.loads(routing_path.read_text(encoding="utf-8"))
        routing_data["timeout_seconds"] = 1800
        routing_data["providers"]["gemini"]["custom_setting"] = "preserve_me"
        routing_path.write_text(json.dumps(routing_data, indent=2), encoding="utf-8")

        # Customize AGENTS.md outside the marked block
        agents_path = self.dest_root / "AGENTS.md"
        old_agents_text = agents_path.read_text(encoding="utf-8")
        custom_header = "# Personal User Instructions\nAlways execute tests before commit.\n\n"
        custom_footer = "\n\n## Additional User Rules\nDo not delete notes.\n"
        agents_path.write_text(custom_header + old_agents_text + custom_footer, encoding="utf-8")

        # 3. Repeated install (upgrade without --refresh-routing)
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        # 4. Verify unrelated config and runtime state are completely intact
        self.assertEqual(unrelated_config.read_text(encoding="utf-8"), 'theme = "custom-dark"\nauth_mode = "manual"\n')
        self.assertEqual(unrelated_state.read_bytes(), b"dummy sqlite binary content")
        self.assertEqual(runtime_state_file.read_text(encoding="utf-8"), '{"active_run": "run-123"}')
        self.assertEqual(custom_agent.read_text(encoding="utf-8"), 'name = "custom-analyst"\nmodel = "gpt-5.6-luna"\n')

        # 5. Verify custom routing was preserved
        upgraded_routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded_routing.get("timeout_seconds"), 1800)
        self.assertEqual(upgraded_routing["providers"]["gemini"].get("custom_setting"), "preserve_me")

        # 6. Verify AGENTS.md preserved text outside block
        new_agents_text = agents_path.read_text(encoding="utf-8")
        self.assertTrue(new_agents_text.startswith("# Personal User Instructions"))
        self.assertTrue(new_agents_text.endswith("Do not delete notes.\n"))

        # 7. Verify backups directory exists and records prior manifest & replaced files
        backups_base = self.dest_root / "agent-framework" / "backups"
        self.assertTrue(backups_base.is_dir())
        backup_runs = list(backups_base.iterdir())
        self.assertGreaterEqual(len(backup_runs), 1)

        new_manifest = json.loads((self.dest_root / "agent-framework" / "install-manifest.json").read_text(encoding="utf-8"))
        latest_backup = Path(new_manifest['backup_directory'])
        self.assertTrue((latest_backup / "agent-framework" / "install-manifest.json").exists())
        self.assertTrue((latest_backup / "agent-framework" / "routing.json").exists())
        self.assertTrue((latest_backup / "AGENTS.md").exists())

        # 8. Check that new manifest records backup paths
        new_manifest = json.loads((self.dest_root / "agent-framework" / "install-manifest.json").read_text(encoding="utf-8"))
        has_backup_count = 0
        for entry in new_manifest["files"]:
            if entry["backup"] is not None:
                has_backup_count += 1
                self.assertTrue(Path(entry["backup"]).exists())
        self.assertGreater(has_backup_count, 10)

    def test_routing_executable_overrides(self):
        """Explicit --gemini or --claude executable override updates corresponding provider only."""
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        routing_path = self.dest_root / "agent-framework" / "routing.json"
        initial_routing = json.loads(routing_path.read_text(encoding="utf-8"))
        initial_gemini_exe = initial_routing["providers"]["gemini"]["executable"]

        # Override Claude only
        custom_claude = "C:\\Tools\\CustomClaude\\claude.exe"
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
            claude_override=custom_claude,
        )

        updated_routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_routing["providers"]["claude"]["executable"], custom_claude)
        self.assertEqual(updated_routing["providers"]["gemini"]["executable"], initial_gemini_exe)

        # Override Gemini only
        custom_gemini = "C:\\Tools\\CustomAgy\\agy.exe"
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
            gemini_override=custom_gemini,
        )

        updated_routing_2 = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_routing_2["providers"]["gemini"]["executable"], custom_gemini)
        self.assertEqual(updated_routing_2["providers"]["claude"]["executable"], custom_claude)

    def test_routing_refresh(self):
        """Explicit --refresh-routing re-generates routing.json from routing.example.json."""
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        routing_path = self.dest_root / "agent-framework" / "routing.json"
        custom_data = json.loads(routing_path.read_text(encoding="utf-8"))
        custom_data["timeout_seconds"] = 5555
        routing_path.write_text(json.dumps(custom_data, indent=2), encoding="utf-8")

        # Upgrade with refresh_routing=True
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
            refresh_routing=True,
        )

        refreshed_data = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed_data["timeout_seconds"], 900)
        self.assertEqual(refreshed_data["providers"]["gemini"]["timeout_seconds"], 3600)

    def test_v5_global_900_migrates_gemini_lifecycle_defaults_only(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 5
        data["providers"]["gemini"].pop("timeout_seconds")
        data["providers"]["gemini"].pop("termination_grace_seconds")
        data["providers"]["gemini"].pop("heartbeat_seconds")
        data["timeout_seconds"] = 900
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 7)
        self.assertEqual(migrated["providers"]["gemini"]["timeout_seconds"], 3600)
        self.assertEqual(migrated["providers"]["gemini"]["termination_grace_seconds"], 120)

    def test_v6_default_gemini_timeout_migrates_to_one_hour(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 6
        data["providers"]["gemini"]["timeout_seconds"] = 1800
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 7)
        self.assertEqual(migrated["providers"]["gemini"]["timeout_seconds"], 3600)

    def test_v6_custom_gemini_timeout_is_preserved(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 6
        data["providers"]["gemini"]["timeout_seconds"] = 2700
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 7)
        self.assertEqual(migrated["providers"]["gemini"]["timeout_seconds"], 2700)

    def test_preserved_routing_fills_missing_universal_heartbeats_only(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["providers"]["gemini"]["heartbeat_seconds"] = 0
        data["providers"]["deepseek"].pop("heartbeat_seconds")
        data["providers"]["claude"].pop("heartbeat_seconds")
        routing_path.write_text(json.dumps(data), encoding="utf-8")

        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))["providers"]
        self.assertEqual(migrated["gemini"]["heartbeat_seconds"], 0)
        self.assertEqual(migrated["deepseek"]["heartbeat_seconds"], 60)
        self.assertEqual(migrated["claude"]["heartbeat_seconds"], 60)

    def test_v5_custom_timeout_is_preserved(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 5
        data["providers"]["gemini"].pop("timeout_seconds")
        data["providers"]["gemini"].pop("termination_grace_seconds")
        data["providers"]["gemini"].pop("heartbeat_seconds")
        data["timeout_seconds"] = 5555
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["timeout_seconds"], 5555)
        self.assertNotIn("timeout_seconds", migrated["providers"]["gemini"])

    def test_v5_partial_gemini_timing_preserves_set_values_and_fills_missing_defaults(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 5
        data["timeout_seconds"] = 900
        gemini = data["providers"]["gemini"]
        gemini["timeout_seconds"] = 321
        gemini.pop("termination_grace_seconds")
        gemini.pop("heartbeat_seconds")
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))["providers"]["gemini"]
        self.assertEqual(migrated["timeout_seconds"], 321)
        self.assertEqual(migrated["termination_grace_seconds"], 120)
        self.assertEqual(migrated["heartbeat_seconds"], 60)

    def test_dry_run_makes_no_changes(self):
        """--dry-run must perform preflight and make zero changes to disk."""
        # Non-existent target root
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
            dry_run=True,
        )
        self.assertFalse(self.dest_root.exists())

        # Existing target with files
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
        )

        backups_base = self.dest_root / "agent-framework" / "backups"
        prior_backups_count = len(list(backups_base.iterdir())) if backups_base.exists() else 0
        agents_md = self.dest_root / "AGENTS.md"
        prior_agents_mtime = agents_md.stat().st_mtime_ns

        # Run dry run
        install_framework(
            codex_home=str(self.dest_root),
            source=str(self.source_root),
            dry_run=True,
        )

        # No new backups or file modifications
        new_backups_count = len(list(backups_base.iterdir())) if backups_base.exists() else 0
        self.assertEqual(prior_backups_count, new_backups_count)
        self.assertEqual(prior_agents_mtime, agents_md.stat().st_mtime_ns)
        self.assertFalse((self.dest_root / ".agent-framework-install.lock").exists())

    def test_malformed_markers_rejects_without_writes(self):
        """Malformed, duplicate, or reversed markers in AGENTS.md must be rejected before any writes."""
        test_cases = [
            ("begin_only", f"{BEGIN_MARKER}\nSome content\n"),
            ("end_only", f"Some content\n{END_MARKER}\n"),
            ("duplicate_begin", f"{BEGIN_MARKER}\ncontent\n{BEGIN_MARKER}\n{END_MARKER}\n"),
            ("duplicate_end", f"{BEGIN_MARKER}\ncontent\n{END_MARKER}\n{END_MARKER}\n"),
            ("reversed", f"{END_MARKER}\ncontent\n{BEGIN_MARKER}\n"),
            ("broken_comment", f"<!-- BEGIN CODEX MULTI-PROVIDER FRAMEWORK\ncontent\n-->\n"),
        ]

        for label, malformed_text in test_cases:
            case_dir = self.test_dir / f"case_{label}"
            case_dir.mkdir(parents=True, exist_ok=True)
            agents_file = case_dir / "AGENTS.md"
            agents_file.write_text(malformed_text, encoding="utf-8")

            with self.assertRaises(PreflightError, msg=f"Failed to reject case: {label}"):
                install_framework(
                    codex_home=str(case_dir),
                    source=str(self.source_root),
                )

            # Ensure AGENTS.md was unchanged and no framework files were written
            self.assertEqual(agents_file.read_text(encoding="utf-8"), malformed_text)
            self.assertFalse((case_dir / "agent-framework").exists())
            self.assertFalse((case_dir / "agents").exists())
            self.assertFalse((case_dir / "skills").exists())

    def test_file_dir_collisions_preflight(self):
        """Preflight must reject file-directory collisions before writes."""
        # Collision: AGENTS.md is a directory
        col_dir = self.test_dir / "col1"
        col_dir.mkdir(parents=True, exist_ok=True)
        (col_dir / "AGENTS.md").mkdir(parents=True, exist_ok=True)

        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(col_dir), source=str(self.source_root))

        # Collision: agents is a regular file
        col_dir2 = self.test_dir / "col2"
        col_dir2.mkdir(parents=True, exist_ok=True)
        (col_dir2 / "agents").write_text("not a directory", encoding="utf-8")

        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(col_dir2), source=str(self.source_root))

    def test_filesystem_root_and_overlap_rejection(self):
        """Preflight must reject filesystem root and source/destination overlaps."""
        # Filesystem root
        root_path = Path(Path.cwd().anchor)
        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(root_path), source=str(self.source_root))

        # Destination identical to source
        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(self.source_root), source=str(self.source_root))

        # Destination inside source repository
        inside_source = self.source_root / "nested_overlap"
        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(inside_source), source=str(self.source_root))

    def test_symlink_or_junction_rejection(self):
        """Preflight must reject symlink/junction components in target path."""
        target_dir = self.test_dir / "symlink_test"
        target_dir.mkdir(parents=True, exist_ok=True)
        real_agents = target_dir / "real_agents"
        real_agents.mkdir(parents=True, exist_ok=True)
        linked_agents = target_dir / "agents"

        # Create directory junction on Windows or symlink on POSIX
        if sys.platform == "win32":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_agents), str(real_agents)],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                self.skipTest(f"mklink /J not permitted: {res.stderr}")
        else:
            linked_agents.symlink_to(real_agents, target_is_directory=True)

        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(target_dir), source=str(self.source_root))

    def test_missing_source_preflight(self):
        """Preflight must reject installation if required source files are missing."""
        dummy_source = self.test_dir / "dummy_source"
        dummy_source.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(PreflightError):
            install_framework(codex_home=str(self.dest_root), source=str(dummy_source))

        self.assertFalse(self.dest_root.exists())

    def test_existing_lock_refusal(self):
        """Installation must refuse execution if an active install lock exists."""
        self.dest_root.mkdir(parents=True, exist_ok=True)
        lock_file = self.dest_root / ".agent-framework-install.lock"
        lock_file.write_text("pid=99999\n", encoding="utf-8")

        with self.assertRaises(InstallLockError):
            install_framework(codex_home=str(self.dest_root), source=str(self.source_root))

    def test_cli_execution(self):
        """Test invoking install.py via CLI with --codex-home and --dry-run."""
        # CLI dry run
        res_dry = subprocess.run(
            [sys.executable, str(self.source_root / "install.py"), "--codex-home", str(self.dest_root), "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_dry.returncode, 0)
        self.assertIn("Dry run requested", res_dry.stdout)
        self.assertFalse(self.dest_root.exists())

        # CLI real run
        res_real = subprocess.run(
            [sys.executable, str(self.source_root / "install.py"), "--codex-home", str(self.dest_root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_real.returncode, 0)
        self.assertTrue((self.dest_root / "agent-framework" / "install-manifest.json").exists())

    def make_link(self, link, target, directory=True):
        if sys.platform == 'win32' and directory:
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            try:
                link.symlink_to(target, target_is_directory=directory)
            except OSError as exc:
                self.skipTest(f'Platform cannot create symlink: {exc}')

    def test_linked_root_rejected_before_resolving(self):
        outside = self.test_dir / 'outside'
        outside.mkdir()
        self.make_link(self.dest_root, outside)
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(list(outside.iterdir()), [])

    def test_linked_backups_rejected_before_writes(self):
        framework = self.dest_root / 'agent-framework'
        framework.mkdir(parents=True)
        outside = self.test_dir / 'outside'
        outside.mkdir()
        self.make_link(framework / 'backups', outside)
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertFalse((self.dest_root / 'agents').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_manifest_collision_preflight(self):
        (self.dest_root / 'agent-framework/install-manifest.json').mkdir(parents=True)
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertFalse((self.dest_root / 'agents').exists())

    def test_bad_routing_preflight_preserves_files(self):
        framework = self.dest_root / 'agent-framework'
        framework.mkdir(parents=True)
        routing = framework / 'routing.json'
        routing.write_bytes(b'{invalid')
        sentinel = framework / 'README.md'
        sentinel.write_bytes(b'original')
        with self.assertRaisesRegex(PreflightError, 'routing.json'):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(sentinel.read_bytes(), b'original')
        self.assertEqual(routing.read_bytes(), b'{invalid')
        self.assertFalse((framework / 'backups').exists())

    def test_missing_runner_and_role_preflight(self):
        for relative in ('scripts/provider_runner.py', 'agents/implementer.toml', 'skills/simplify/SKILL.md'):
            path = self.source_root / relative
            retained = path.with_suffix('.retained')
            path.rename(retained)
            with self.assertRaises(PreflightError):
                install_framework(str(self.dest_root), source=str(self.source_root))
            self.assertFalse(self.dest_root.exists())
            retained.rename(path)

    def test_preserve_instruction_bytes_and_backups(self):
        self.dest_root.mkdir()
        path = self.dest_root / 'AGENTS.md'
        before = b'\xef\xbb\xbf# Personal\r\n\r\n'
        after = b'\r\n\r\n# More\r\n'
        original = before + BEGIN_MARKER.encode() + b'\nold\n' + END_MARKER.encode() + after
        path.write_bytes(original)
        install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertTrue(path.read_bytes().endswith(after))
        manifest = json.loads((self.dest_root / 'agent-framework/install-manifest.json').read_text())
        entry = next(e for e in manifest['files'] if Path(e['path']) == path)
        self.assertEqual(Path(entry['backup']).read_bytes(), original)

    def test_environment_root_and_default(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.dest_root)}):
            install_framework(source=str(self.source_root))
        self.assertTrue((self.dest_root / 'AGENTS.md').exists())
        default_home = self.test_dir / 'default-home'
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch('install.Path.home', return_value=default_home):
            install_framework(source=str(self.source_root))
        self.assertTrue((default_home / '.codex/AGENTS.md').exists())

    def test_missing_providers_and_policy_update(self):
        with mock.patch('install.shutil.which', return_value=None):
            install_framework(str(self.dest_root), source=str(self.source_root))
        routing = json.loads((self.dest_root / 'agent-framework/routing.json').read_text())
        self.assertEqual(routing['providers']['gemini']['executable'], 'agy')
        self.assertEqual(routing['providers']['claude']['executable'], 'claude')
        policy = self.source_root / 'GLOBAL_POLICY.md'
        policy.write_text('Updated policy {{CODEX_ROOT}}', encoding='utf-8')
        install_framework(str(self.dest_root), source=str(self.source_root))
        text = (self.dest_root / 'AGENTS.md').read_text()
        self.assertIn('Updated policy ' + self.dest_root.resolve().as_posix(), text)
        self.assertEqual(text.count(BEGIN_MARKER), 1)

    def test_linked_source_rejected(self):
        alias = self.test_dir / 'source-link'
        self.make_link(alias, self.source_root)
        with self.assertRaisesRegex(PreflightError, 'physical path'):
            install_framework(str(self.dest_root), source=str(alias))
        self.assertFalse(self.dest_root.exists())

    def test_partial_failure_retains_original_backups_and_releases_lock(self):
        self.dest_root.mkdir()
        personal = self.dest_root / 'AGENTS.md'
        personal.write_bytes(b'Original personal policy\r\n')
        write_bytes = Path.write_bytes
        def fail_policy_write(path, content):
            if path == personal:
                raise OSError('injected disk failure')
            return write_bytes(path, content)
        with mock.patch('install.Path.write_bytes', new=fail_policy_write):
            with self.assertRaisesRegex(OSError, 'injected disk failure'):
                install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(personal.read_bytes(), b'Original personal policy\r\n')
        backups = list((self.dest_root / 'agent-framework/backups').glob('*/AGENTS.md'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), personal.read_bytes())
        self.assertFalse((self.dest_root / '.agent-framework-install.lock').exists())
        self.assertFalse((self.dest_root / 'agent-framework/install-manifest.json').exists())


    def test_deepseek_executable_override(self):
        """Specifying custom deepseek executable path overrides discovery."""
        install_framework(
            str(self.dest_root),
            source=str(self.source_root),
            deepseek_override="/custom/path/to/codex",
        )
        routing = json.loads((self.dest_root / "agent-framework/routing.json").read_text(encoding="utf-8"))
        self.assertEqual(routing["providers"]["deepseek"]["executable"], "/custom/path/to/codex")
        self.assertEqual(routing["providers"]["deepseek"]["profile"], "deepseek")
        self.assertEqual(routing["providers"]["deepseek"]["model"], "deepseek-flash")

    def test_upgrade_injects_deepseek_preserving_customizations(self):
        """Upgrade on older routing.json without deepseek adds deepseek while preserving existing customizations."""
        # 1. First install
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))

        # 2. Simulate a pre-v5 routing file: remove deepseek, customize gemini
        del routing["providers"]["deepseek"]
        routing["version"] = 4
        routing["providers"]["gemini"]["model"] = "gemini-custom-ultra"
        routing["custom_field"] = "custom_value"
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")

        # 3. Upgrade without refresh_routing
        install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)
        updated_routing = json.loads(routing_path.read_text(encoding="utf-8"))

        # 4. Verify custom settings preserved and deepseek added
        self.assertEqual(updated_routing["providers"]["gemini"]["model"], "gemini-custom-ultra")
        self.assertEqual(updated_routing["custom_field"], "custom_value")
        self.assertIn("deepseek", updated_routing["providers"])
        self.assertEqual(updated_routing["providers"]["deepseek"]["model"], "deepseek-flash")
        self.assertEqual(updated_routing["providers"]["deepseek"]["profile"], "deepseek")
        self.assertEqual(updated_routing["providers"]["deepseek"]["api_key_env"], "DEEPSEEK_API_KEY")

    def test_v5_preserves_deliberate_deepseek_absence(self):
        """The one-time v4 migration must not re-enable a v5 opt-out by removal."""
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        del routing["providers"]["deepseek"]
        routing["version"] = 5
        routing_path.write_text(json.dumps(routing), encoding="utf-8")
        install_framework(str(self.dest_root), source=str(self.source_root))
        updated = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertNotIn("deepseek", updated["providers"])

    def test_malformed_routing_version_fails_preflight(self):
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        for bad_version in ("5", True, 5.0):
            with self.subTest(version=bad_version):
                routing["version"] = bad_version
                routing_path.write_text(json.dumps(routing), encoding="utf-8")
                with self.assertRaisesRegex(PreflightError, "version must be an integer"):
                    install_framework(str(self.dest_root), source=str(self.source_root))

    def test_positional_calling_convention_backward_compatibility(self):
        """Legacy 6 positional arguments (codex_home, dry_run, gemini, claude, refresh_routing, source) must work without shift."""
        install_framework(
            str(self.dest_root),  # codex_home
            False,                # dry_run
            None,                 # gemini_override
            None,                 # claude_override
            False,                # refresh_routing
            str(self.source_root) # source
        )
        routing_path = self.dest_root / "agent-framework/routing.json"
        self.assertTrue(routing_path.exists())
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertIn("deepseek", routing["providers"])
        self.assertIn("gemini", routing["providers"])
        self.assertIn("claude", routing["providers"])

    def test_malformed_source_routing_fails_preflight_before_mutation(self):
        """During migration with preserving routing, malformed source routing.example.json must raise PreflightError before mutation."""
        # 1. Initial install
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        original_routing_bytes = routing_path.read_bytes()

        # 2. Corrupt source routing.example.json
        (self.source_root / "routing.example.json").write_text("{malformed json", encoding="utf-8")

        # 3. Upgrade with preserving routing
        with self.assertRaises(PreflightError) as ctx:
            install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)
        self.assertIn("routing", str(ctx.exception).lower())

        # 4. Verify no mutation occurred on target
        self.assertEqual(routing_path.read_bytes(), original_routing_bytes)


if __name__ == "__main__":
    unittest.main()
