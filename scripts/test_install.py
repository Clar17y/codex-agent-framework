#!/usr/bin/env python3
"""Comprehensive test suite for the Codex agent framework installer."""

import uuid
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import unittest
from unittest import mock

# Ensure install.py from repository root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from install import (
    InstallLockError,
    PreflightError,
    install_framework,
    resolve_routing,
    BEGIN_MARKER,
    END_MARKER,
)

SCRATCH_BASE = REPO_ROOT / ".llm-output" / "scratch_test_install"
SOURCE_FILES = ('install.py', 'GLOBAL_POLICY.md', 'README.md', 'routing.example.json', 'task-template.json')
SOURCE_DIRS = ('agents', 'skills', 'scripts', 'docs')


def copy_source_package(dest, extra_dirs=()):
    """Copy the installable package (not caches or scratch output) into dest."""
    dest.mkdir()
    for name in SOURCE_FILES:
        shutil.copy2(REPO_ROOT / name, dest / name)
    for name in (*SOURCE_DIRS, *extra_dirs):
        shutil.copytree(REPO_ROOT / name, dest / name, ignore=shutil.ignore_patterns('__pycache__'))


class TestInstallFramework(unittest.TestCase):
    """Test suite covering all installer contracts and acceptance criteria."""

    def setUp(self):
        """Prepare retained scratch directory under repo .llm-output."""
        SCRATCH_BASE.mkdir(parents=True, exist_ok=True)
        self.test_dir = SCRATCH_BASE / uuid.uuid4().hex[:12]
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.fake_home = (self.test_dir / 'user_home').resolve()
        self.fake_home.mkdir()
        home_patch = mock.patch.object(Path, 'home', return_value=self.fake_home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.dest_root = self.test_dir / "target_codex"
        self.source_root = self.test_dir / "source"
        copy_source_package(self.source_root)

    def test_v12_custom_profile_and_chain_preserved_and_bad_reference_rejected(self):
        install_framework(str(self.dest_root), source=str(self.source_root))
        path = self.dest_root / 'agent-framework/routing.json'
        config = json.loads(path.read_text())
        config['profiles']['openai-sol']['model'] = 'operator-sol'
        config['activities']['planning'] = ['openai-sol', 'gemini-flash']
        path.write_text(json.dumps(config))
        install_framework(str(self.dest_root), source=str(self.source_root))
        persisted = json.loads(path.read_text())
        self.assertEqual(persisted['profiles']['openai-sol']['model'], 'operator-sol')
        self.assertEqual(persisted['activities']['planning'], ['openai-sol', 'gemini-flash'])
        persisted['activities']['planning'] = ['absent-profile']
        path.write_text(json.dumps(persisted))
        before_bad = path.read_bytes()
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(path.read_bytes(), before_bad)

    def test_unknown_activity_keys_fail_preflight_before_mutation(self):
        source_path = self.source_root / 'routing.example.json'
        source_config = json.loads(source_path.read_text())
        source_config['activities']['unsupported'] = ['openai-sol']
        source_path.write_text(json.dumps(source_config))
        with self.assertRaisesRegex(PreflightError, 'Unknown activity chain: unsupported'):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertFalse(self.dest_root.exists())

        source_config['activities'].pop('unsupported')
        source_path.write_text(json.dumps(source_config))
        install_framework(str(self.dest_root), source=str(self.source_root))
        framework = self.dest_root / 'agent-framework'
        routing_path = framework / 'routing.json'
        manifest_path = framework / 'install-manifest.json'
        sentinel = framework / 'README.md'
        saved = json.loads(routing_path.read_text())
        saved['activities']['unsupported'] = ['openai-sol']
        routing_path.write_text(json.dumps(saved))
        before = (routing_path.read_bytes(), manifest_path.read_bytes(), sentinel.read_bytes())
        with self.assertRaisesRegex(PreflightError, 'Unknown activity chain: unsupported'):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual((routing_path.read_bytes(), manifest_path.read_bytes(), sentinel.read_bytes()), before)

        saved['activities'].pop('unsupported')
        saved['activities']['planning'] = ['gemini-flash', 'openai-sol']
        routing_path.write_text(json.dumps(saved))
        install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(json.loads(routing_path.read_text())['activities']['planning'],
                         ['gemini-flash', 'openai-sol'])
        stable = routing_path.read_bytes()
        source_config['activities']['unsupported'] = ['openai-sol']
        source_path.write_text(json.dumps(source_config))
        with self.assertRaisesRegex(PreflightError, 'Unknown activity chain: unsupported'):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(routing_path.read_bytes(), stable)
        source_config['activities'].pop('unsupported')
        source_path.write_text(json.dumps(source_config))
        for bad_chain in ([], ['openai-sol', 'openai-sol'], ['absent-profile']):
            with self.subTest(chain=bad_chain):
                saved['activities']['planning'] = bad_chain
                routing_path.write_text(json.dumps(saved))
                before_bad = routing_path.read_bytes()
                message = ('references unknown or disabled profile' if bad_chain == ['absent-profile']
                           else 'Activity planning requires a nonempty chain')
                with self.assertRaisesRegex(PreflightError, message):
                    install_framework(str(self.dest_root), source=str(self.source_root))
                self.assertEqual(routing_path.read_bytes(), before_bad)

    def test_registry_validation_matches_dispatcher_for_source_and_saved_config(self):
        from scripts import provider_runner as runner

        source_path = self.source_root / 'routing.example.json'
        source_base = json.loads(source_path.read_text())

        def malformed(config, case):
            if case == 'empty_profile_name':
                config['profiles'][''] = dict(config['profiles']['openai-sol'])
            elif case == 'blank_profile_name':
                config['profiles'][' '] = dict(config['profiles']['openai-sol'])
            elif case == 'unknown_activity':
                config['activities']['unsupported'] = ['openai-sol']
            elif case == 'empty_chain':
                config['activities']['planning'] = []
            elif case == 'duplicate_chain':
                config['activities']['planning'] = ['openai-sol', 'openai-sol']
            elif case == 'unknown_reference':
                config['activities']['planning'] = ['absent-profile']
            elif case == 'disabled_reference':
                config['profiles']['openai-sol']['enabled'] = False
                config['activities']['planning'] = ['openai-sol']
            elif case == 'unhashable_chain_item':
                config['activities']['planning'] = [['openai-sol']]
            elif case == 'invalid_profile_field':
                config['profiles']['openai-sol']['unexpected'] = True
            elif case == 'unhashable_effort':
                config['profiles']['openai-sol']['effort'] = ['medium']

        cases = ('empty_profile_name', 'blank_profile_name', 'unknown_activity', 'empty_chain', 'duplicate_chain',
                 'unknown_reference', 'disabled_reference', 'unhashable_chain_item',
                 'invalid_profile_field', 'unhashable_effort')
        for case in cases:
            with self.subTest(source=case):
                candidate = json.loads(json.dumps(source_base))
                malformed(candidate, case)
                with self.assertRaises(ValueError):
                    runner.activity_routes(candidate, 'planning')
                source_path.write_text(json.dumps(candidate))
                with self.assertRaises(PreflightError):
                    install_framework(str(self.dest_root), source=str(self.source_root))
                self.assertFalse(self.dest_root.exists())

        for field in ('profiles', 'activities'):
            with self.subTest(source_shape=field):
                candidate = json.loads(json.dumps(source_base))
                candidate[field] = []
                with self.assertRaises(ValueError):
                    runner.activity_routes(candidate, 'planning')
                source_path.write_text(json.dumps(candidate))
                with self.assertRaises(PreflightError):
                    install_framework(str(self.dest_root), source=str(self.source_root))
                self.assertFalse(self.dest_root.exists())

        source_base['profiles']['operator-extra'] = {'provider': 'openai', 'model': 'operator-model',
                                                      'effort': 'medium'}
        source_base['activities']['planning'] = ['operator-extra', 'gemini-flash', 'openai-sol']
        self.assertEqual(runner.activity_routes(source_base, 'planning'),
                         ['operator-extra', 'gemini-flash', 'openai-sol'])
        source_path.write_text(json.dumps(source_base))
        install_framework(str(self.dest_root), source=str(self.source_root))
        framework = self.dest_root / 'agent-framework'
        routing_path = framework / 'routing.json'
        saved_base = json.loads(routing_path.read_text())
        self.assertEqual(runner.activity_routes(saved_base, 'planning'),
                         ['operator-extra', 'gemini-flash', 'openai-sol'])
        manifest = (framework / 'install-manifest.json').read_bytes()
        readme = (framework / 'README.md').read_bytes()
        for case in cases:
            with self.subTest(saved=case):
                candidate = json.loads(json.dumps(saved_base))
                malformed(candidate, case)
                with self.assertRaises(ValueError):
                    runner.activity_routes(candidate, 'planning')
                routing_path.write_text(json.dumps(candidate))
                invalid_bytes = routing_path.read_bytes()
                with self.assertRaises(PreflightError):
                    install_framework(str(self.dest_root), source=str(self.source_root))
                self.assertEqual(routing_path.read_bytes(), invalid_bytes)
                self.assertEqual((framework / 'install-manifest.json').read_bytes(), manifest)
                self.assertEqual((framework / 'README.md').read_bytes(), readme)

    def test_v12_reader_collision_and_missing_source_fail_preflight(self):
        helper = self.fake_home / '.gemini/config/agents/framework-reader.md'
        helper.parent.mkdir(parents=True)
        helper.write_text('unrelated custom agent')
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertFalse((self.dest_root / 'agent-framework/routing.json').exists())
        helper.unlink()
        (self.source_root / 'scripts/test_activity_dispatch.py').unlink()
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertFalse((self.dest_root / 'agent-framework/routing.json').exists())

    def test_reader_manifest_owned_upgrade_and_operator_edit_collision(self):
        install_framework(str(self.dest_root), source=str(self.source_root))
        helper = self.fake_home / '.gemini/config/agents/framework-reader.md'
        packaged = self.source_root / 'agents/framework-reader.md'
        packaged.write_bytes(packaged.read_bytes() + b'\n# packaged revision\n')
        config_path = self.dest_root / 'agent-framework/routing.json'
        config = json.loads(config_path.read_text())
        config['providers']['openai']['windows_sandbox'] = 'unelevated'
        config['providers']['openai']['orchestration_windows_sandbox'] = 'mxc'
        config_path.write_text(json.dumps(config))
        install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(helper.read_bytes(), packaged.read_bytes())
        self.assertEqual(json.loads(config_path.read_text())['providers']['openai']['windows_sandbox'],
                         'unelevated')
        self.assertEqual(json.loads(config_path.read_text())['providers']['openai']['orchestration_windows_sandbox'],
                         'mxc')
        manifest = json.loads((self.dest_root / 'agent-framework/install-manifest.json').read_text())
        entry = next(entry for entry in manifest['files'] if Path(entry['path']) == helper)
        self.assertTrue(Path(entry['backup']).is_file())
        helper.write_text('operator edit')
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(helper.read_text(), 'operator edit')

    def test_custom_codex_home_reader_uses_actual_home_for_flag_env_and_upgrade(self):
        from scripts import provider_runner as runner
        helper = self.fake_home / '.gemini/config/agents/framework-reader.md'
        custom_root = self.test_dir / 'nested with spaces' / 'profiles' / 'codex-home'
        install_framework(codex_home=str(custom_root), source=str(self.source_root))
        config_path = custom_root / 'agent-framework/routing.json'
        config = json.loads(config_path.read_text())
        self.assertEqual(config['providers']['gemini']['reader_agent_file'], str(helper))
        self.assertEqual(helper.read_bytes(), (self.source_root / 'agents/framework-reader.md').read_bytes())
        runner.validate_gemini_reader(config['providers']['gemini'], custom_root)
        self.assertFalse((custom_root.parent / '.gemini/config/agents/framework-reader.md').exists())
        custom_guide = custom_root / 'agent-framework/docs/FRAMEWORK.md'
        self.assertIn(f'[FRAMEWORK.md](<{custom_guide.resolve().as_posix()}>)',
                      (custom_root / 'skills/route-activity/SKILL.md').read_text())
        self.assertTrue(custom_guide.is_file())

        # An earlier v12 install could have saved the wrong generated path.
        config['providers']['gemini']['reader_agent_file'] = str(custom_root.parent / '.gemini/config/agents/framework-reader.md')
        config_path.write_text(json.dumps(config))
        install_framework(codex_home=str(custom_root), source=str(self.source_root))
        migrated = json.loads(config_path.read_text())
        self.assertEqual(migrated['providers']['gemini']['reader_agent_file'], str(helper))

        env_root = self.test_dir / 'another space' / 'nested' / 'codex-home'
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(env_root)}):
            install_framework(source=str(self.source_root))
        env_config = json.loads((env_root / 'agent-framework/routing.json').read_text())
        self.assertEqual(env_config['providers']['gemini']['reader_agent_file'], str(helper))
        runner.validate_gemini_reader(env_config['providers']['gemini'], env_root)
        env_guide = env_root / 'agent-framework/docs/FRAMEWORK.md'
        self.assertIn(f'[FRAMEWORK.md](<{env_guide.resolve().as_posix()}>)',
                      (env_root / 'skills/route-activity/SKILL.md').read_text())
        self.assertTrue(env_guide.is_file())

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
        for name in ('run_tests.py', 'test_run_tests.py'):
            self.assertEqual((af_dir / 'scripts' / name).read_bytes(),
                             (self.source_root / 'scripts' / name).read_bytes())
        self.assertEqual(
            (af_dir / "scripts" / "test_muse_provider.py").read_bytes(),
            (self.source_root / "scripts" / "test_muse_provider.py").read_bytes(),
        )
        for name in ('jev_client.py', 'jev_search.py', 'jev_review.py'):
            self.assertTrue((af_dir / 'scripts' / name).is_file())
        self.assertTrue((af_dir / "docs" / "FRAMEWORK.md").exists())
        self.assertTrue((af_dir / "scripts" / "test_activity_dispatch.py").exists())
        self.assertEqual((self.fake_home / '.gemini/config/agents/framework-reader.md').read_bytes(),
                         (self.source_root / 'agents/framework-reader.md').read_bytes())
        self.assertEqual(len(json.loads((af_dir / 'routing.json').read_text())['activities']), 15)

        # Check agents
        agents_dir = self.dest_root / "agents"
        self.assertTrue((agents_dir / "implementer.toml").exists())
        self.assertTrue((agents_dir / "reviewer.toml").exists())
        expected_roles = {
            "implementer": ("gpt-6-luna", "medium"),
            "explorer": ("gpt-6-luna", "medium"),
            "docs-researcher": ("gpt-6-luna", "medium"),
            "refactor-auditor": ("gpt-6-luna", "medium"),
            "verifier": ("gpt-6-luna", "medium"),
            "complex-implementer": ("gpt-6.1-sol", "medium"),
            "planner": ("gpt-6.1-sol", "medium"),
            "test-engineer": ("gpt-6.1-sol", "medium"),
            "correctness-gate": ("gpt-6.1-sol", "high"),
            "security-reviewer": ("gpt-6.1-sol", "high"),
            "quality-gate-max": ("gpt-6.1-sol", "max"),
            "reviewer": ("gpt-6.1-sol", "medium"),
        }
        for role, expected in expected_roles.items():
            with self.subTest(role=role):
                data = tomllib.loads((agents_dir / (role + ".toml")).read_text(encoding="utf-8"))
                self.assertEqual((data["model"], data["model_reasoning_effort"]), expected)

        # Check skills
        skills_dir = self.dest_root / "skills"
        gemini_skill = skills_dir / "ask-gemini" / "SKILL.md"
        claude_skill = skills_dir / "ask-claude" / "SKILL.md"
        self.assertTrue(gemini_skill.exists())
        self.assertTrue(claude_skill.exists())
        muse_skill = skills_dir / "ask-muse" / "SKILL.md"
        self.assertTrue(muse_skill.exists())
        route_skill = skills_dir / 'route-activity/SKILL.md'
        self.assertTrue(route_skill.exists())
        guide = af_dir / 'docs/FRAMEWORK.md'
        self.assertIn(f'[FRAMEWORK.md](<{guide.resolve().as_posix()}>)', route_skill.read_text())
        self.assertTrue(guide.is_file())
        simplify = skills_dir / 'simplify/SKILL.md'
        self.assertTrue(simplify.exists())
        simplify_text = simplify.read_text(encoding='utf-8')
        self.assertNotIn('{{CODEX_ROOT}}', simplify_text)
        self.assertIn(self.dest_root.resolve().as_posix() + '/agent-framework/docs/FRAMEWORK.md', simplify_text)
        for name in ('jev-search', 'jev-review'):
            content = (skills_dir / name / 'SKILL.md').read_text(encoding='utf-8')
            self.assertNotIn('{{CODEX_ROOT}}', content)
            self.assertIn(self.dest_root.resolve().as_posix() + '/agent-framework/scripts/', content)

        # Check AGENTS.md
        agents_md = self.dest_root / "AGENTS.md"
        self.assertTrue(agents_md.exists())

        # Verify template substitution with POSIX path
        dest_posix = self.dest_root.resolve().as_posix()
        self.assertIn(dest_posix, muse_skill.read_text(encoding="utf-8"))
        policy_content = (af_dir / "GLOBAL_POLICY.md").read_text(encoding="utf-8")
        self.assertIn(dest_posix, policy_content)
        self.assertNotIn("{{CODEX_ROOT}}", policy_content)
        self.assertIn("gpt-6-luna", policy_content)
        self.assertIn("claude-opus-5-5", policy_content)
        self.assertNotIn("gpt-5.6-", policy_content)

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

        # The installed policy is generated from this package, not a separate rule copy.
        expected_policy = (self.source_root / 'GLOBAL_POLICY.md').read_text(encoding='utf-8').replace(
            '{{CODEX_ROOT}}', dest_posix)
        self.assertEqual(policy_content, expected_policy)
        self.assertIn(expected_policy.strip(), agents_content)
        source_task = json.loads((self.source_root / 'task-template.json').read_text(encoding='utf-8'))
        installed_task = json.loads((af_dir / 'task-template.json').read_text(encoding='utf-8'))
        self.assertEqual(installed_task, source_task)
        self.assertFalse(installed_task['coverage']['required'])
        self.assertEqual(installed_task['coverage']['items'][0]['status'], 'planned')

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
        self.assertEqual(routing.get("version"), 12)
        self.assertIn("providers", routing)
        self.assertIn("gemini", routing["providers"])
        self.assertNotIn("deepseek", routing["providers"])
        self.assertIn("claude", routing["providers"])
        self.assertEqual(routing["providers"]["muse"]["model"], "muse-wonder-1.0")
        self.assertEqual(routing["providers"]["gemini"]["timeout_seconds"], 3600)
        self.assertEqual(routing["providers"]["claude"]["model"], "claude-opus-5-5")
        self.assertEqual(routing["providers"]["claude"]["effort"], "medium")

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

    def test_muse_upgrade_preserves_choices_and_quota_state(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 9
        data["providers"]["muse"] = {"executable": "custom-muse", "model": "cli-default",
                                     "effort": "high", "heartbeat_seconds": 0, "custom": "keep"}
        data["providers"]["gemini"]["model"] = "operator-choice"
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        quota = self.dest_root / "agent-framework/state/muse-quota.json"
        quota.parent.mkdir(parents=True, exist_ok=True)
        quota.write_bytes(b'{"retry_at":4070908800,"operator":"keep"}')
        expected_quota = quota.read_bytes()
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        resolved = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["version"], 12)
        self.assertEqual(resolved["providers"]["muse"]["executable"], "custom-muse")
        self.assertEqual(resolved["providers"]["muse"]["model"], "cli-default")
        self.assertEqual(resolved["providers"]["muse"]["effort"], "high")
        self.assertEqual(resolved["providers"]["muse"]["heartbeat_seconds"], 0)
        self.assertEqual(resolved["providers"]["muse"]["custom"], "keep")
        self.assertEqual(resolved["providers"]["muse"]["max_model_steps"], 100)
        self.assertEqual(resolved["providers"]["gemini"]["model"], "operator-choice")
        self.assertEqual(quota.read_bytes(), expected_quota)
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root), muse_override="new-muse")
        overridden = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(overridden["providers"]["muse"]["executable"], "new-muse")
        self.assertEqual(overridden["providers"]["muse"]["model"], "cli-default")
        self.assertEqual(quota.read_bytes(), expected_quota)

    def test_v9_without_muse_adds_defaults_and_preserves_other_settings(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 9
        data["providers"].pop("muse")
        data["providers"]["gemini"]["heartbeat_seconds"] = 0
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch("install.shutil.which", return_value=None):
            install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        resolved = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["version"], 12)
        self.assertEqual(resolved["providers"]["muse"]["executable"], "muse")
        self.assertEqual(resolved["providers"]["muse"]["model"], "muse-wonder-1.0")
        self.assertEqual(resolved["providers"]["gemini"]["heartbeat_seconds"], 0)
        self.assertTrue((self.dest_root / "skills/ask-muse/SKILL.md").is_file())

    def test_muse_profiles_fresh_and_v10_upgrade(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        fresh = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(fresh["version"], 12)
        self.assertEqual(fresh["providers"]["muse"]["profiles"], {
            "complex": {"model": "muse-wonder-full-1.0", "effort": "medium"},
            "escalation": {"model": "muse-wonder-full-1.0", "effort": "high"}})
        # Simulate v10: no profiles, custom base/executable/timing preserved.
        data = json.loads(json.dumps(fresh))
        data["version"] = 10
        muse = data["providers"]["muse"]
        muse.pop("profiles")
        muse.update(executable="custom-muse", model="cli-default", effort="low",
                    timeout_seconds=123, custom="keep")
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        quota = self.dest_root / "agent-framework/state/muse-quota.json"
        quota.parent.mkdir(parents=True, exist_ok=True)
        quota.write_bytes(b'{"retry_at":4070908800}')
        resolved = resolve_routing(self.source_root, self.dest_root)
        self.assertEqual(resolved["version"], 12)
        muse = resolved["providers"]["muse"]
        self.assertEqual((muse["executable"], muse["model"], muse["effort"], muse["timeout_seconds"], muse["custom"]),
                         ("custom-muse", "cli-default", "low", 123, "keep"))
        self.assertEqual(muse["profiles"]["complex"], {"model": "muse-wonder-full-1.0", "effort": "medium"})
        self.assertEqual(quota.read_bytes(), b'{"retry_at":4070908800}')
        # Positional calling convention still works.
        self.assertEqual(resolve_routing(self.source_root, self.dest_root, None, None, False)["version"], 12)

    def test_muse_profiles_partial_custom_and_repeat(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 10
        data["providers"]["muse"]["profiles"] = {
            "complex": {"model": "operator-full"},
            "escalation": {"effort": "xhigh"}}
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        resolved = resolve_routing(self.source_root, self.dest_root)
        profiles = resolved["providers"]["muse"]["profiles"]
        self.assertEqual(profiles["complex"], {"model": "operator-full", "effort": "medium"})
        self.assertEqual(profiles["escalation"], {"model": "muse-wonder-full-1.0", "effort": "xhigh"})
        routing_path.write_text(json.dumps(resolved), encoding="utf-8")
        self.assertEqual(resolve_routing(self.source_root, self.dest_root), resolved)

    def test_muse_profiles_malformed_rejected_without_overwrite(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        baseline = json.loads(routing_path.read_text(encoding="utf-8"))
        bad_profiles = [[], {"complex": "x"}, {"complex": {"model": ""}},
                        {"complex": {"effort": "bogus"}},
                        {"complex": {"model": "x", "timeout_seconds": 1}},
                        {"unknown": {"model": "x", "effort": "medium"}}]
        for profiles in bad_profiles:
            with self.subTest(profiles=profiles):
                data = json.loads(json.dumps(baseline))
                data["providers"]["muse"]["profiles"] = profiles
                routing_path.write_text(json.dumps(data), encoding="utf-8")
                before = routing_path.read_bytes()
                with self.assertRaises(PreflightError):
                    resolve_routing(self.source_root, self.dest_root)
                self.assertEqual(routing_path.read_bytes(), before)

    def test_fresh_muse_launcher_discovery(self):
        muse_path = str(self.test_dir / "muse.cmd")
        with mock.patch("install.shutil.which", side_effect=lambda command: muse_path if command == "muse" else None):
            install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing = json.loads((self.dest_root / "agent-framework/routing.json").read_text(encoding="utf-8"))
        self.assertEqual(routing["providers"]["muse"]["executable"], os.path.abspath(muse_path))

    def test_jev_defaults_and_preserving_explicit_settings(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing = self.dest_root / 'agent-framework/routing.json'
        original = json.loads(routing.read_text(encoding='utf-8'))
        self.assertTrue(original['capabilities']['jev']['enabled'])
        self.assertEqual(original['capabilities']['jev']['authorization_mode'], 'all_workspaces')
        self.assertEqual(original['capabilities']['jev']['allowed_roots'], [])
        old = dict(original)
        old.pop('capabilities')
        routing.write_text(json.dumps(old), encoding='utf-8')
        migrated = resolve_routing(self.source_root, self.dest_root)
        self.assertEqual(migrated, original)
        customized = original['capabilities']['jev']
        customized.update(enabled=True, allowed_roots=['C:/authorized/project'], max_candidates=25)
        original['capabilities']['unrelated'] = {'preserve': True}
        routing.write_text(json.dumps(original), encoding='utf-8')
        self.assertEqual(resolve_routing(self.source_root, self.dest_root), original)

    def test_jev_preserves_disabled_and_legacy_allowlist(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / 'agent-framework/routing.json'
        original = json.loads(routing_path.read_text(encoding='utf-8'))
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                jev = original['capabilities']['jev']
                jev.pop('authorization_mode', None)
                jev.update(enabled=enabled, allowed_roots=['C:/authorized/project'])
                routing_path.write_text(json.dumps(original), encoding='utf-8')
                result = resolve_routing(self.source_root, self.dest_root)['capabilities']['jev']
                self.assertIs(result['enabled'], enabled)
                self.assertEqual(result['authorization_mode'], 'allowed_roots')
                self.assertEqual(result['allowed_roots'], ['C:/authorized/project'])

    def test_malformed_jev_capability_is_not_silently_replaced(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing = self.dest_root / 'agent-framework/routing.json'
        original = json.loads(routing.read_text(encoding='utf-8'))
        for bad in ([], {'jev': False}, {'jev': {'enabled': 'true'}},
                    {'jev': {'authorization_mode': None}},
                    {'jev': {'authorization_mode': 'all'}}):
            with self.subTest(capabilities=bad):
                original['capabilities'] = bad
                routing.write_text(json.dumps(original), encoding='utf-8')
                with self.assertRaises(PreflightError):
                    resolve_routing(self.source_root, self.dest_root)

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

    def test_v8_review_upgrade_preserves_settings_state_and_is_idempotent(self):
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 7
        data["providers"]["claude"].update({
            "model": "claude-opus-5", "executable": "/custom/claude",
            "effort": "high", "heartbeat_seconds": 0, "timeout_seconds": 1234,
        })
        data["providers"]["deepseek"] = {"enabled": False}
        data["providers"]["gemini"]["model"] = "custom-gemini"
        data["custom"] = {"preserve": True}
        routing_path.write_text(json.dumps(data), encoding="utf-8")
        quota = self.dest_root / "agent-framework/state/claude-quota.json"
        quota.parent.mkdir(parents=True)
        quota.write_bytes(b'{"model":"claude-opus-5","retry_at":4070908800}')
        original_quota = quota.read_bytes()
        config = self.dest_root / "config.toml"
        config.write_bytes(b'model = "gpt-6-astra"\r\n')
        role = self.dest_root / "agents/implementer.toml"
        role.write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")

        install_framework(str(self.dest_root), source=str(self.source_root))
        data["version"] = 12
        data["providers"]["claude"]["model"] = "claude-opus-5-5"
        data["providers"].pop("deepseek", None)
        self.assertEqual(json.loads(routing_path.read_text(encoding="utf-8")), data)
        self.assertEqual(quota.read_bytes(), original_quota)
        self.assertEqual(config.read_bytes(), b'model = "gpt-6-astra"\r\n')
        self.assertEqual(tomllib.loads(role.read_text(encoding="utf-8"))["model"], "gpt-6-luna")
        self.assertIn("claude-opus-5-5", (self.dest_root / "AGENTS.md").read_text(encoding="utf-8"))

        installed = routing_path.read_bytes()
        install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(routing_path.read_bytes(), installed)
        self.assertEqual(quota.read_bytes(), original_quota)

    def test_v8_review_migration_respects_version_and_custom_models(self):
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        baseline = routing_path.read_text(encoding="utf-8")
        cases = [(version, "claude-opus-5", "claude-opus-5-5") for version in (4, 5, 6, 7)]
        cases += [(7, "custom-opus", "custom-opus"), (8, "claude-opus-5", "claude-opus-5"),
                  (10, "custom-future", "custom-future")]
        for version, before, after in cases:
            with self.subTest(version=version, model=before):
                data = json.loads(baseline)
                data["version"] = version
                data["providers"]["claude"]["model"] = before
                routing_path.write_text(json.dumps(data), encoding="utf-8")
                resolved = resolve_routing(self.source_root, self.dest_root)
                data["version"] = max(version, 12)
                data["providers"]["claude"]["model"] = after
                self.assertEqual(resolved, data)

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
        self.assertEqual(migrated["version"], 12)
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
        self.assertEqual(migrated["version"], 12)
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
        self.assertEqual(migrated["version"], 12)
        self.assertEqual(migrated["providers"]["gemini"]["timeout_seconds"], 2700)

    def test_preserved_routing_fills_missing_universal_heartbeats_only(self):
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["providers"]["gemini"]["heartbeat_seconds"] = 0
        data["providers"]["claude"].pop("heartbeat_seconds")
        routing_path.write_text(json.dumps(data), encoding="utf-8")

        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        migrated = json.loads(routing_path.read_text(encoding="utf-8"))["providers"]
        self.assertEqual(migrated["gemini"]["heartbeat_seconds"], 0)
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
        # Path.home patches do not cross process boundaries. Keep the reader
        # registration and CLI discovery out of the operator's actual home.
        child_env = dict(os.environ, USERPROFILE=str(self.fake_home), HOME=str(self.fake_home))
        # CLI dry run
        res_dry = subprocess.run(
            [sys.executable, str(self.source_root / "install.py"), "--codex-home", str(self.dest_root), "--dry-run"],
            capture_output=True,
            text=True,
            env=child_env,
        )
        self.assertEqual(res_dry.returncode, 0, res_dry.stderr)
        self.assertIn("Dry run requested", res_dry.stdout)
        self.assertFalse(self.dest_root.exists())

        # CLI real run
        res_real = subprocess.run(
            [sys.executable, str(self.source_root / "install.py"), "--codex-home", str(self.dest_root)],
            capture_output=True,
            text=True,
            env=child_env,
        )
        self.assertEqual(res_real.returncode, 0, res_real.stderr)
        self.assertTrue((self.dest_root / "agent-framework" / "install-manifest.json").exists())
        self.assertTrue((self.fake_home / '.gemini/config/agents/framework-reader.md').is_file())

    def make_link(self, link, target, directory=True):
        if sys.platform == 'win32' and directory:
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, text=True)
            if result.returncode and 'Access is denied' in result.stderr:
                self.skipTest(f'Platform denies junction creation: {result.stderr.strip()}')
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
        for relative in ('scripts/provider_runner.py', 'scripts/test_muse_provider.py',
                         'agents/implementer.toml', 'skills/simplify/SKILL.md'):
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


    def test_fresh_install_contains_no_deepseek(self):
        """Fresh install includes Muse but no retired DeepSeek route."""
        install_framework(codex_home=str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework" / "routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(routing.get("version"), 12)
        self.assertIn("gemini", routing["providers"])
        self.assertIn("claude", routing["providers"])
        self.assertNotIn("deepseek", routing["providers"])

    def test_upgrade_from_pre_v5_and_v8_removes_deepseek_and_preserves_unrelated(self):
        """Upgrades from pre-v5 (v4) and v8 remove legacy providers.deepseek while preserving unrelated settings and quota files."""
        # Case 1: Upgrade from pre-v5 (v4) with legacy deepseek entry
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        v4_data = {
            "version": 4,
            "providers": {
                "gemini": {"executable": "agy", "model": "gemini-custom-ultra", "timeout_seconds": 3600},
                "deepseek": {"executable": "codex", "model": "deepseek-flash", "profile": "deepseek"},
                "claude": {"executable": "claude", "model": "claude-opus-5", "effort": "medium"},
            },
            "custom_field": "custom_v4_value",
        }
        routing_path.write_text(json.dumps(v4_data, indent=2), encoding="utf-8")
        quota = self.dest_root / "agent-framework/state/claude-quota.json"
        quota.parent.mkdir(parents=True, exist_ok=True)
        quota.write_bytes(b'{"model":"claude-opus-5","retry_at":4070908800}')
        original_quota = quota.read_bytes()

        install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)
        upgraded_v4 = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded_v4.get("version"), 12)
        self.assertNotIn("deepseek", upgraded_v4["providers"])
        self.assertEqual(upgraded_v4["providers"]["gemini"]["model"], "gemini-custom-ultra")
        self.assertEqual(upgraded_v4["providers"]["claude"]["model"], "claude-opus-5-5")
        self.assertEqual(upgraded_v4["custom_field"], "custom_v4_value")
        self.assertEqual(quota.read_bytes(), original_quota)

        # Case 2: Upgrade from v8 with legacy deepseek entry
        v8_data = {
            "version": 8,
            "providers": {
                "gemini": {"executable": "agy", "model": "gemini-2.5-pro", "timeout_seconds": 3600},
                "deepseek": {"executable": "codex", "model": "deepseek-flash", "enabled": True},
                "claude": {"executable": "/custom/claude", "model": "claude-opus-5-5", "effort": "high"},
            },
            "custom_v8_setting": 42,
        }
        routing_path.write_text(json.dumps(v8_data, indent=2), encoding="utf-8")
        install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)
        upgraded_v8 = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded_v8.get("version"), 12)
        self.assertNotIn("deepseek", upgraded_v8["providers"])
        self.assertEqual(upgraded_v8["providers"]["gemini"]["model"], "gemini-2.5-pro")
        self.assertEqual(upgraded_v8["providers"]["claude"]["executable"], "/custom/claude")
        self.assertEqual(upgraded_v8["custom_v8_setting"], 42)
        self.assertEqual(quota.read_bytes(), original_quota)

    def test_repeated_upgrade_idempotent(self):
        """Repeated installation on upgraded config is idempotent and does not re-add deepseek."""
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        first_installed_bytes = routing_path.read_bytes()

        # Second install
        install_framework(str(self.dest_root), source=str(self.source_root))
        second_installed_bytes = routing_path.read_bytes()
        self.assertEqual(first_installed_bytes, second_installed_bytes)
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(routing.get("version"), 12)
        self.assertNotIn("deepseek", routing["providers"])

    def test_retired_custom_credential_stays_filtered_after_upgrade(self):
        from scripts.provider_runner import child_environment, retired_secrets

        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        data = json.loads(routing_path.read_text(encoding="utf-8"))
        data["version"] = 8
        data["providers"]["deepseek"] = {"api_key_env": "CUSTOM_RETIRED_DS_KEY"}
        data["retired_secret_env_vars"] = ["OLDER_RETIRED_KEY"]
        routing_path.write_text(json.dumps(data), encoding="utf-8")

        install_framework(str(self.dest_root), source=str(self.source_root))
        upgraded = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertNotIn("deepseek", upgraded["providers"])
        self.assertEqual(upgraded["retired_secret_env_vars"],
                         ["OLDER_RETIRED_KEY", "CUSTOM_RETIRED_DS_KEY"])
        credentials = {"CUSTOM_RETIRED_DS_KEY": "fixture-retired-secret",
                       "OLDER_RETIRED_KEY": "fixture-older-secret"}
        with mock.patch.dict(os.environ, credentials):
            for provider in ("gemini", "claude", "git"):
                child = child_environment(upgraded, provider)
                self.assertTrue(all(name not in child for name in credentials))
            captured = dict(retired_secrets(upgraded))
            self.assertTrue(all(captured[name] == value for name, value in credentials.items()))
        self.assertNotIn("fixture-retired-secret", routing_path.read_text(encoding="utf-8"))
        installed = routing_path.read_bytes()
        install_framework(str(self.dest_root), source=str(self.source_root))
        self.assertEqual(routing_path.read_bytes(), installed)

    def test_deepseek_cli_flag_removed(self):
        """Passing --deepseek to install.py is rejected by argument parsing."""
        res = subprocess.run(
            [sys.executable, str(self.source_root / "install.py"), "--codex-home", str(self.dest_root), "--deepseek", "codex"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unrecognized arguments", res.stderr)

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
        self.assertEqual(routing.get("version"), 12)
        self.assertNotIn("deepseek", routing["providers"])
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

    def test_workflow_default_merged_on_upgrade(self):
        """Ordinary install merges missing workflow.require_jev_evidence=true default while preserving config."""
        # 1. Install initially
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        # Strip workflow to simulate legacy pre-workflow configuration
        routing.pop("workflow", None)
        routing["capabilities"]["jev"]["enabled"] = True
        routing["capabilities"]["jev"]["allowed_roots"] = ["C:/dev/test"]
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")

        # 2. Upgrade without refresh_routing
        install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)

        # 3. Verify workflow.require_jev_evidence merged, Jev enabled/allowed_roots preserved
        upgraded = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertIn("workflow", upgraded)
        self.assertIs(upgraded["workflow"].get("require_jev_evidence"), True)
        self.assertIs(upgraded["capabilities"]["jev"]["enabled"], True)
        self.assertEqual(upgraded["capabilities"]["jev"]["allowed_roots"], ["C:/dev/test"])

    def test_workflow_explicit_false_and_true_preserved_on_upgrade(self):
        """Explicit operator workflow settings (true/false) and custom fields are preserved on upgrade."""
        for explicit_value in (False, True):
            with self.subTest(explicit_value=explicit_value):
                install_framework(str(self.dest_root), source=str(self.source_root))
                routing_path = self.dest_root / "agent-framework/routing.json"
                routing = json.loads(routing_path.read_text(encoding="utf-8"))
                routing["workflow"] = {
                    "require_jev_evidence": explicit_value,
                    "custom_unrelated_setting": "preserved_value",
                }
                routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")

                install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)

                upgraded = json.loads(routing_path.read_text(encoding="utf-8"))
                self.assertIs(upgraded["workflow"].get("require_jev_evidence"), explicit_value)
                self.assertEqual(upgraded["workflow"].get("custom_unrelated_setting"), "preserved_value")

    def test_workflow_validation_rejects_invalid_types(self):
        """Non-dict workflow or non-boolean require_jev_evidence raises PreflightError."""
        install_framework(str(self.dest_root), source=str(self.source_root))
        routing_path = self.dest_root / "agent-framework/routing.json"

        # Non-dict workflow
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        routing["workflow"] = "not_a_dict"
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)

        # Non-bool require_jev_evidence (string)
        routing["workflow"] = {"require_jev_evidence": "true"}
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)

        # Non-bool require_jev_evidence (int)
        routing["workflow"] = {"require_jev_evidence": 1}
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")
        with self.assertRaises(PreflightError):
            install_framework(str(self.dest_root), source=str(self.source_root), refresh_routing=False)


if __name__ == "__main__":
    unittest.main()
