#!/usr/bin/env python3
"""Install the framework using Python 3.11+ and the standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

BEGIN_MARKER = '<!-- BEGIN CODEX MULTI-PROVIDER FRAMEWORK -->'
END_MARKER = '<!-- END CODEX MULTI-PROVIDER FRAMEWORK -->'
ROLE_FILES = (
    'complex-implementer', 'correctness-gate', 'docs-researcher', 'explorer',
    'implementer', 'planner', 'quality-gate-max', 'refactor-auditor',
    'reviewer', 'security-reviewer', 'test-engineer', 'verifier',
)


class InstallError(Exception):
    pass


class PreflightError(InstallError):
    pass


class InstallLockError(InstallError):
    pass


def check_path(path, *, directory=False):
    """Check the lexical path before resolving links, including missing leaves."""
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PreflightError(f'Refusing linked path: {component}. Use a physical path outside linked/synced folders; for source packages on macOS, try --source "$(pwd -P)".')
        expected_dir = component != path or directory
        if expected_dir and not stat.S_ISDIR(info.st_mode):
            raise PreflightError(f'Expected directory: {component}')
        if not expected_dir and not stat.S_ISREG(info.st_mode):
            raise PreflightError(f'Expected regular file: {component}')


def read_text(path):
    # Preserve surrounding personal instructions, including BOM and CRLF bytes.
    try:
        return path.read_bytes().decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PreflightError(f'Expected UTF-8 text in {path}: {exc}') from exc


def validate_agents_markers(agents_path):
    content = read_text(agents_path) if agents_path.exists() else ''
    begins = list(re.finditer(re.escape(BEGIN_MARKER), content))
    ends = list(re.finditer(re.escape(END_MARKER), content))
    marker_comments = re.findall(r'<!--[ \t]*(?:BEGIN|END)[ \t]+CODEX MULTI-PROVIDER FRAMEWORK', content, re.I)
    if len(begins) != len(ends) or len(begins) > 1 or len(marker_comments) != len(begins) + len(ends):
        raise PreflightError(f'Ambiguous or malformed policy markers: {agents_path}')
    if not begins:
        return None, None
    if begins[0].start() > ends[0].start():
        raise PreflightError(f'Reversed policy markers: {agents_path}')
    return begins[0].start(), ends[0].end()


def generate_agents_content(path, policy):
    content = read_text(path) if path.exists() else ''
    start, end = validate_agents_markers(path)
    block = f'{BEGIN_MARKER}\n{policy.rstrip()}\n{END_MARKER}'
    if start is not None:
        return content[:start] + block + content[end:]
    separator = '\n\n' if content and not content.endswith('\n\n') else ''
    return content + separator + block + '\n'


def collect_sources(source):
    """Explicit core files make incomplete source packages fail before mutation."""
    names = ['GLOBAL_POLICY.md', 'task-template.json', 'README.md',
             'routing.example.json', 'install.py', 'scripts/provider_runner.py',
             'scripts/test_provider_runner.py', 'scripts/test_install.py', 'docs/FRAMEWORK.md']
    names += [f'agents/{name}.toml' for name in ROLE_FILES]
    names += [f'skills/{name}/SKILL.md' for name in ('ask-gemini', 'ask-claude', 'simplify')]
    # Optional maintained templates, never recursive installation of state or logs.
    names += [p.relative_to(source).as_posix() for p in (source / 'docs').glob('*')
              if p.suffix in ('.md', '.json')]
    files = sorted(set(names))
    for name in files:
        path = source / name
        check_path(path)
        if not path.is_file():
            raise PreflightError(f'Required source file missing: {path}')
    return files


def resolve_routing(source, root, gemini_override=None, claude_override=None, refresh_routing=False, deepseek_override=None):
    target = root / 'agent-framework/routing.json'
    preserving = target.exists() and not refresh_routing
    config_path = target if preserving else source / 'routing.example.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise PreflightError(f'Invalid routing file {config_path}: {exc}. Repair it or use --refresh-routing to restore package defaults.') from exc
    if not isinstance(config, dict) or not isinstance(config.get('providers'), dict):
        raise PreflightError('Routing configuration must contain a providers object.')

    source_defaults = {}
    if preserving:
        source_example_path = source / 'routing.example.json'
        try:
            source_example = json.loads(source_example_path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError, OSError) as exc:
            raise PreflightError(f'Invalid source routing file {source_example_path}: {exc}. Repair it or use --refresh-routing to restore package defaults.') from exc
        if not isinstance(source_example, dict) or not isinstance(source_example.get('providers'), dict):
            raise PreflightError(f'Source routing file {source_example_path} must contain a providers object.')
        source_defaults = source_example['providers']

    target_version = config.get('version', 1) if preserving else 5
    if isinstance(target_version, bool) or not isinstance(target_version, int):
        raise PreflightError('Routing configuration version must be an integer.')

    providers_info = [
        ('gemini', 'agy', gemini_override),
        ('deepseek', 'codex', deepseek_override),
        ('claude', 'claude', claude_override)
    ]
    for provider, command, override in providers_info:
        entry = config['providers'].get(provider)
        if entry is None and preserving:
            if provider == 'deepseek':
                if target_version < 5 or override is not None:
                    default_entry = source_defaults.get('deepseek', {
                        'executable': command,
                        'profile': 'deepseek',
                        'model': 'deepseek-flash',
                        'api_key_env': 'DEEPSEEK_API_KEY'
                    })
                    entry = dict(default_entry)
                    config['providers']['deepseek'] = entry
                    if override is None:
                        discovered = shutil.which(command)
                        entry['executable'] = os.path.abspath(discovered) if discovered else command
                        if not discovered:
                            print(f'Warning: {command} not found on PATH; retaining command name.')
                else:
                    continue
            else:
                default_entry = source_defaults.get(provider, {
                    'executable': command,
                    'model': 'gemini-3.8-flash-medium' if provider == 'gemini' else 'claude-opus-5'
                })
                entry = dict(default_entry)
                config['providers'][provider] = entry
                if override is None:
                    discovered = shutil.which(command)
                    entry['executable'] = os.path.abspath(discovered) if discovered else command
                    if not discovered:
                        print(f'Warning: {command} not found on PATH; retaining command name.')

        if entry is None:
            continue

        if not isinstance(entry, dict) or not entry.get('model') or not entry.get('executable'):
            raise PreflightError(f'Invalid routing entry: {provider}')
        if override is not None:
            if not override.strip():
                raise PreflightError(f'Empty executable override: {provider}')
            entry['executable'] = override
            if not shutil.which(override):
                print(f'Warning: executable override {override!r} is not available; check its path before use.')
        elif not preserving:
            discovered = shutil.which(command)
            # Keep stable launcher paths (including Homebrew symlinks) across upgrades.
            entry['executable'] = os.path.abspath(discovered) if discovered else command
            if not discovered:
                print(f'Warning: {command} not found on PATH; retaining command name.')

    if preserving and target_version < 5:
        config['version'] = 5
    elif not preserving:
        config['version'] = 5
    return config


def build_plan(source, root, gemini_override=None, claude_override=None, refresh_routing=False, deepseek_override=None):
    files = collect_sources(source)
    plan = {}
    for name in files:
        target = root / (name if name.startswith(('agents/', 'skills/')) else 'agent-framework/' + name)
        check_path(target)
        content = (source / name).read_bytes()
        if name == 'GLOBAL_POLICY.md' or name.startswith('skills/'):
            content = content.decode('utf-8-sig').replace('{{CODEX_ROOT}}', root.as_posix()).encode('utf-8')
        plan[target] = content
    agents = root / 'AGENTS.md'
    routing = root / 'agent-framework/routing.json'
    check_path(agents)
    check_path(routing)
    policy = plan[root / 'agent-framework/GLOBAL_POLICY.md'].decode('utf-8')
    plan[agents] = generate_agents_content(agents, policy).encode('utf-8')
    data = resolve_routing(
        source=source,
        root=root,
        gemini_override=gemini_override,
        claude_override=claude_override,
        refresh_routing=refresh_routing,
        deepseek_override=deepseek_override,
    )
    plan[routing] = (json.dumps(data, indent=2) + '\n').encode('utf-8')
    return plan


def install_framework(codex_home=None, dry_run=False, gemini_override=None,
                      claude_override=None, refresh_routing=False, source=None,
                      deepseek_override=None):
    if sys.version_info < (3, 11):
        raise PreflightError('Python 3.11 or newer is required.')
    source_root = Path(source or Path(__file__).absolute().parent).expanduser().absolute()
    root = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().absolute()
    check_path(source_root, directory=True)
    check_path(root, directory=True)
    source_root, root = source_root.resolve(), root.resolve()
    if root == root.parent:
        raise PreflightError('Refusing a filesystem root as the Codex directory.')
    if source_root.is_relative_to(root) or root.is_relative_to(source_root):
        raise PreflightError('Source package and destination must not overlap. Run the installer from a separate source clone or release ZIP, not the installed copy.')
    manifest_path = root / 'agent-framework/install-manifest.json'
    backup_base = root / 'agent-framework/backups'
    lock_path = root / '.agent-framework-install.lock'
    check_path(manifest_path)
    check_path(backup_base, directory=True)
    check_path(lock_path)
    if lock_path.exists():
        raise InstallLockError(f'Existing installer lock: {lock_path}. Investigate before removing it.')
    plan = build_plan(
        source=source_root,
        root=root,
        gemini_override=gemini_override,
        claude_override=claude_override,
        refresh_routing=refresh_routing,
        deepseek_override=deepseek_override,
    )
    if dry_run:
        print('Dry run requested. No changes made.')
        print(f'Target Codex root: {root}')
        for target in plan:
            print(f'  {target}')
        return

    root.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise InstallLockError(f'Existing installer lock: {lock_path}') from exc
    backup_dir = backup_base / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
    try:
        with lock:
            lock.write(f'pid={os.getpid()}\n')
        # Re-read mutable installed settings after acquiring the installer lock.
        check_path(manifest_path)
        check_path(backup_base, directory=True)
        plan = build_plan(
            source=source_root,
            root=root,
            gemini_override=gemini_override,
            claude_override=claude_override,
            refresh_routing=refresh_routing,
            deepseek_override=deepseek_override,
        )
        backup_dir.mkdir(parents=True, exist_ok=False)
        backups = {}
        for target in [*plan, manifest_path]:
            if target.exists():
                backup = backup_dir / target.relative_to(root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[target] = str(backup)
        entries = []
        for target, content in plan.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            entries.append({'path': str(target), 'backup': backups.get(target),
                            'sha256': hashlib.sha256(content).hexdigest().upper()})
        manifest = {'version': 5, 'installed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'backup_directory': str(backup_dir), 'files': entries}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    except BaseException:
        print(f'Installation did not complete. Partial changes may exist; no automatic rollback. Backups: {backup_dir}', file=sys.stderr)
        raise
    finally:
        lock_path.unlink()
    print(f'Framework installed successfully into {root}')
    print(f'Backups stored in: {backup_dir}')
    print('Start a new Codex task to discover the installed roles and skills.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', help='Override CODEX_HOME or ~/.codex.')
    parser.add_argument('--dry-run', action='store_true', help='Validate and list destinations without writes.')
    parser.add_argument('--gemini', help='Override Gemini executable.')
    parser.add_argument('--deepseek', help='Override DeepSeek (Codex) executable.')
    parser.add_argument('--claude', help='Override Claude executable.')
    parser.add_argument('--refresh-routing', action='store_true', help='Reset routing to package defaults and rediscover CLIs.')
    parser.add_argument('--source', help='Use another complete source package.')
    args = parser.parse_args(argv)
    try:
        install_framework(
            codex_home=args.codex_home,
            dry_run=args.dry_run,
            gemini_override=args.gemini,
            claude_override=args.claude,
            refresh_routing=args.refresh_routing,
            source=args.source,
            deepseek_override=args.deepseek,
        )
    except (InstallError, OSError, ValueError) as exc:
        print(f'Installation error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
