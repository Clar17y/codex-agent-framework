"""Bounded external provider execution with shared subscription quota preflight."""
import argparse
import contextlib
import copy
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UTC = dt.timezone.utc
STREAM_POLL_SECONDS = 1.0
STREAM_PROGRESS_EMIT_SECONDS = 15.0
MAX_STREAM_LINE_BYTES = 1024 * 1024
STREAM_READ_CHUNK_BYTES = 64 * 1024
MIN_HEARTBEAT_SECONDS = 5.0
FINAL_OUTPUT_TAIL_BYTES = (2 * MAX_STREAM_LINE_BYTES) + STREAM_READ_CHUNK_BYTES
ARTIFACT_COPY_CHUNK_BYTES = 64 * 1024
CODEX_TOOL_ITEM_TYPES = frozenset(("command_execution", "file_change", "mcp_tool_call", "web_search"))
# Compatibility aliases for callers/tests written against the original
# Gemini-only telemetry implementation.
GEMINI_STREAM_POLL_SECONDS = STREAM_POLL_SECONDS
GEMINI_PROGRESS_EMIT_SECONDS = STREAM_PROGRESS_EMIT_SECONDS
MAX_EVIDENCE_BYTES = 1024 * 1024
MUSE_PROFILE_NAMES = ("routine", "complex", "escalation")
MUSE_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
READ_ACTIVITIES = frozenset(("planning", "exploration", "test_design", "independent_review",
                             "security_review", "refactor_audit", "docs_research", "deep_review"))
WRITE_ACTIVITIES = frozenset(("implementation", "implementation_complex", "implementation_long_horizon",
                              "testing", "verification", "documentation"))
PROVIDERS = frozenset(("openai", "claude", "gemini", "muse"))
ACTIVITY_EFFORTS = {"gemini": frozenset(("low", "medium", "high")),
                    "claude": frozenset(("medium", "high")),
                    "openai": frozenset(MUSE_EFFORTS), "muse": frozenset(MUSE_EFFORTS)}


def activity_routes(config, activity):
    """Validate the complete registry before selecting or launching a route."""
    profiles, activities = config.get("profiles"), config.get("activities")
    providers = config.get("providers")
    if not all(isinstance(x, dict) for x in (profiles, activities, providers)):
        raise ValueError("Routing requires providers, profiles and activities objects")
    if activity not in READ_ACTIVITIES | WRITE_ACTIVITIES | {"orchestration"}:
        raise ValueError(f"Unknown activity: {activity}")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name or not isinstance(profile, dict):
            raise ValueError(f"Invalid profile: {name!r}")
        if set(profile) - {"provider", "model", "effort", "enabled"}:
            raise ValueError(f"Unknown fields in profile {name}")
        if profile.get("provider") not in PROVIDERS or profile["provider"] not in providers:
            raise ValueError(f"Invalid provider in profile {name}")
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise ValueError(f"Invalid model in profile {name}")
        if profile.get("effort", "medium") not in ACTIVITY_EFFORTS[profile["provider"]]:
            raise ValueError(f"Invalid effort in profile {name}")
        if not isinstance(profile.get("enabled", True), bool):
            raise ValueError(f"Invalid enabled flag in profile {name}")
        entry = providers[profile["provider"]]
        if not isinstance(entry, dict) or not entry.get("executable"):
            raise ValueError(f"Invalid executable for profile {name}")
    for name, chain in activities.items():
        if name not in READ_ACTIVITIES | WRITE_ACTIVITIES | {"orchestration"}:
            raise ValueError(f"Unknown activity chain: {name}")
        if not isinstance(chain, list) or not chain or len(chain) != len(set(chain)):
            raise ValueError(f"Activity {name} requires a nonempty chain without duplicates")
        if any(profile not in profiles or not profiles[profile].get("enabled", True) for profile in chain):
            raise ValueError(f"Activity {name} references unknown or disabled profile")
    if activity not in activities:
        raise ValueError(f"Activity {activity} has no chain")
    return activities[activity]


def effective_activity_provider(config, profile_name):
    profile = config["profiles"][profile_name]
    provider_name = profile["provider"]
    provider = copy.deepcopy(config["providers"][provider_name])
    provider.pop("profiles", None)
    provider.update(model=profile["model"], effort=profile.get("effort", "medium"))
    return provider_name, provider


def validate_gemini_reader(provider, workspace):
    """Check an installed Antigravity custom agent before using its tool allowlist."""
    name = provider.get("reader_agent")
    path = provider.get("reader_agent_file")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ValueError("Gemini reader_agent must be a simple custom-agent name")
    if not isinstance(path, str) or not path:
        raise ValueError("Gemini reader_agent_file must name the installed custom-agent file")
    expected_path = (Path.home() / ".gemini" / "config" / "agents" / f"{name}.md").resolve()
    if Path(os.path.expanduser(path)).resolve() != expected_path:
        raise ValueError("Gemini reader_agent_file must name the selected global custom agent")
    workspace = Path(workspace).resolve()
    agent_roots = (Path.home() / ".gemini" / "config" / "agents",
                   workspace / ".agents" / "agents",
                   Path.home() / ".gemini" / "plugins",
                   workspace / ".agents" / "plugins")
    for root in agent_roots:
        if not root.exists():
            continue
        for shadow in root.rglob("*.md"):
            if shadow.resolve() == expected_path:
                continue
            # AGY identifies agents by YAML name, not just filename. Reject
            # aliases in any known discovery root before --agent is launched.
            with shadow.open(encoding="utf-8") as source:
                header = source.read(4096)
            frontmatter = header.split("---", 2)[1] if header.startswith("---") and header.count("---") >= 2 else ""
            declared = re.search(r"(?m)^name:\s*(.*?)\s*$", frontmatter)
            declared_name = declared.group(1).split("#", 1)[0].strip().strip("'\"") if declared else None
            if declared_name == name or shadow.stem == name or (
                    shadow.name == "agent.md" and shadow.parent.name == name):
                raise ValueError(f"Gemini agent name is ambiguous with the validated global reader: {shadow}")
    content = expected_path.read_text(encoding="utf-8")
    expected = ("---\nname: " + name + "\ndescription: Framework static reader.\n"
                "tools:\n  - view_file\n  - grep_search\n"
                "mainAgent: true\nsubagent: false\ncommandExecutionPolicy: off\n"
                "mcpServers: []\nskills: []\nplugins: []\n---\n")
    if not content.startswith(expected):
        raise ValueError("Gemini reader agent does not have the enforced read/search-only capability definition")


def resolve_muse_profile(config, profile_name=None):
    """Return a copy of the Muse provider with the selected profile applied.

    The base entry supplies routine model/effort plus all shared controls
    (executable, timing, credentials, steps). Named profiles under
    ``providers.muse.profiles`` override only model/effort. The input
    config is never mutated.
    """
    selected = profile_name or "routine"
    if selected not in MUSE_PROFILE_NAMES:
        raise ValueError(
            f"Unsupported Muse profile '{selected}'; expected one of {', '.join(MUSE_PROFILE_NAMES)}")
    providers = config.get("providers") if isinstance(config, dict) else None
    base = providers.get("muse") if isinstance(providers, dict) else None
    if not isinstance(base, dict):
        raise ValueError("Provider 'muse' is missing from config")
    profiles = base.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        raise ValueError("providers.muse.profiles must be an object")
    effective = copy.deepcopy(base)
    effective.pop("profiles", None)
    if selected != "routine":
        profile = profiles.get(selected) if isinstance(profiles, dict) else None
        if profile is None:
            raise ValueError(
                f"Muse profile '{selected}' is not configured under providers.muse.profiles")
        if not isinstance(profile, dict):
            raise ValueError(f"providers.muse.profiles.{selected} must be an object")
        unknown = sorted(set(profile) - {"model", "effort"})
        if unknown:
            raise ValueError(
                f"providers.muse.profiles.{selected} may only override model/effort; got {unknown}")
        if "model" in profile:
            if not isinstance(profile["model"], str) or not profile["model"].strip():
                raise ValueError(f"providers.muse.profiles.{selected}.model must be a nonempty string")
            effective["model"] = profile["model"]
        if "effort" in profile:
            if profile["effort"] not in MUSE_EFFORTS:
                raise ValueError(f"providers.muse.profiles.{selected}.effort is unsupported: {profile['effort']!r}")
            effective["effort"] = profile["effort"]
    model = effective.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Muse model must be a nonempty CLI model ID or cli-default")
    effort = effective.get("effort", "medium")
    if effort not in MUSE_EFFORTS:
        raise ValueError(f"Unsupported Muse reasoning effort: {effort!r}")
    effective["effort"] = effort
    effective["muse_profile"] = selected
    meta = {"muse_profile": selected, "model": model, "effort": effort}
    return effective, meta


def require_muse_profile_allowed(profile_name, provider_name, role=None):
    """Reject an explicit Muse profile on any non-Muse route or review."""
    if profile_name is None:
        return
    if profile_name not in MUSE_PROFILE_NAMES:
        raise ValueError(
            f"Unsupported Muse profile '{profile_name}'; expected one of {', '.join(MUSE_PROFILE_NAMES)}")
    if role == "review":
        raise ValueError("Muse profiles are not permitted for review; review is Claude-only")
    if provider_name != "muse":
        raise ValueError("--muse-profile is only permitted with --provider muse")

def safe_regular_file(path, workspace):
    """Load the shared Jev path checks only when evidence validation needs them."""
    try:
        from jev_search import safe_regular_file as jev_safe_regular_file
    except ImportError:
        try:
            from scripts.jev_search import safe_regular_file as jev_safe_regular_file
        except ImportError as exc:
            raise ValueError("Jev evidence checks are unavailable; reinstall the complete framework") from exc
    return jev_safe_regular_file(path, workspace)

DEEPSEEK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["SUCCESS", "FAILED", "BLOCKED", "ERROR"]
        },
        "response": {
            "type": "string"
        }
    },
    "required": ["status", "response"],
    "additionalProperties": False
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                # Windows readers/scanners can briefly deny an atomic rename.
                # Keep the old file intact and never retry inference or other I/O.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_text_tail(path, limit=FINAL_OUTPUT_TAIL_BYTES):
    """Read a bounded suffix containing only complete newline-delimited records."""
    if limit <= 0:
        raise ValueError("Tail read limit must be positive")
    path = Path(path)
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - limit)
        handle.seek(start)
        raw = handle.read(limit)
    truncated = start > 0
    if truncated:
        newline = raw.find(b"\n")
        raw = raw[newline + 1:] if newline >= 0 else b""
    return raw.decode("utf-8", errors="replace"), truncated


def workspace_state_dir(state_dir, workspace):
    identity = os.path.normcase(str(Path(workspace).resolve()))
    return state_dir / "workspaces" / hashlib.sha256(identity.encode("utf-8")).hexdigest()


def orchestration_path(state_dir, workspace, run_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
        raise ValueError("Invalid orchestration run id")
    return workspace_state_dir(state_dir, workspace) / "orchestrations" / f"{run_id}.json"


def orchestration_children(path):
    directory = path.with_suffix("") / "children"
    if not directory.exists():
        return []
    children = []
    for item in sorted(directory.glob("*.json")):
        child = read_json(item)
        pending_path = child.get("provider_pending_path")
        if child.get("status") == "unresolved" and pending_path:
            if read_json(Path(pending_path)).get("status") == "resolved":
                child.update(status="failed", result_status="operator_resolved_child")
                write_json(item, child)
        children.append(child)
    return children


def settle_orchestration_parent(path):
    parent = read_json(path)
    if (parent.get("status") == "uncertain_exit" and parent.get("local_process_stopped") is True and
            (parent.get("terminal_established") is True or parent.get("operator_confirmed_stopped") is True) and
            all(child.get("status") in ("completed", "failed") for child in orchestration_children(path))):
        parent["status"] = "resolved"
        write_json(path, parent)
    return parent


def register_child_dispatch(state_dir, workspace, run_id, owned_paths, activity=None):
    parent_path = orchestration_path(state_dir, workspace, run_id)
    with provider_lock(state_dir, 30, "orchestration.lock"):
        parent = read_json(parent_path)
        if parent.get("status") not in ("launching", "running"):
            raise ValueError("Parent orchestrator is no longer accepting children")
        scope = [os.path.normcase(str((workspace / path).resolve())) for path in parent.get("owned_paths", [])]
        child_scope = [os.path.normcase(str((workspace / path).resolve())) for path in owned_paths]
        if scope and not child_scope and activity not in READ_ACTIVITIES:
            raise ValueError("Writing child ownership is outside orchestrator scope")
        if scope and child_scope and not all(any(Path(child).is_relative_to(Path(root)) for root in scope) for child in child_scope):
            raise ValueError("Child ownership is outside orchestrator scope")
        child_path = parent_path.with_suffix("") / "children" / (str(uuid.uuid4()) + ".json")
        write_json(child_path, {"status": "running", "workspace": str(workspace),
                                "owned_paths": owned_paths, "parent_run_id": run_id})
    return child_path


def resolve_child_dispatch(state_dir, child_path, result):
    status = result.get("status")
    uncertain = (status in ("blocked_pending_run", "uncertain_exit", "timeout", "cancelled", "unresolved") or
                 result.get("fallback_blocked_by_pending") is True or
                 result.get("fallback_authorized") is False or
                 bool(result.get("cleanup")) or
                 status == "provider_error" and result.get("terminal_established") is False)
    resolution = "unresolved" if uncertain else "completed" if status == "completed" else "failed"
    with provider_lock(state_dir, 30, "orchestration.lock"):
        record = read_json(child_path)
        record.update(status=resolution, result_status=status)
        pending = result.get("pending_path")
        if not pending and result.get("attempts"):
            pending = result["attempts"][-1].get("pending_path")
        if pending:
            record["provider_pending_path"] = pending
        write_json(child_path, record)
        parent_path = child_path.parent.parent.with_suffix(".json")
        settle_orchestration_parent(parent_path)


def operator_resolve_orchestration(state_dir, workspace, run_id, reason, child_id=None):
    """Record an operator's explicit cessation confirmation without losing run evidence."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A cessation reason is required")
    state_dir = Path(state_dir).resolve()
    workspace = Path(workspace).resolve()
    parent_path = orchestration_path(state_dir, workspace, run_id)
    with provider_lock(state_dir, 30, "orchestration.lock"):
        parent = read_json(parent_path)
        recorded_workspace = parent.get("workspace")
        if (not isinstance(recorded_workspace, str) or not Path(recorded_workspace).is_absolute() or
                os.path.normcase(str(Path(recorded_workspace).resolve())) != os.path.normcase(str(workspace))):
            raise ValueError("Orchestration workspace does not match")
        target = parent_path
        if child_id:
            if not re.fullmatch(r"[0-9a-f-]{36}", child_id):
                raise ValueError("Invalid child run id")
            target = parent_path.with_suffix("") / "children" / f"{child_id}.json"
            child = read_json(target)
            if child.get("parent_run_id") != run_id:
                raise ValueError("Child is not registered under this orchestrator")
            pending = child.get("provider_pending_path")
            if pending and read_json(Path(pending)).get("status") != "resolved":
                raise ValueError("Resolve the provider pending record after cessation confirmation first")
            if child.get("status") not in ("completed", "failed"):
                child.update(status="failed", result_status="operator_resolved_child",
                             operator_resolution={"reason": reason.strip(), "at": dt.datetime.now(UTC).isoformat()})
                write_json(target, child)
        else:
            if any(child.get("status") not in ("completed", "failed") for child in orchestration_children(parent_path)):
                raise ValueError("Resolve every child before confirming the parent stopped")
            if parent.get("status") != "resolved":
                parent.update(status="uncertain_exit", local_process_stopped=True,
                              operator_confirmed_stopped=True,
                              operator_resolution={"reason": reason.strip(), "at": dt.datetime.now(UTC).isoformat()})
                write_json(parent_path, parent)
        parent = settle_orchestration_parent(parent_path)
    return {"status": parent.get("status"), "orchestration_pending_path": str(parent_path),
            "resolved_record": str(target)}


def normalized_owned_paths(workspace, owned_paths):
    """Return canonical workspace-relative claims; an empty list claims all files."""
    if not isinstance(owned_paths, list) or not all(isinstance(path, str) and path for path in owned_paths):
        raise ValueError("owned_paths must be a list of non-empty paths")
    normalized = set()
    for name in owned_paths:
        if any(character in name for character in "*?"):
            raise ValueError("Owned paths must be literal paths; wildcards are not allowed")
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("Owned paths must remain within workspace")
        relative = path.relative_to(workspace).as_posix()
        if relative == ".":
            return []
        normalized.add(os.path.normcase(relative))
    return sorted(normalized)


def claims_overlap(first_workspace, first, second_workspace, second):
    """Compare canonical claims; an empty claim is its workspace directory."""
    left_claims = first or [os.path.normcase(str(Path(first_workspace).resolve()))]
    right_claims = second or [os.path.normcase(str(Path(second_workspace).resolve()))]
    for left in left_claims:
        for right in right_claims:
            left_path, right_path = Path(left), Path(right)
            if left_path.is_relative_to(right_path) or right_path.is_relative_to(left_path):
                return True
    return False


def pending_records(workspace_state):
    """Read both the old workspace marker and the per-run record directory."""
    paths = [workspace_state / "gemini-pending.json"]
    directory = workspace_state / "gemini-pending"
    try:
        directory_mode = directory.stat().st_mode
    except FileNotFoundError:
        directory_mode = None
    except OSError as exc:
        return [(directory, pending_inspection_error(directory, exc))]
    if directory_mode is not None:
        if not stat.S_ISDIR(directory_mode):
            return [(directory, pending_inspection_error(directory, ValueError("expected a directory")))]
        try:
            paths.extend(sorted(directory.glob("*.json")))
        except OSError as exc:
            return [(directory, pending_inspection_error(directory, exc))]
    records = []
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            records.append((path, pending_inspection_error(path, exc)))
            continue
        try:
            records.append((path, read_json(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # An unreadable record is uncertain and must block this workspace.
            records.append((path, pending_inspection_error(path, exc)))
    return records


def pending_inspection_error(path, exc):
    """Represent unreadable ownership evidence as an unresolved, blocking record."""
    return {
        "status": "unreadable",
        "state_error": f"Pending ownership state could not be read at {path}: {type(exc).__name__}: {exc}",
    }


def pending_claims(pending):
    """Legacy or malformed ownership deliberately fails closed to the workspace."""
    if not isinstance(pending, dict):
        return None, []
    owner = pending.get("workspace")
    if not isinstance(owner, str) or not Path(owner).is_absolute():
        return None, []
    workspace = Path(owner).resolve()
    claims = pending.get("owned_paths")
    if claims is None:
        return workspace, []
    try:
        return workspace, [os.path.normcase(str((workspace / path).resolve())) for path in normalized_owned_paths(workspace, claims)]
    except ValueError:
        return workspace, []


def all_pending_records(state_dir):
    """Find per-workspace records so nested workspace aliases share claims."""
    root = state_dir / "workspaces"
    try:
        root_mode = root.stat().st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [(root, pending_inspection_error(root, exc))]
    if not stat.S_ISDIR(root_mode):
        return [(root, pending_inspection_error(root, ValueError("expected a directory")))]
    try:
        workspace_states = list(root.iterdir())
    except OSError as exc:
        return [(root, pending_inspection_error(root, exc))]
    records = []
    for workspace_state in workspace_states:
        try:
            workspace_mode = workspace_state.stat().st_mode
        except OSError as exc:
            records.append((workspace_state, pending_inspection_error(workspace_state, exc)))
            continue
        if stat.S_ISDIR(workspace_mode):
            records.extend(pending_records(workspace_state))
    return records


def all_orchestration_records(state_dir):
    """Find parent claims in every workspace, including unreadable state."""
    root = state_dir / "workspaces"
    if not root.exists():
        return []
    records = []
    try:
        for workspace_state in root.iterdir():
            directory = workspace_state / "orchestrations"
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                try:
                    records.append((path, read_json(path)))
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    records.append((path, pending_inspection_error(path, exc)))
    except OSError as exc:
        records.append((root, pending_inspection_error(root, exc)))
    return records


def legacy_pending_for(state_dir, workspace):
    """Preserve old evidence, but scope identifiable legacy runs to their workspace."""
    path = state_dir / "gemini-pending.json"
    try:
        path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return pending_inspection_error(path, exc)
    try:
        pending = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return pending_inspection_error(path, exc)
    if not isinstance(pending, dict):
        return {}
    if pending.get("status") == "resolved":
        return None
    owner = pending.get("workspace")
    if not owner and pending.get("logs"):
        logs = Path(pending["logs"])
        if logs.parent.name == "agent-framework" and logs.parent.parent.name == ".llm-output":
            owner = str(logs.parent.parent.parent)
    if owner and Path(owner).is_absolute():
        if not claims_overlap(owner, [], workspace, []):
            return None
    return pending


@contextlib.contextmanager
def provider_lock(directory, timeout, name="gemini.lock"):
    """OS-owned lock releases on process exit, including crashes."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Provider coordination lock is busy")
                time.sleep(0.1)
        released = False
        def release():
            nonlocal released
            if released:
                return
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            released = True
        try:
            yield release
        finally:
            release()


def validate_coverage_task(task):
    """Validate optional coverage bookkeeping, never the truth/completeness of claims."""
    if "coverage" not in task:
        return
    coverage = task["coverage"]
    if not isinstance(coverage, dict):
        raise ValueError("Task coverage must be an object")
    required = coverage.get("required", False)
    if not isinstance(required, bool):
        raise ValueError("Task coverage.required must be a boolean")
    items = coverage.get("items")
    if not isinstance(items, list):
        raise ValueError("Task coverage.items must be a list")
    if required and not items:
        raise ValueError("Task coverage.required requires non-empty coverage.items")
    for index, item in enumerate(items):
        label = f"Task coverage.items[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        for field in ("invariant", "check"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}.{field} must be a non-empty string")
        cases = item.get("cases")
        if not isinstance(cases, list) or not cases or any(
                not isinstance(case, str) or not case.strip() for case in cases):
            raise ValueError(f"{label}.cases must be a non-empty list of non-empty strings")
        status = item.get("status")
        if status not in ("planned", "proved", "unproved"):
            raise ValueError(f"{label}.status must be 'planned', 'proved' or 'unproved'")
        for field in ("evidence", "limitation"):
            if field in item and (not isinstance(item[field], str) or not item[field].strip()):
                raise ValueError(f"{label}.{field} must be a non-empty string when supplied")
        if status == "proved" and "evidence" not in item:
            raise ValueError(f"{label} with status 'proved' requires evidence")
        if status == "unproved" and "limitation" not in item:
            raise ValueError(f"{label} with status 'unproved' requires a limitation")
        if status == "planned" and "evidence" in item:
            raise ValueError(f"{label} with status 'planned' cannot claim execution evidence")


def prompt_for(workspace, task_path, task=None, activity=None):
    task = read_json(task_path) if task is None else task
    for field in ("objective", "acceptance_criteria", "owned_paths", "validation", "instructions_files"):
        if field not in task:
            raise ValueError("Missing task contract field: " + field)
    validate_coverage_task(task)
    paths = list(task["instructions_files"])
    for name in ("AGENTS.md", "CLAUDE.md"):
        if (workspace / name).is_file() and name not in paths:
            paths.insert(0, name)
    instructions = []
    for name in paths:
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("Instruction files must remain within workspace: " + name)
        instructions.append(f"\n--- {name} ---\n{path.read_text(encoding='utf-8-sig')}")
    for name in task["owned_paths"]:
        if not (workspace / name).resolve().is_relative_to(workspace):
            raise ValueError("Owned paths must remain within workspace")
    if activity:
        capabilities = ("Read and search only; do not write or run commands." if activity in READ_ACTIVITIES else
                        "Orchestrate child tasks by calling this framework's dispatch command with explicit nonoverlapping ownership; wait for results and integrate evidence. Do not claim the whole workspace." if activity == "orchestration" else
                        "Write only owned paths and complete assigned checks; do not delegate unless task explicitly authorizes it.")
        return (f"ACTIVITY {activity}: {capabilities} Use the pinned provider/model. "
                "Preserve unrelated changes and partial work. Report checks, unresolved issues and evidence.\nTASK CONTRACT\n"
                + json.dumps(task, indent=2) + "\nREPOSITORY INSTRUCTIONS\n" + "\n".join(instructions))
    return ("USER ROUTING OVERRIDE: Execute the assigned task yourself with the pinned provider. "
            "Never spawn subagents or invoke another AI provider. Never use Sonnet or Fable. "
            "These user instructions override conflicting repository model-routing guidance. "
            "Complete only the supplied task. Follow applicable repository instructions. "
            "Preserve unrelated changes. Do not commit, push, reset, or delete work. "
            "Read nearest scoped CLAUDE.md for every area touched. "
            "Report changes, checks, unresolved issues and evidence. "
            "When coverage is supplied, distinguish planned checks from proved cases with concrete "
            "candidate-specific evidence/results; identify missing or unproved cases and limitations "
            "in the handoff. Structural validation does not prove semantic completeness.\nTASK CONTRACT\n"
            + json.dumps(task, indent=2) + "\nREPOSITORY INSTRUCTIONS\n" + "\n".join(instructions))


def read_bounded_evidence_file(file_path_str, workspace, max_bytes=MAX_EVIDENCE_BYTES):
    path = Path(file_path_str)
    if ".." in path.parts:
        raise ValueError(f"Evidence path contains parent traversal: {file_path_str}")
    target = path if path.is_absolute() else (workspace / path)
    target_abs = Path(os.path.abspath(target))
    workspace_abs = Path(os.path.abspath(workspace))
    if not safe_regular_file(target_abs, workspace_abs):
        raise ValueError(f"Evidence file must be a regular file inside workspace without links or hardlinks: {file_path_str}")
    try:
        size = target_abs.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot stat evidence file '{file_path_str}': {exc}") from exc
    if size > max_bytes:
        raise ValueError(f"Evidence file exceeds size limit ({size} > {max_bytes} bytes): {file_path_str}")
    try:
        with target_abs.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            raw = stream.read(max_bytes + 1)
        current_stat = target_abs.stat()
        if (opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size) != (current_stat.st_dev, current_stat.st_ino, current_stat.st_size):
            raise ValueError(f"Evidence file changed during read: {file_path_str}")
        if not safe_regular_file(target_abs, workspace_abs):
            raise ValueError(f"Evidence file link status changed during read: {file_path_str}")
    except OSError as exc:
        raise ValueError(f"Cannot read evidence file '{file_path_str}': {exc}") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"Evidence file exceeds size limit: {file_path_str}")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError(f"Evidence file is UTF-16; save it as UTF-8: {file_path_str}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"Evidence file must be UTF-8: {file_path_str}") from exc
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Evidence file is malformed JSON: {file_path_str}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Evidence file must contain a JSON object: {file_path_str}")
    return data


def validate_jev_task(task, workspace, role):
    stage_name = "search" if role == "implement" else "review"
    jev_section = task.get("jev")
    if jev_section is None or not isinstance(jev_section, dict):
        raise ValueError(f"Task contract missing required 'jev' object for {role} role")
    if stage_name not in jev_section:
        raise ValueError(f"Task contract missing required 'jev.{stage_name}' object for {role} role")
    stage = jev_section[stage_name]
    if not isinstance(stage, dict):
        raise ValueError(f"Task contract 'jev.{stage_name}' must be an object")

    decision_status = stage.get("status")
    if decision_status not in ("attempted", "not_applicable"):
        raise ValueError(f"Task jev.{stage_name}.status must be 'attempted' or 'not_applicable'; got {decision_status!r}")

    if decision_status == "not_applicable":
        reason = stage.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Task jev.{stage_name} with status 'not_applicable' requires a non-empty string 'reason'")
        return {
            "stage": stage_name,
            "decision": "not_applicable",
            "reason": reason.strip(),
        }

    # decision_status == "attempted"
    result_path = stage.get("result_path")
    if not isinstance(result_path, str) or not result_path.strip():
        raise ValueError(f"Task jev.{stage_name} with status 'attempted' requires a non-empty string 'result_path'")
    result_path_str = result_path.strip()

    evidence = read_bounded_evidence_file(result_path_str, workspace)

    cmd = evidence.get("command")
    if cmd in ("doctor", "inspect"):
        raise ValueError(f"Diagnostic {cmd} output rejected as actual Jev evidence: {result_path_str}")
    if cmd != stage_name:
        raise ValueError(f"Evidence command '{cmd}' does not match required stage '{stage_name}': {result_path_str}")

    helper_status = evidence.get("status")
    if helper_status not in ("complete", "partial", "unavailable", "error"):
        raise ValueError(f"Jev evidence status must be 'complete', 'partial', 'unavailable', or 'error'; got {helper_status!r}")

    if helper_status == "complete" and isinstance(evidence.get("coverage"), dict) and evidence["coverage"].get("complete") is False:
        raise ValueError(f"Fabricated success: helper status is 'complete' but coverage.complete is false: {result_path_str}")

    if stage_name == "search":
        evidence_ws = evidence.get("workspace")
        if evidence_ws is not None and isinstance(evidence_ws, str) and evidence_ws.strip():
            try:
                ev_resolved = Path(evidence_ws).resolve()
                ws_resolved = Path(workspace).resolve()
                if os.path.normcase(str(ev_resolved)) != os.path.normcase(str(ws_resolved)):
                    raise ValueError(
                        f"Search evidence workspace mismatch: evidence workspace '{evidence_ws}' does not match active workspace '{workspace}'"
                    )
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"Invalid workspace path in search evidence: {exc}") from exc

    if stage_name == "review":
        if helper_status in ("complete", "partial"):
            diff_sha = evidence.get("diff_sha256")
            if not isinstance(diff_sha, str) or not diff_sha.strip():
                raise ValueError(f"Successful/partial review evidence must include 'diff_sha256': {result_path_str}")

            review_info = task.get("review")
            if not isinstance(review_info, dict) or not review_info.get("diff_path") or not isinstance(review_info["diff_path"], str) or not review_info["diff_path"].strip():
                raise ValueError(f"Review task requires non-empty 'review.diff_path' to bind with review evidence")
            diff_path_str = review_info["diff_path"].strip()

            diff_path = Path(diff_path_str)
            if ".." in diff_path.parts:
                raise ValueError(f"Review diff path contains parent traversal: {diff_path_str}")
            diff_target = diff_path if diff_path.is_absolute() else (workspace / diff_path)
            diff_target_abs = Path(os.path.abspath(diff_target))
            if not safe_regular_file(diff_target_abs, Path(os.path.abspath(workspace))):
                raise ValueError(f"Review diff file must be a regular file inside workspace without links or hardlinks: {diff_path_str}")
            try:
                with diff_target_abs.open("rb") as f:
                    diff_raw = f.read(MAX_EVIDENCE_BYTES + 1)
            except OSError as exc:
                raise ValueError(f"Cannot read review diff file '{diff_path_str}': {exc}") from exc
            if len(diff_raw) > MAX_EVIDENCE_BYTES:
                raise ValueError(f"Review diff file exceeds size limit: {diff_path_str}")
            actual_diff_sha = hashlib.sha256(diff_raw).hexdigest()
            if diff_sha.strip().lower() != actual_diff_sha.lower():
                raise ValueError(
                    f"Review evidence diff hash mismatch: evidence diff_sha256 '{diff_sha}' does not match diff file SHA-256 '{actual_diff_sha}' ({diff_path_str})"
                )

    jev_meta = {
        "stage": stage_name,
        "decision": "attempted",
        "result_path": result_path_str,
        "status": helper_status,
    }
    if "coverage" in evidence and evidence["coverage"] is not None:
        jev_meta["coverage"] = evidence["coverage"]
    if "diff_sha256" in evidence and evidence["diff_sha256"] is not None:
        jev_meta["diff_sha256"] = evidence["diff_sha256"]
    if "error" in evidence and evidence["error"] is not None:
        jev_meta["error"] = evidence["error"]
    if "focus_count" in evidence:
        jev_meta["focus_count"] = evidence["focus_count"]
    if "unknown_count" in evidence:
        jev_meta["unknown_count"] = evidence["unknown_count"]

    return jev_meta


def direct_windows_codex_command(command, platform_name=None):
    """Replace an npm .cmd shim with its direct Node argv, without invoking cmd.exe."""
    if (platform_name or os.name) != "nt":
        return command
    launcher = Path(command[0])
    discovered = shutil.which(command[0])
    if discovered:
        launcher = Path(discovered)
    if launcher.suffix.lower() not in (".cmd", ".bat"):
        return command

    launcher = launcher.resolve()
    try:
        shim = launcher.read_text(encoding="utf-8-sig", errors="replace")[:256]
    except OSError:
        shim = ""
    if re.search(r"\bvolta run\b", shim, re.I):
        try:
            resolved = subprocess.run(["volta", "which", "codex"], capture_output=True, text=True,
                                      timeout=5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Volta Codex launcher discovery timed out before provider launch") from exc
        binary = Path(resolved.stdout.strip())
        if resolved.returncode == 0 and binary.is_file():
            if binary.suffix.lower() == ".exe":
                return [str(binary), *command[1:]]
            # Volta can report an extensionless POSIX shell shim or JS file.
            # Resolve its package entry through Node below, never CreateProcess it.
            launcher = binary.resolve()
    codex_js = launcher.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    sibling_node = launcher.parent / "node.exe"
    node = str(sibling_node) if sibling_node.is_file() else shutil.which("node")
    if not codex_js.is_file() or not node:
        raise ValueError(
            "DeepSeek's Windows Codex launcher is a batch shim, but its direct npm "
            "Node entrypoint could not be found. Reinstall Codex or configure providers.deepseek.executable "
            "as an argv list containing the Node executable and @openai/codex/bin/codex.js."
        )
    return [str(Path(node).resolve()), str(codex_js), *command[1:]]


def direct_windows_muse_command(command, platform_name=None):
    """Run the native Muse PowerShell launcher as argv, without cmd.exe."""
    if (platform_name or os.name) != "nt":
        return command
    launcher = Path(shutil.which(command[0]) or command[0])
    if launcher.suffix.lower() not in (".cmd", ".bat"):
        return command
    script = launcher.resolve().with_name(".muse-launcher.ps1")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not script.is_file() or not powershell:
        raise ValueError("Muse's Windows batch launcher requires its sibling .muse-launcher.ps1 and PowerShell")
    return [powershell, "-NoProfile", "-NonInteractive", "-File", str(script), *command[1:]]


def direct_windows_muse_config_command(command, platform_name=None):
    """Use the installed binary for JSON -c values; the batch/PS shim strips quotes."""
    if (platform_name or os.name) != "nt":
        return command
    launcher = Path(shutil.which(command[0]) or command[0])
    if launcher.suffix.lower() not in (".cmd", ".bat"):
        return command
    launcher = launcher.resolve()
    version_file = launcher.with_name(".muse-version")
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("Muse JSON config requires an installed native binary") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("Muse installed version is invalid")
    binary = launcher.with_name(f"muse-bin-{version}.exe")
    if not binary.is_file():
        raise ValueError("Muse JSON config requires the selected native binary")
    return [str(binary), *command[1:]]


def orchestration_writable_roots(provider, state_dir, workspace):
    """Permit only the shared state and explicitly named provider cache directories."""
    if state_dir is None:
        raise ValueError("OpenAI orchestration requires the effective shared state directory")
    extras = provider.get("orchestration_writable_roots", [])
    if not isinstance(extras, list) or not all(isinstance(value, str) and value.strip() for value in extras):
        raise ValueError("OpenAI orchestration_writable_roots must be a list of paths")
    workspace = Path(workspace).resolve()
    home = Path.home().resolve()
    roots = []
    for value in [str(state_dir), *extras]:
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            raise ValueError("OpenAI orchestration writable roots must be absolute paths")
        path = path.resolve()
        if (path == Path(path.anchor) or home.is_relative_to(path) or
                workspace.is_relative_to(path) and path != workspace):
            raise ValueError("OpenAI orchestration writable root is too broad")
        if path not in roots:
            roots.append(path)
    return [str(path) for path in roots]


def command_for(role, provider, prompt, timeout, workspace, effort="medium", provider_name=None, output_dir=None, activity=None, state_dir=None):
    executable = provider["executable"]
    command = [executable] if isinstance(executable, str) else list(executable)
    if not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("Provider executable must be a string or nonempty argv list")

    if provider_name is None:
        if role == "review":
            provider_name = "claude"
        elif provider.get("profile") == "deepseek" or provider.get("model") == "deepseek-flash":
            provider_name = "deepseek"
        else:
            provider_name = "gemini"

    if provider_name in ("deepseek", "openai"):
        command = direct_windows_codex_command(command)
    elif provider_name == "muse":
        command = (direct_windows_muse_config_command(command) if activity in READ_ACTIVITIES
                   else direct_windows_muse_command(command))

    model = provider["model"]
    if activity:
        reader = activity in READ_ACTIVITIES
        if provider_name == "openai":
            windows = []
            if os.name == "nt":
                sandbox = (provider.get("orchestration_windows_sandbox", provider.get("windows_sandbox"))
                           if activity == "orchestration" else provider.get("windows_sandbox"))
                if sandbox not in ("mxc", "elevated", "unelevated"):
                    raise ValueError("OpenAI on Windows requires an explicit supported windows_sandbox")
                windows = ["-c", f'windows.sandbox="{sandbox}"']
            orchestration = []
            if activity == "orchestration":
                roots = orchestration_writable_roots(provider, state_dir, workspace)
                orchestration = ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots),
                                 "-c", "sandbox_workspace_write.network_access=true"]
            return command + ["exec", "--ignore-user-config", "--strict-config", "--ephemeral",
                              "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                              "-c", f'model_reasoning_effort="{effort}"', "-c", "mcp_servers={}",
                              "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false",
                              "-c", "features.apps=false", "-c", "apps._default.enabled=false",
                              "-c", "agents.enabled=false", "-c", "features.plugins=false",
                              "-c", "features.hooks=false", "-c", "allow_login_shell=false",
                              *windows, "-c", 'approval_policy="never"', *orchestration,
                              "-m", model, "-s",
                              "read-only" if reader else "workspace-write",
                              "--json", "-C", str(workspace), prompt]
        if provider_name == "claude":
            base = ["-p", prompt, "--model", model, "--effort", effort,
                    "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                    "--strict-mcp-config", "--disable-slash-commands"]
            if reader:
                base += ["--safe-mode", "--tools", "Read,Glob,Grep"]
            else:
                base += ["--dangerously-skip-permissions"]
            return command + base
        if provider_name == "gemini":
            base = ["--print", prompt, "--model", model, "--effort", effort,
                    "--output-format", "stream-json", "--print-timeout", f"{timeout}s"]
            if reader:
                validate_gemini_reader(provider, workspace)
                helper = provider.get("reader_agent")
                if not isinstance(helper, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", helper):
                    raise ValueError("Gemini static activity requires a validated reader_agent")
                base += ["--agent", helper]
            else:
                base += ["--mode", "accept-edits", "--dangerously-skip-permissions", "--add-dir", str(workspace)]
            return command + base
        if provider_name == "muse":
            if output_dir is None:
                raise ValueError("Muse requires an output directory")
            steps = provider.get("max_model_steps", 100)
            if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
                raise ValueError("Muse max_model_steps must be positive")
            base = ["exec", "--json", "--prompt-file", str(output_dir / "prompt.txt"),
                    "--workspace", str(workspace), "--reasoning-effort", effort,
                    "--no-session-log", "--disable-reminders", "--max-model-steps", str(steps)]
            if reader:
                # Exact per-run selection is intended to exclude delegation/MCP;
                # the CLI rejects unknown names before execution. Live enforcement
                # still requires signed-in validation on the installed CLI.
                base += ["--disable-shell", "--disable-write", "--disable-web-tools",
                         "--no-foreign-personal-context", "-c", 'run.toolset=["read_file"]']
            else:
                base += ["--disable-approval", "--trust-workspace"]
            if model != "cli-default":
                base += ["--model", model]
            return command + base
        raise ValueError(f"Unsupported activity provider: {provider_name}")
    if role == "review":
        if provider_name != "claude":
            raise ValueError(f"Review role is Claude-only; got {provider_name}")
        expected = "claude-opus-5-5"
        if model != expected:
            raise ValueError(f"{role} requires pinned model {expected}; got {model}")
        if effort not in ("medium", "high"):
            raise ValueError(f"Claude review effort must be 'medium' or 'high'; got {effort}")
        return command + ["-p", "Read-only independent review. Do not change files.\n" + prompt,
                          "--model", model, "--effort", effort, "--output-format", "stream-json", "--verbose",
                          "--no-session-persistence",
                          "--dangerously-skip-permissions", "--safe-mode", "--tools", "Read,Glob,Grep", "--strict-mcp-config",
                          "--disable-slash-commands"]

    if role == "implement":
        if provider_name == "gemini":
            expected = "gemini-3.8-flash-medium"
            if model != expected:
                raise ValueError(f"{role} requires pinned model {expected}; got {model}")
            log_file = output_dir / "provider.log" if output_dir else Path(workspace) / ".llm-output" / "provider.log"
            return command + ["--print", prompt, "--model", model, "--mode", "accept-edits",
                              "--dangerously-skip-permissions", "--add-dir", str(workspace), "--output-format", "stream-json",
                              "--print-timeout", f"{timeout}s", "--log-file", str(log_file)]
        elif provider_name == "deepseek":
            expected = "deepseek-flash"
            if model != expected:
                raise ValueError(f"{role} with deepseek requires pinned model {expected}; got {model}")
            profile = provider.get("profile", "deepseek")
            last_msg = str((output_dir / "last_message.txt") if output_dir else (Path(workspace) / ".llm-output" / "last_message.txt"))
            if output_dir:
                schema_file = output_dir / "output_schema.json"
            else:
                schema_file = Path(workspace) / ".llm-output" / "output_schema.json"
            return command + ["exec", "-p", profile, "--model", model,
                              "--approve-for-me",
                              "-c", "shell_environment_policy.ignore_default_excludes=false",
                              "--json",
                              "--output-schema", str(schema_file),
                              "--output-last-message", last_msg, "--ephemeral", prompt]
        elif provider_name == "muse":
            if output_dir is None:
                raise ValueError("Muse requires an output directory for its prompt file")
            if model != "cli-default" and (not isinstance(model, str) or not model.strip()):
                raise ValueError("Muse model must be a nonempty CLI model ID or cli-default")
            effort = provider.get("effort", "medium")
            if effort not in MUSE_EFFORTS:
                raise ValueError("Unsupported Muse reasoning effort")
            steps = provider.get("max_model_steps", 100)
            if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
                raise ValueError("Muse max_model_steps must be a positive integer")
            args = ["exec", "--json", "--prompt-file", str(output_dir / "prompt.txt"),
                    "--workspace", str(workspace), "--reasoning-effort", effort,
                    "--disable-approval", "--trust-workspace", "--no-session-log",
                    "--disable-reminders", "--max-model-steps", str(steps)]
            if model != "cli-default":
                args += ["--model", model]
            return command + args
        raise ValueError(f"Unsupported implementation provider: {provider_name}")

    raise ValueError(f"Unsupported role: {role}")


def error_objects(text):
    try:
        values = [json.loads(text)]
    except (ValueError, RecursionError):
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except (ValueError, RecursionError):
                pass
    errors = []
    for value in values:
        if not isinstance(value, dict):
            continue
        error = value.get("error")
        if error:
            errors.append(error if isinstance(error, dict) else {"message": str(error)})
        if value.get("is_error") is True or value.get("type") == "error":
            errors.append(value)
        elif str(value.get("status", "")).upper() in ("ERROR", "FAILED", "FAILURE", "BLOCKED"):
            errors.append({**value, "message": value.get("message", value.get("response", "Provider reported failure"))})
    return errors


def structured_error_message(errors):
    """Return the first useful message from normalized provider errors."""
    for error in errors:
        for field in ("message", "result", "response"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def account_auth_error(result):
    """Classify account login failures without confusing throttles or model errors."""
    codes = {str(code).upper() for code in result.get("error_codes", [])}
    if codes & {"UNAUTHENTICATED", "AUTHENTICATION_ERROR", "INVALID_CREDENTIALS", "AUTH_REQUIRED", "OAUTH_TOKEN_EXPIRED"}:
        return True
    message = str(result.get("error", "")).lower()
    return bool(re.search(r"authentication required|not logged in|login required|invalid credentials|unauthorized|oauth token (?:has )?expired|please run /login|please (?:log in|login)", message))


def parse_deepseek_final_output(output_dir, stdout_text, stdout_truncated=False):
    """Parse and validate final JSON output object from DeepSeek.
    Success requires explicit success status plus a string response.
    Arbitrary messages, progress events, malformed output, or blocked/error status are not success."""
    raw_content = None
    if output_dir:
        last_msg_file = output_dir / "last_message.txt"
        if last_msg_file.exists():
            raw_content, truncated = read_text_tail(last_msg_file)
            if truncated:
                return None, False, False
            raw_content = raw_content.strip()

    obj = None
    if raw_content:
        try:
            parsed = json.loads(raw_content)
            if isinstance(parsed, dict):
                obj = parsed
        except (ValueError, RecursionError):
            return None, False, False
    elif stdout_text and not stdout_truncated:
        try:
            parsed = json.loads(stdout_text.strip())
            if isinstance(parsed, dict):
                obj = parsed
        except (ValueError, RecursionError):
            for line in reversed(stdout_text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and "status" in parsed and "response" in parsed and "type" not in parsed:
                        obj = parsed
                        break
                except (ValueError, RecursionError):
                    pass

    if not isinstance(obj, dict):
        return None, False, False

    if "type" in obj and obj.get("type") in ("turn.completed", "turn_completed", "response.done", "item.completed", "step"):
        return None, False, False

    status = obj.get("status")
    response = obj.get("response")
    if not isinstance(status, str) or isinstance(status, bool) or not isinstance(response, str) or isinstance(response, bool):
        return None, False, False

    status_upper = status.strip().upper()
    if status_upper not in ("SUCCESS", "FAILED", "BLOCKED", "ERROR"):
        return None, False, False
    is_terminal = True
    is_success = (status_upper == "SUCCESS")
    return obj, is_terminal, is_success


def parse_codex_events(stdout_text):
    """Parse Codex JSONL events to extract terminal outcome and structured errors."""
    terminal_event = None
    terminal_turn = None
    has_terminal_402 = False
    intermediate_errors = []

    for line in stdout_text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue

        evt_type = obj.get("type")
        if evt_type == "turn.completed":
            terminal_turn = "completed"
            terminal_event = obj
        elif evt_type == "turn.failed":
            terminal_turn = "failed"
            terminal_event = obj
            err = obj.get("error")
            if isinstance(err, dict):
                code = err.get("code") or err.get("status")
                if code == 402 or str(code).lower() in ("402", "insufficient_balance"):
                    has_terminal_402 = True
        elif evt_type == "error" or "error" in obj:
            err = obj.get("error") or obj
            intermediate_errors.append(err)

    return terminal_turn, terminal_event, has_terminal_402, intermediate_errors


def parse_openai_terminal(stdout_text, truncated=False):
    """Accept only a root Codex JSONL terminal after a started thread."""
    if truncated or not stdout_text.endswith("\n"):
        return None, False, []
    started = False
    thread_id = None
    terminal = None
    errors = []
    answer = False
    for raw in stdout_text.splitlines():
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            return None, False, []
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None, False, []
        kind = event["type"]
        if terminal is not None:
            return None, False, []
        if not started and kind != "thread.started":
            return None, False, []
        if kind == "thread.started" and not started and isinstance(event.get("thread_id"), str) and event["thread_id"].strip():
            started = True
            thread_id = event["thread_id"]
        elif kind == "thread.started":
            return None, False, []
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str) and item["text"].strip():
                answer = True
        elif kind in ("turn.completed", "turn.failed"):
            if not started or terminal is not None:
                return None, False, []
            for key in ("thread_id", "parent_thread_id"):
                if key in event and event[key] != thread_id:
                    return None, False, []
            terminal = event
        elif kind == "error":
            pass  # Intermediate diagnostics cannot establish account exhaustion.
    if terminal and terminal["type"] == "turn.failed":
        error = terminal.get("error")
        if isinstance(error, dict):
            errors.append(error)
        elif isinstance(error, str):
            errors.append({"message": error})
    if terminal and terminal["type"] == "turn.completed" and not answer:
        return None, False, []
    return terminal, terminal is not None, errors


def parse_gemini_final_output(stdout_text, stdout_truncated=False):
    """Return Gemini's terminal payload from stream-json or legacy JSON output.

    Stream progress is deliberately not treated as a terminal result.  A missing
    or malformed final ``event=result`` therefore retains pending ownership.
    """
    terminal = None
    saw_result_event = False
    stream_errors = []

    def inspect(value, allow_legacy=False):
        nonlocal terminal, saw_result_event
        if not isinstance(value, dict):
            return
        event = value.get("event")
        if event == "result":
            saw_result_event = True
            candidate = value.get("result")
            if isinstance(candidate, dict):
                terminal = candidate
        elif event == "error":
            error = value.get("error")
            if isinstance(error, dict):
                stream_errors.append(error)
            elif isinstance(error, str) and error.strip():
                stream_errors.append({"message": error.strip()})
            else:
                stream_errors.append(value)
        elif event is None and allow_legacy:
            status_envelope = (
                isinstance(value.get("status"), str) and
                str(value.get("status")).upper() in ("SUCCESS", "ERROR", "FAILED", "FAILURE", "BLOCKED") and
                isinstance(value.get("response"), str)
            )
            error_envelope = (
                value.get("is_error") is True or
                isinstance(value.get("error"), (dict, str))
            )
            if status_envelope or error_envelope:
                # Retain compatibility with older/fake CLIs that emit one
                # explicit terminal envelope even when stream-json was
                # requested. Arbitrary one-line JSON diagnostics remain
                # non-terminal, and bare errors within JSONL remain progress.
                terminal = value

    stripped = stdout_text.strip()
    if not stripped:
        return None, False, []
    if not stdout_truncated:
        try:
            inspect(json.loads(stripped), allow_legacy=True)
            return terminal, saw_result_event, stream_errors
        except (ValueError, RecursionError):
            pass
    if stdout_truncated or terminal is None:
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                inspect(json.loads(line), allow_legacy=False)
            except (ValueError, RecursionError):
                continue
    return terminal, saw_result_event, stream_errors


def parse_claude_final_output(stdout_text):
    """Return Claude's terminal payload from stream-json or legacy JSON output.

    Stream progress is deliberately not treated as a terminal result. A missing
    or malformed final result event therefore retains uncertain status.
    """
    terminal = None
    saw_result_event = False
    stream_errors = []

    def inspect(value, allow_legacy=False):
        nonlocal terminal, saw_result_event
        if not isinstance(value, dict):
            return
        msg_type = value.get("type")
        if msg_type == "result":
            saw_result_event = True
            terminal = value
        elif msg_type == "error":
            err = value.get("error")
            if isinstance(err, dict):
                stream_errors.append(err)
            elif isinstance(err, str) and err.strip():
                stream_errors.append({"message": err.strip()})
            else:
                stream_errors.append(value)
        # Claude's legacy single-object format still carries type=result and
        # is handled above. Bare JSON diagnostics are never terminal.

    stripped = stdout_text.strip()
    if not stripped:
        return None, False, []
    try:
        inspect(json.loads(stripped), allow_legacy=True)
    except (ValueError, RecursionError):
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                inspect(json.loads(line), allow_legacy=False)
            except (ValueError, RecursionError):
                continue
    return terminal, saw_result_event, stream_errors


def successful_response(text, role, provider="gemini", output_dir=None):
    if provider == "deepseek":
        _, _, is_success = parse_deepseek_final_output(output_dir, text)
        return is_success

    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("denied_actions") or value.get("permission_denials"):
        return False
    if role == "implement":
        return value.get("status") == "SUCCESS" and isinstance(value.get("response"), str)
    return value.get("type") == "result" and value.get("is_error") is False and value.get("subtype") == "success"


def permission_denials(text):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return []
    if not isinstance(value, dict):
        return []
    return value.get("denied_actions") or value.get("permission_denials") or []


def complete_terminal_failure(text):
    """Recognize one complete provider failure envelope, never JSONL diagnostics."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    status = str(value.get("status", "")).upper()
    return status in ("ERROR", "FAILED", "FAILURE", "BLOCKED") or value.get("is_error") is True or value.get("type") == "error" or isinstance(value.get("error"), (dict, str))


def plain_terminal_error(stderr, exit_code):
    """Recognize only exact, standalone CLI diagnostics on failed invocations."""
    if exit_code == 0:
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if len(lines) != 1:
        return None  # Mixed output cannot establish a terminal launch failure.
    line = lines[0]
    if re.fullmatch(r"missing meta credentials: run `muse login` or set META_API_KEY, or save credentials at .+", line):
        return {"code": "CLI_SETUP_ERROR", "message": "Muse login required", "source": "terminal_stderr"}
    if terminal_quota_message(line):
        return {"code": "QUOTA_EXHAUSTED", "message": line, "source": "terminal_stderr"}
    if re.fullmatch(r"Error: (?:not logged in|authentication required|invalid model|model not found)[.!]?", line, re.IGNORECASE):
        return {"code": "CLI_SETUP_ERROR", "message": line, "source": "terminal_stderr"}
    return None


def terminal_quota_message(message, provider=None):
    """Match provider diagnostics, never a quoted phrase inside task/error prose."""
    prefix = r"(?:error:\s*)?"
    patterns = [r"(?:daily|plan|monthly|weekly|session) (?:quota (?:exhausted|exceeded)|(?:usage )?limit (?:reached|exceeded|exhausted))(?:[.!]|[.!]?\s+resets?\s+[^\n]+)?"]
    if provider in (None, "gemini"):
        patterns.append(r"individual quota reached(?:\.[ ]+Please upgrade your subscription to increase your limits\.)?(?:\s+Resets in [^\n]+)?[.!]?")
    if provider in (None, "claude"):
        patterns.append(r"you['’]ve hit your(?: (?:session|weekly|plan|monthly|opus))? limit(?:[.!]|\s*[·-]\s*resets?\s+[^\n]+)?")
        patterns.append(r"opus (?:usage )?limit (?:reached|exceeded|exhausted)[.!]?")
    if provider in (None, "openai"):
        suffix = r"(?:[.!](?:[ ]+[^\n]{0,500})?)?"
        patterns.append(r"you['’]ve hit your (?:usage|weekly|plan) limit" + suffix)
        patterns.append(r"you['’]ve reached your (?:weekly |plan )?usage limit" + suffix)
        patterns.append(r"your chatgpt plan has reached its usage limit[.!]?")
    return any(re.fullmatch(prefix + pattern, message.strip(), re.I) for pattern in patterns)


def _duration_seconds(text):
    match = re.search(r"(?:resets?\s+in|retry(?:s|ies)?\s+in)\s+((?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s\s*)?)(?=$|[.,;!])", text, re.I)
    if not match or not re.search(r"\d", match.group(1)):
        return None
    try:
        parts = {unit.lower(): int(value) for value, unit in re.findall(r"(\d+)\s*([hms])", match.group(1), re.I)}
        seconds = parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)
        return seconds if 0 < seconds < 253402300800 else None
    except ValueError:
        return None


def _reset_timestamp(value, observed):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stamp = float(value)
        elif isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not parsed.tzinfo:
                return None
            stamp = parsed.timestamp()
        else:
            return None
        if math.isfinite(stamp) and stamp > observed:
            dt.datetime.fromtimestamp(stamp, UTC)  # Must fit the persisted ISO timestamp.
            return stamp
    except (OverflowError, OSError, ValueError):
        pass
    return None


def quota_cooldown(config):
    if not isinstance(config, dict):
        raise ValueError("Routing configuration must be an object")
    value = config.get("quota_probe_seconds", 3600)
    observed = time.time()
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and _reset_timestamp(observed + value, observed) is not None)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("quota_probe_seconds must be a finite positive number")
    return float(value)


def _valid_local_timestamp(candidate, zone):
    """Reject DST gaps and folds: a provider wall time then has no unique instant."""
    first = candidate.replace(fold=0)
    second = candidate.replace(fold=1)
    if first.utcoffset() != second.utcoffset():
        return None
    stamp = first.timestamp()
    if dt.datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != first.replace(tzinfo=None):
        return None
    return stamp


def quota_error(errors, provider="gemini"):
    for error in errors:
        code = str(error.get("code", error.get("status", ""))).upper()
        raw_message = str(error.get("message", error.get("result", "")))
        message = raw_message.lower()
        # RESOURCE_EXHAUSTED/429 alone also means short-term throttling: do not cache.
        if any(phrase in message for phrase in ("per minute", "per second", "requests/min", "requests/sec", "rate limit", "rate quota")):
            continue
        if code in ("QUOTA_EXHAUSTED", "INSUFFICIENT_QUOTA", "USAGE_LIMIT_REACHED", "WEEKLY_LIMIT_REACHED") or terminal_quota_message(raw_message, provider):
            found = dict(error)
            found["message"] = raw_message
            return found
    return None


def muse_quota_error(terminal):
    """Accept only native root failure with a tagged subscription limit reason."""
    if not isinstance(terminal, dict) or terminal.get("terminal") != "failed":
        return None
    reason = terminal.get("reason")
    if not isinstance(reason, str):
        return None
    match = re.fullmatch(r"API error (\d{3}): (.+) \(quota_exhausted\)", reason.strip(), re.I)
    if not match or match.group(1) != "403" or not terminal_quota_message(match.group(2), "muse"):
        return None
    return {"code": "QUOTA_EXHAUSTED", "message": match.group(2),
            "source": "muse_root_terminal", "http_status": int(match.group(1))}


def retry_at(error, cooldown, observed=None):
    observed = time.time() if observed is None else observed
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
               for value in (observed, cooldown)):
        raise ValueError("observed and cooldown must be finite positive numbers")
    if _reset_timestamp(observed + cooldown, observed) is None:
        raise ValueError("Probe cooldown must produce a representable future timestamp")
    value = error.get("reset_at", error.get("resets_at"))
    stamp = _reset_timestamp(value, observed)
    if stamp:
        return stamp, "provider_reset"
    duration = _duration_seconds(str(error.get("message", error.get("result", ""))))
    if duration:
        stamp = _reset_timestamp(observed + duration, observed)
        if stamp is not None:
            return stamp, "provider_reset"
    human_text = str(error.get("message", error.get("result", "")))
    human = re.search(r"resets?\s+(?:(Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\s+)?(?:(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(\d{4}))?[,]?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", human_text, re.I)
    if human:
        try:
            from zoneinfo import ZoneInfo
            weekday, month, day, year, hour_text, minute_text, ampm, zone = human.groups()
            if not 1 <= int(hour_text) <= 12 or not 0 <= int(minute_text or 0) <= 59:
                raise ValueError("Invalid reset clock time")
            hour = int(hour_text) % 12 + (12 if ampm.lower() == "pm" else 0)
            minute = int(minute_text or 0)
            local = dt.datetime.fromtimestamp(observed, UTC).astimezone(ZoneInfo(zone))
            year = int(year) if year else local.year
            month_number = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(month[:3].lower()) + 1 if month else local.month
            zone_info = ZoneInfo(zone)
            candidate = (dt.datetime(year, month_number, int(day), hour, minute, tzinfo=zone_info)
                         if month else local.replace(hour=hour, minute=minute, second=0, microsecond=0))
            if weekday:
                target = list(("mon", "tue", "wed", "thu", "fri", "sat", "sun")).index(weekday[:3].lower())
                if month and candidate.weekday() != target:
                    return observed + cooldown, "probe_cooldown"
                if not month:
                    candidate += dt.timedelta(days=(target - candidate.weekday()) % 7)
            stamp = _valid_local_timestamp(candidate, zone_info)
            if stamp is not None and stamp > observed:
                return stamp, "provider_reset"
        except (ValueError, LookupError, TypeError, OverflowError, OSError):
            pass
    return observed + cooldown, "probe_cooldown"


def quota_path(state_dir, provider):
    return state_dir / f"{provider}-quota.json"


def balance_path(state_dir):
    return state_dir / "deepseek-balance.json"


def luna_fallback():
    return {"model": "gpt-6-luna", "effort": "medium"}


def quota_fallback(provider, config=None):
    if provider in ("gemini", "muse"):
        return luna_fallback()
    if provider == "deepseek":
        return luna_fallback()
    return {"model": "gpt-6-astra", "effort": "low"}


def _parse_decimal_string(val, field_name):
    if not isinstance(val, str) or isinstance(val, bool):
        raise ValueError(f"Balance field '{field_name}' must be a string; got {type(val).__name__}")
    val_clean = val.strip()
    if not val_clean:
        raise ValueError(f"Balance field '{field_name}' must not be empty")
    try:
        d = Decimal(val_clean)
    except InvalidOperation as exc:
        raise ValueError(f"Balance field '{field_name}' is not a valid decimal: {val!r}") from exc
    if not d.is_finite():
        raise ValueError(f"Balance field '{field_name}' must be finite: {val!r}")
    if d < 0:
        raise ValueError(f"Balance field '{field_name}' must not be negative: {val!r}")
    return d, val_clean


def validate_balance_info_entry(item):
    if not isinstance(item, dict):
        raise ValueError("Balance info entry must be a dictionary")
    currency = item.get("currency")
    if not isinstance(currency, str) or isinstance(currency, bool) or not currency.strip():
        raise ValueError("currency must be a non-empty string")

    entry = {"currency": currency.strip()}
    if "total_balance" not in item:
        raise ValueError("Balance info entry missing 'total_balance'")
    _, entry["total_balance"] = _parse_decimal_string(item["total_balance"], "total_balance")

    for field in ("granted_balance", "topped_up_balance"):
        if field in item:
            _, entry[field] = _parse_decimal_string(item[field], field)
        else:
            entry[field] = "0.00"
    return entry


def _sanitize_secret(text, secret):
    if not text or not secret:
        return text
    res = str(text).replace(secret, "[REDACTED]")
    raw = secret.strip()
    if raw:
        res = res.replace(raw, "[REDACTED]")
    return re.sub(r"(Bearer\s+)[^\s,'\"]+", r"\1[REDACTED]", res, flags=re.IGNORECASE)


def _sanitize_all_secrets(text, secrets=None):
    if not text:
        return text
    res = str(text)
    if secrets:
        for s in secrets:
            if s and isinstance(s, str):
                raw = s.strip()
                if len(raw) >= 4:
                    res = res.replace(s, "[REDACTED]")
                    res = res.replace(raw, "[REDACTED]")
    res = re.sub(r"(Bearer\s+)[^\s,'\"]+", r"\1[REDACTED]", res, flags=re.IGNORECASE)
    res = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", res)
    return res


def deepseek_secrets(config):
    """Return current/retired DeepSeek credentials for filtering, never disclose them in results."""
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    deepseek = providers.get("deepseek", {}) if isinstance(providers, dict) else {}
    configured = deepseek.get("api_key_env", "DEEPSEEK_API_KEY") if isinstance(deepseek, dict) else "DEEPSEEK_API_KEY"
    names = {"DEEPSEEK_API_KEY"}
    retired = config.get("retired_secret_env_vars", []) if isinstance(config, dict) else []
    if isinstance(retired, list):
        names.update(name for name in retired if isinstance(name, str))
    if isinstance(configured, str):
        names.add(configured)
    return [(name, os.environ[name]) for name in names if os.environ.get(name) and os.environ[name].strip()]


def muse_secrets(config):
    """Known Muse API-key overrides are excluded from every external child."""
    names = {"META_API_KEY", "MUSE_API_KEY", "META_MODEL_API_KEY"}
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    muse = providers.get("muse", {}) if isinstance(providers, dict) else {}
    if isinstance(muse, dict) and isinstance(muse.get("api_key_env"), str):
        names.add(muse["api_key_env"])
    return [(name, os.environ[name]) for name in names if os.environ.get(name) and os.environ[name].strip()]


def framework_secrets(config):
    """Credentials used by framework helpers, never by execution-provider children."""
    secrets = dict(deepseek_secrets(config) + muse_secrets(config))
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        secrets["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
    return list(secrets.items())


def child_environment(config, provider_name, checked_deepseek_key=None):
    """Keep helper credentials out of external execution-provider environments."""
    env = dict(os.environ)
    env.pop("FRAMEWORK_PARENT_RUN_ID", None)
    env.pop("TYPESAFE_API_KEY", None)
    for name in ("META_API_KEY", "MUSE_API_KEY", "META_MODEL_API_KEY"):
        env.pop(name, None)
    if provider_name == "openai":
        # The desktop host exports IPC, permission and task-session controls as
        # CODEX_* variables. A nested Codex CLI must keep only CODEX_HOME for
        # subscription auth; inheriting host policy can inject MCP tools and
        # deny even read-only local commands despite --ignore-user-config.
        for name in tuple(env):
            if name.upper().startswith("CODEX_") and name.upper() != "CODEX_HOME":
                env.pop(name, None)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_BASE_URL"):
            env.pop(name, None)
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    muse = providers.get("muse", {}) if isinstance(providers, dict) else {}
    if isinstance(muse, dict) and isinstance(muse.get("api_key_env"), str):
        env.pop(muse["api_key_env"], None)
    for name, _ in framework_secrets(config):
        env.pop(name, None)
    if provider_name == "deepseek" and checked_deepseek_key:
        # The default Codex profile consumes this canonical name.  Do not leave a
        # second configured name that could make it select a different account.
        env["DEEPSEEK_API_KEY"] = checked_deepseek_key
    return env


def sanitize_run_artifacts(output, secrets):
    if not Path(output).exists():
        return []
    errors = []
    exact = set()
    for secret in secrets or ():
        if secret and isinstance(secret, str):
            stripped = secret.strip()
            if len(stripped) >= 4:
                exact.add(secret.encode("utf-8"))
                exact.add(stripped.encode("utf-8"))
    exact_pattern = None
    if exact:
        exact_pattern = re.compile(
            b"|".join(re.escape(value) for value in sorted(exact, key=len, reverse=True)))
    # Match Python text-regex whitespace without decoding the complete file.
    # The multi-byte alternatives cover Unicode whitespace encoded as UTF-8.
    whitespace = (
        b"(?:[\\x09-\\x0d\\x1c-\\x20]|\\xc2\\x85|\\xc2\\xa0|\\xe1\\x9a\\x80|"
        b"\\xe2\\x80[\\x80-\\x8a]|\\xe2\\x80[\\xa8-\\xa9]|\\xe2\\x80\\xaf|"
        b"\\xe2\\x81\\x9f|\\xe3\\x80\\x80)"
    )
    generic_pattern = re.compile(
        b"(?P<bearer>(?P<bearer_prefix>(?i:Bearer)" + whitespace +
        b"+)(?:(?!" + whitespace + b")[^,'\"])+)|"
        b"(?P<sk>\\bsk-[A-Za-z0-9_-]{16,}\\b)")

    def copy_range(source, destination, start, end):
        while start < end:
            stop = min(start + ARTIFACT_COPY_CHUNK_BYTES, end)
            destination.write(source[start:stop])
            start = stop

    def rewrite(source_path, destination_path, pattern, preserve_bearer_prefix=False):
        with source_path.open("rb") as source_handle:
            with mmap.mmap(source_handle.fileno(), 0, access=mmap.ACCESS_READ) as source:
                match = pattern.search(source)
                if match is None:
                    return False
                with destination_path.open("wb") as destination:
                    position = 0
                    while match is not None:
                        copy_range(source, destination, position, match.start())
                        if preserve_bearer_prefix and match.start("bearer") >= 0:
                            copy_range(source, destination, match.start("bearer_prefix"),
                                       match.end("bearer_prefix"))
                        destination.write(b"[REDACTED]")
                        position = match.end()
                        match = pattern.search(source, position)
                    copy_range(source, destination, position, len(source))
        return True

    def copy_in_place(source_path, target_path):
        """Bounded Windows fallback when an inherited handle blocks replacement."""
        with source_path.open("rb") as source, target_path.open("r+b") as target:
            opened = os.fstat(target.fileno())
            current = os.stat(target_path, follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode) or
                    opened.st_nlink != 1 or not os.path.samestat(opened, current)):
                raise PermissionError("refusing in-place redaction of a linked or replaced artifact")
            target.seek(0)
            while True:
                chunk = source.read(ARTIFACT_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                target.write(chunk)
            target.truncate()

    for name in ("prompt.txt", "stdout.log", "stderr.log", "last_message.txt",
                 "provider.log", "progress.json", "heartbeat.json",
                 "status.txt", "diff.txt", "head.txt"):
        path = Path(output) / name
        exact_temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
        generic_temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                continue
            source_path = path
            changed = False
            if exact_pattern is not None and rewrite(path, exact_temporary, exact_pattern):
                source_path = exact_temporary
                changed = True
            if rewrite(source_path, generic_temporary, generic_pattern,
                       preserve_bearer_prefix=True):
                source_path = generic_temporary
                changed = True
            if changed:
                # Atomic replacement avoids following a hard link while writing.
                try:
                    os.replace(source_path, path)
                except PermissionError:
                    # Windows can deny replacement while an exited provider's
                    # descendant still holds the inherited log handle. Preserve
                    # the old in-place behavior without following hard links.
                    copy_in_place(source_path, path)
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        finally:
            for temporary in (exact_temporary, generic_temporary):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return errors


class BalanceError(str):
    """A balance query error string annotated with category kind ('depleted' vs 'unverified') and structured code."""
    def __new__(cls, message, kind="unverified", code="UNKNOWN"):
        obj = str.__new__(cls, message)
        obj.kind = kind
        obj.code = code
        return obj


def classify_balance_error(err):
    if err is None:
        return None, None
    kind = getattr(err, "kind", None)
    code = getattr(err, "code", None)
    if kind:
        return kind, code
    err_str = str(err)
    err_lower = err_str.lower()
    if any(k in err_lower for k in ("depleted", "is_available=false", "is_available is false", "402", "insufficient balance")):
        return "depleted", "DEPLETED"
    return "unverified", "UNVERIFIED"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"HTTP redirect ({code}) is not permitted for balance queries", headers, fp)


def query_deepseek_balance(config, timeout=15):
    """Query official DeepSeek GET /user/balance endpoint using standard library. Never prints or persists key."""
    deepseek_config = config.get("providers", {}).get("deepseek", {}) if isinstance(config.get("providers"), dict) else {}
    env_name = deepseek_config.get("api_key_env", "DEEPSEEK_API_KEY")
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        return None, BalanceError(f"Invalid api_key_env configuration: {env_name!r}", kind="unverified", code="INVALID_CONFIG")

    raw_api_key = os.environ.get(env_name)
    if raw_api_key is None or not raw_api_key.strip():
        return None, BalanceError(f"Environment variable '{env_name}' is not set or empty", kind="unverified", code="MISSING_KEY")
    api_key = raw_api_key.strip()
    if any(c in api_key for c in "\r\n\t") or not api_key.isascii() or not api_key.isprintable():
        return None, BalanceError(f"Environment variable '{env_name}' contains invalid key characters", kind="unverified", code="INVALID_KEY")

    base_url = deepseek_config.get("base_url", "https://api.deepseek.com")
    parsed_url = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
    if (parsed_url is None or parsed_url.scheme != "https" or parsed_url.netloc != "api.deepseek.com"
            or parsed_url.path not in ("", "/") or parsed_url.query or parsed_url.fragment or parsed_url.username or parsed_url.password):
        return None, BalanceError("DeepSeek balance endpoint must be https://api.deepseek.com", kind="unverified", code="INVALID_URL")
    url = "https://api.deepseek.com/user/balance"

    def sanitize(msg):
        return _sanitize_secret(msg, raw_api_key)

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "codex-agent-framework",
            },
        )
        opener = urllib.request.build_opener(NoRedirectHandler)
        resp_ctx = opener.open(req, timeout=timeout)
        with resp_ctx as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 402:
                return None, BalanceError("HTTP 402: Insufficient balance", kind="depleted", code="HTTP_402")
            if exc.code == 401:
                return None, BalanceError("HTTP 401: Unauthorized / invalid DeepSeek API key", kind="unverified", code="HTTP_401")
            if exc.code == 429:
                return None, BalanceError(sanitize(f"DeepSeek balance query rate limited (HTTP 429): {exc.reason}"), kind="unverified", code="RATE_LIMITED")
            if 500 <= exc.code < 600:
                return None, BalanceError(sanitize(f"DeepSeek balance query server error (HTTP {exc.code}): {exc.reason}"), kind="unverified", code="SERVER_ERROR")
            return None, BalanceError(sanitize(f"DeepSeek balance query failed with HTTP {exc.code}: {exc.reason}"), kind="unverified", code="HTTP_ERROR")
        finally:
            if hasattr(exc, "close"):
                exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, BalanceError(sanitize(f"DeepSeek balance query network error: {exc}"), kind="unverified", code="NETWORK_ERROR")
    except (ValueError, UnicodeDecodeError) as exc:
        return None, BalanceError(sanitize(f"DeepSeek balance response malformed: {exc}"), kind="unverified", code="MALFORMED_RESPONSE")
    except Exception as exc:
        return None, BalanceError(sanitize(f"DeepSeek balance query unexpected error: {exc}"), kind="unverified", code="UNEXPECTED_ERROR")

    if not isinstance(data, dict):
        return None, BalanceError("DeepSeek balance response is not a JSON object", kind="unverified", code="MALFORMED_RESPONSE")
    if "is_available" not in data or not isinstance(data["is_available"], bool):
        return None, BalanceError("DeepSeek balance response missing boolean 'is_available'", kind="unverified", code="MALFORMED_RESPONSE")
    if "balance_infos" not in data or not isinstance(data["balance_infos"], list):
        return None, BalanceError("DeepSeek balance response missing list 'balance_infos'", kind="unverified", code="MALFORMED_RESPONSE")

    validated_infos = []
    has_positive = False
    for item in data["balance_infos"]:
        if not isinstance(item, dict):
            return None, BalanceError("DeepSeek balance item is not a JSON object", kind="unverified", code="MALFORMED_RESPONSE")
        for field in ("currency", "total_balance", "granted_balance", "topped_up_balance"):
            if field not in item:
                return None, BalanceError(f"DeepSeek balance item missing required field '{field}'", kind="unverified", code="MALFORMED_RESPONSE")
        try:
            entry = validate_balance_info_entry(item)
            validated_infos.append(entry)
            if Decimal(entry["total_balance"]) > 0:
                has_positive = True
        except ValueError as exc:
            return None, BalanceError(f"DeepSeek balance item invalid: {exc}", kind="unverified", code="MALFORMED_RESPONSE")

    data["balance_infos"] = validated_infos

    if data["is_available"] is False:
        return data, BalanceError("DeepSeek reported account is not available (is_available=false)", kind="depleted", code="ACCOUNT_UNAVAILABLE")

    if not has_positive:
        return data, BalanceError("DeepSeek balance is depleted (no positive total_balance)", kind="depleted", code="BALANCE_DEPLETED")

    return data, None


def balance_state(state_dir):
    path = balance_path(state_dir)
    try:
        state = read_json(path)
        if not isinstance(state, dict):
            raise ValueError("Balance snapshot must be a JSON object")
        if state.get("is_available") is not None and not isinstance(state.get("is_available"), bool):
            raise ValueError("Balance snapshot 'is_available' must be boolean")
        if "balance_infos" not in state or not isinstance(state["balance_infos"], list):
            raise ValueError("Balance snapshot missing 'balance_infos' list")
        for item in state["balance_infos"]:
            validate_balance_info_entry(item)
        return state, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError) as exc:
        return None, str(exc)


def record_balance_snapshot(state_dir, data, error=None, config=None):
    observed = time.time()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_timeout = float(config.get("lock_timeout_seconds", 30)) if isinstance(config, dict) else 30.0
    with provider_lock(state_dir, lock_timeout, "balance.lock"):
        existing, _ = balance_state(state_dir)
        if existing and isinstance(existing.get("observed_at"), str):
            try:
                existing_stamp = dt.datetime.fromisoformat(existing["observed_at"].replace("Z", "+00:00")).timestamp()
                if existing_stamp > observed:
                    return existing
            except (ValueError, OSError):
                pass

        model = "deepseek-flash"
        if config and isinstance(config.get("providers"), dict) and isinstance(config["providers"].get("deepseek"), dict):
            model = config["providers"]["deepseek"].get("model", model)

        observed_iso = dt.datetime.fromtimestamp(observed, UTC).isoformat().replace("+00:00", "Z")
        kind, code = classify_balance_error(error)

        if data is None and existing:
            # Preserve last successful monetary snapshot on unverified error
            snapshot = dict(existing)
            snapshot["last_check_error"] = str(error)
            snapshot["last_check_error_at"] = observed_iso
            snapshot["last_check_error_kind"] = kind
            write_json(balance_path(state_dir), snapshot)
            return snapshot

        raw_balance_infos = data.get("balance_infos", []) if isinstance(data, dict) else []
        validated_infos = []
        has_positive = False
        for item in raw_balance_infos:
            try:
                entry = validate_balance_info_entry(item)
                validated_infos.append(entry)
                if Decimal(entry["total_balance"]) > 0:
                    has_positive = True
            except (ValueError, TypeError):
                pass

        total_balance = validated_infos[0]["total_balance"] if len(validated_infos) == 1 else None
        raw_available = data.get("is_available", False) if isinstance(data, dict) else False
        is_available = bool(raw_available and (error is None) and has_positive)
        snapshot = {
            "version": 1,
            "provider": "deepseek",
            "model": model,
            "observed_at": observed_iso if data is not None else None,
            "is_available": is_available if data is not None else None,
            "total_balance": total_balance,
            "balance_infos": validated_infos,
            "error": str(error) if error else None,
            "error_kind": kind,
            "last_check_error": str(error) if (error and data is None) else None,
            "last_check_error_at": observed_iso if (error and data is None) else None,
            "last_check_error_kind": kind if (error and data is None) else None,
        }
        write_json(balance_path(state_dir), snapshot)
        return snapshot


def refresh_balance_safe(state_dir, config):
    try:
        data, err = query_deepseek_balance(config, timeout=10)
        if data is not None:
            record_balance_snapshot(state_dir, data, err, config)
    except Exception:
        pass


def deepseek_insufficient_balance_error(errors, stderr_text=""):
    """Detect an actual DeepSeek HTTP 402/insufficient-balance run failure.
    Rate limits and unrelated errors are not balance exhaustion."""
    for error in errors:
        code = str(error.get("code", error.get("status", ""))).lower()
        msg = str(error.get("message", error.get("result", ""))).lower()
        if any(rl in msg for rl in ("rate limit", "rate_limit", "too many requests", "429", "requests per minute", "requests/min")):
            continue
        if code in ("402", "insufficient_balance") or "insufficient_balance" in code:
            return error
        if "insufficient balance" in msg or "insufficient_balance" in msg or "402 payment required" in msg or "402: payment required" in msg:
            return error
    for line in stderr_text.splitlines():
        line_clean = line.strip()
        lower = line_clean.lower()
        if any(rl in lower for rl in ("rate limit", "rate_limit", "too many requests", "429", "requests per minute", "requests/min")):
            continue
        if re.search(r"\b402\b", lower) and any(kw in lower for kw in ("insufficient", "payment required", "balance")):
            return {"code": "INSUFFICIENT_BALANCE", "message": line_clean, "source": "terminal_stderr"}
        if "insufficient balance" in lower or "insufficient_balance" in lower:
            return {"code": "INSUFFICIENT_BALANCE", "message": line_clean, "source": "terminal_stderr"}
    return None


def quota_state(state_dir, provider):
    path = quota_path(state_dir, provider)
    try:
        state = read_json(path)
        retry = state.get("retry_at") if isinstance(state, dict) else None
        if not isinstance(state, dict) or isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry <= 0:
            raise ValueError("quota state must contain numeric retry_at")
        dt.datetime.fromtimestamp(retry, UTC)
        return state, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        return None, str(exc)


def quota_record(provider, error, state_dir, logs, config, model=None, muse_profile=None, effort=None):
    observed = time.time()
    retry, reason = retry_at(error, quota_cooldown(config), observed)
    reset = retry if reason == "provider_reset" else None
    if model is None:
        model = config["providers"][provider].get("model")
    state = {"version": 1, "provider": provider, "model": model,
             "observed_at": dt.datetime.fromtimestamp(observed, UTC).isoformat().replace("+00:00", "Z"),
             "retry_at": retry, "retry_at_iso": dt.datetime.fromtimestamp(retry, UTC).isoformat().replace("+00:00", "Z"),
             "reset_at": dt.datetime.fromtimestamp(reset, UTC).isoformat().replace("+00:00", "Z") if reset else None,
             "reason": reason, "provenance": "operator_report" if logs == "operator" else ("provider_reset" if reason == "provider_reset" else "probe_cooldown"),
             "source_logs": str(logs), "confirmed_error": error, "error": error}
    if provider == "muse" and muse_profile is not None:
        state["muse_profile"] = muse_profile
    if provider == "muse" and effort is not None:
        state["effort"] = effort
    existing, state_error = quota_state(state_dir, provider)
    if state_error:
        raise ValueError("Refusing to overwrite malformed quota state: " + state_error)
    if existing and existing.get("retry_at", 0) > observed and existing.get("retry_at", 0) > retry:
        return existing
    write_json(quota_path(state_dir, provider), state)
    return state


def stop_process(process):
    evidence = {"tree_termination_attempted": True, "tree_termination_outcome": "unknown",
                "tree_cessation_verified": False}
    if os.name == "nt":
        try:
            run = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 capture_output=True, timeout=30, check=False)
            evidence["tree_termination_outcome"] = "taskkill_exit_" + str(run.returncode)
        except Exception as exc:
            evidence["tree_termination_outcome"] = "taskkill_error: " + str(exc)
        if process.poll() is None:
            try:
                process.kill()
                evidence["direct_kill_attempted"] = True
            except Exception as exc:
                evidence["direct_kill_error"] = str(exc)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            evidence["tree_termination_outcome"] = "signal_sent"
        except Exception as exc:
            evidence["tree_termination_outcome"] = "signal_error: " + str(exc)
    try:
        process.wait(timeout=10)
    except Exception as exc:
        evidence["direct_wait_error"] = str(exc)
    evidence["direct_process_stopped"] = process.poll() is not None
    return evidence


def new_progress_state(provider_name="gemini"):
    """Mutable incremental parser state; private fields never reach artifacts."""
    return {
        "provider": provider_name,
        "offset": 0,
        "remainder": b"",
        "event_count": 0,
        "malformed_event_count": 0,
        "completed_steps": set(),
        "active_step": None,
        "last_event": None,
        "last_event_at": None,
        "terminal": None,
        "usage": None,
        "conversation_id": None,
        "last_error": None,
        "discarding_oversize_line": False,
        "last_emitted_monotonic": None,
        "muse_root_run_id": None,
        "muse_session_id": None,
        "muse_terminal": None,
    }


def new_gemini_progress_state():
    return new_progress_state("gemini")


def compact_usage(value):
    if not isinstance(value, dict):
        return None
    allowed = (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cache_read_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
    )
    data = dict(value)
    if "cache_read_tokens" not in data and "cache_read_input_tokens" in data:
        data["cache_read_tokens"] = data["cache_read_input_tokens"]
    if "total_tokens" not in data:
        inp = data.get("input_tokens")
        out = data.get("output_tokens")
        if (isinstance(inp, (int, float)) and not isinstance(inp, bool) and
                isinstance(out, (int, float)) and not isinstance(out, bool)):
            data["total_tokens"] = inp + out
    usage = {key: data[key] for key in allowed
             if (isinstance(data.get(key), (int, float)) and
                 not isinstance(data.get(key), bool) and
                 math.isfinite(data[key]) and data[key] >= 0)}
    return usage or None


def compact_label(value, default="unknown", limit=80):
    """Keep diagnostic labels identifier-like so content cannot leak through typed fields."""
    if not isinstance(value, str):
        return default
    value = value.strip()
    if not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        return default
    if _sanitize_all_secrets(value) != value:
        return default
    return value


def compact_gemini_label(value, default="unknown", limit=80):
    return compact_label(value, default=default, limit=limit)


def compact_gemini_event(value):
    """Summarize one stream event without copying prompts, text, parameters, or tool output."""
    event = compact_label(value.get("event"))
    if event == "init":
        init = value.get("init") if isinstance(value.get("init"), dict) else {}
        summary = {"event": "init", "summary": "initialized"}
        model = compact_label(init.get("model"), default=None, limit=120)
        if model:
            summary["model"] = model
        return summary
    if event == "step_update":
        step = value.get("step_update") if isinstance(value.get("step_update"), dict) else {}
        step_type = compact_label(step.get("step_type"))
        step_state = compact_label(step.get("state"), limit=40)
        tool_name = compact_label(step.get("tool_name"), default=None, limit=120)
        label = f"{step_type} {step_state}"
        summary = {"event": "step_update", "summary": label,
                   "step_type": step_type, "step_state": step_state}
        if isinstance(step.get("step_index"), int) and not isinstance(step.get("step_index"), bool):
            summary["step_index"] = step["step_index"]
        if tool_name:
            summary["tool_name"] = tool_name
            summary["summary"] = f"tool {tool_name} {step_state}"
        if (isinstance(step.get("duration_seconds"), (int, float)) and
                not isinstance(step.get("duration_seconds"), bool) and
                math.isfinite(step["duration_seconds"]) and step["duration_seconds"] >= 0):
            summary["duration_seconds"] = step["duration_seconds"]
        usage = compact_usage(step.get("usage"))
        if usage:
            summary["usage"] = usage
        return summary
    if event == "result":
        terminal = value.get("result") if isinstance(value.get("result"), dict) else {}
        status = compact_label(terminal.get("status"), limit=40)
        summary = {"event": "result", "summary": f"terminal {status}", "status": status}
        if (isinstance(terminal.get("duration_seconds"), (int, float)) and
                not isinstance(terminal.get("duration_seconds"), bool) and
                math.isfinite(terminal["duration_seconds"]) and terminal["duration_seconds"] >= 0):
            summary["duration_seconds"] = terminal["duration_seconds"]
        usage = compact_usage(terminal.get("usage"))
        if usage:
            summary["usage"] = usage
        return summary
    return {"event": event, "summary": event}


def update_gemini_progress(state, value, observed_at):
    if not isinstance(value, dict):
        return
    state["event_count"] += 1
    summary = compact_gemini_event(value)
    state["last_event"] = summary
    state["last_event_at"] = observed_at
    conversation_id = value.get("conversation_id")
    if not conversation_id and isinstance(value.get("step_update"), dict):
        conversation_id = value["step_update"].get("conversation_id")
    if not conversation_id and isinstance(value.get("result"), dict):
        conversation_id = value["result"].get("conversation_id")
    conversation_id = compact_label(conversation_id, default=None, limit=200)
    if conversation_id:
        state["conversation_id"] = conversation_id

    if summary["event"] == "step_update":
        step_identity = summary.get("step_index")
        if step_identity is None:
            step_identity = state["event_count"]
        key = (step_identity, summary.get("step_type"))
        if summary.get("step_state") == "ACTIVE":
            state["active_step"] = {key: summary[key] for key in
                                    ("step_index", "step_type", "step_state", "tool_name", "summary") if key in summary}
        elif summary.get("step_state") == "DONE":
            state["completed_steps"].add(key)
            if (state.get("active_step") or {}).get("step_index") == summary.get("step_index"):
                state["active_step"] = None
        if summary.get("usage"):
            state["usage"] = summary["usage"]
    elif summary["event"] == "result":
        state["terminal"] = {key: summary[key] for key in
                             ("status", "duration_seconds", "summary") if key in summary}
        state["active_step"] = None
        if summary.get("usage"):
            state["usage"] = summary["usage"]
    elif summary["event"] == "error":
        state["last_error"] = {"summary": "error", "observed_at": observed_at}


def compact_claude_event(value):
    """Summarize one Claude stream-json event without copying prompts, text, parameters, or tool output."""
    event_type = compact_label(value.get("type") or value.get("event"))
    if event_type == "system":
        subtype = compact_label(value.get("subtype"), default=None, limit=40)
        label = f"system {subtype}" if subtype else "initialized"
        summary = {"event": "system", "summary": label}
        if subtype:
            summary["subtype"] = subtype
        return summary
    if event_type == "assistant":
        msg = value.get("message") if isinstance(value.get("message"), dict) else value
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        tool_name = None
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                candidate = compact_label(item.get("name"), default=None, limit=120)
                if candidate:
                    tool_name = candidate
                    break
        summary = {"event": "assistant"}
        if tool_name:
            summary["tool_name"] = tool_name
            summary["step_type"] = "tool"
            summary["step_state"] = "ACTIVE"
            summary["summary"] = f"tool {tool_name} ACTIVE"
        else:
            summary["summary"] = "assistant turn"
        usage = compact_usage(msg.get("usage") or value.get("usage"))
        if usage:
            summary["usage"] = usage
        return summary
    if event_type == "user":
        msg = value.get("message") if isinstance(value.get("message"), dict) else value
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        has_tool_result = any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
        summary = {"event": "user"}
        if has_tool_result:
            summary["step_type"] = "tool"
            summary["step_state"] = "DONE"
            summary["summary"] = "tool DONE"
        else:
            summary["summary"] = "user turn"
        return summary
    if event_type == "result":
        subtype = compact_label(value.get("subtype"), default="unknown", limit=40)
        is_error = value.get("is_error") is True
        status = "SUCCESS" if (subtype == "success" and not is_error) else ("ERROR" if is_error else (subtype.upper() if subtype != "unknown" else "UNKNOWN"))
        summary = {"event": "result", "status": status, "summary": f"terminal {status}"}
        if (isinstance(value.get("duration_seconds"), (int, float)) and
                not isinstance(value.get("duration_seconds"), bool) and
                math.isfinite(value["duration_seconds"]) and value["duration_seconds"] >= 0):
            summary["duration_seconds"] = value["duration_seconds"]
        elif (isinstance(value.get("duration_ms"), (int, float)) and
              not isinstance(value.get("duration_ms"), bool) and
              math.isfinite(value["duration_ms"]) and value["duration_ms"] >= 0):
            summary["duration_seconds"] = value["duration_ms"] / 1000
        usage = compact_usage(value.get("usage"))
        if usage:
            summary["usage"] = usage
        return summary
    if event_type == "error":
        return {"event": "error", "summary": "error"}
    return {"event": event_type, "summary": event_type}


def update_claude_progress(state, value, observed_at):
    if not isinstance(value, dict):
        return
    state["event_count"] += 1
    summary = compact_claude_event(value)
    state["last_event"] = summary
    state["last_event_at"] = observed_at
    session_id = compact_label(value.get("session_id") or value.get("conversation_id"), default=None, limit=200)
    if session_id:
        state["conversation_id"] = session_id

    if summary.get("step_state") == "ACTIVE":
        state["active_step"] = {k: summary[k] for k in
                                ("step_type", "step_state", "tool_name", "summary") if k in summary}
    elif summary.get("step_state") == "DONE":
        tool_name = (state.get("active_step") or {}).get("tool_name") or summary.get("tool_name") or "tool"
        state["completed_steps"].add(("tool", tool_name, state["event_count"]))
        state["active_step"] = None
        if tool_name and summary.get("summary") == "tool DONE":
            summary["tool_name"] = tool_name
            summary["summary"] = f"tool {tool_name} DONE"
            state["last_event"] = summary
    elif summary.get("event") == "result":
        state["terminal"] = {k: summary[k] for k in
                             ("status", "duration_seconds", "summary") if k in summary}
        state["active_step"] = None
    elif summary.get("event") == "error":
        state["last_error"] = {"summary": "error", "observed_at": observed_at}
    if summary.get("usage"):
        state["usage"] = summary["usage"]


def compact_deepseek_event(value):
    """Summarize one Codex JSONL event without copying prompts, text, parameters, or tool output."""
    event_type = compact_label(value.get("type") or value.get("event"))
    if event_type == "thread.started":
        return {"event": "thread.started", "summary": "thread started"}
    if event_type == "turn.started":
        return {"event": "turn.started", "summary": "turn started"}
    if event_type == "turn.completed":
        summary = {"event": "turn.completed", "summary": "turn completed"}
        usage = compact_usage(value.get("usage"))
        if usage:
            summary["usage"] = usage
        return summary
    if event_type == "turn.failed":
        return {"event": "turn.failed", "status": "FAILED", "summary": "terminal FAILED"}
    if event_type == "item.started":
        item = value.get("item") if isinstance(value.get("item"), dict) else {}
        item_type = compact_label(item.get("type"), default="item", limit=60)
        if item_type in CODEX_TOOL_ITEM_TYPES:
            candidate = ((item.get("tool") or item.get("name"))
                         if item_type == "mcp_tool_call" else item_type)
            tool_name = compact_label(candidate, default=item_type, limit=120)
            return {"event": "item.started", "step_type": "item", "tool_name": tool_name,
                    "step_state": "ACTIVE", "summary": f"tool {tool_name} ACTIVE"}
        return {"event": "item.started", "summary": f"{item_type} started"}
    if event_type == "item.completed":
        item = value.get("item") if isinstance(value.get("item"), dict) else {}
        item_type = compact_label(item.get("type"), default="item", limit=60)
        if item_type in CODEX_TOOL_ITEM_TYPES:
            candidate = ((item.get("tool") or item.get("name"))
                         if item_type == "mcp_tool_call" else item_type)
            tool_name = compact_label(candidate, default=item_type, limit=120)
            return {"event": "item.completed", "step_type": "item", "tool_name": tool_name,
                    "step_state": "DONE", "summary": f"tool {tool_name} DONE"}
        return {"event": "item.completed", "summary": f"{item_type} completed"}
    if event_type == "error":
        return {"event": "error", "summary": "error"}
    # DeepSeek final response schema line emitted in stdout
    if isinstance(value.get("status"), str) and "response" in value and event_type == "unknown":
        status = compact_label(value.get("status"), default="unknown", limit=40)
        return {"event": "result", "status": status, "summary": f"terminal {status}"}
    return {"event": event_type, "summary": event_type}


def update_deepseek_progress(state, value, observed_at):
    if not isinstance(value, dict):
        return
    state["event_count"] += 1
    summary = compact_deepseek_event(value)
    state["last_event"] = summary
    state["last_event_at"] = observed_at
    thread_id = compact_label(value.get("thread_id") or value.get("conversation_id"), default=None, limit=200)
    if thread_id:
        state["conversation_id"] = thread_id

    if summary.get("step_state") == "ACTIVE":
        state["active_step"] = {k: summary[k] for k in
                                ("step_type", "step_state", "tool_name", "summary") if k in summary}
    elif summary.get("step_state") == "DONE":
        tool_name = summary.get("tool_name") or (state.get("active_step") or {}).get("tool_name") or "item"
        item_id = (value.get("item") or {}).get("id") if isinstance(value.get("item"), dict) else None
        item_key = compact_label(item_id, default=None, limit=100) if isinstance(item_id, str) else None
        key = item_key or ("item", tool_name, state["event_count"])
        state["completed_steps"].add(key)
        if (state.get("active_step") or {}).get("tool_name") == tool_name:
            state["active_step"] = None
    elif summary.get("event") == "turn.completed":
        state["completed_steps"].add(("turn", state["event_count"]))
        state["active_step"] = None
    elif summary.get("event") in ("turn.failed", "result"):
        state["terminal"] = {k: summary[k] for k in
                             ("status", "summary") if k in summary}
        state["active_step"] = None
    elif summary.get("event") == "error":
        state["last_error"] = {"summary": "error", "observed_at": observed_at}
    if summary.get("usage"):
        state["usage"] = summary["usage"]


def update_progress(state, value, observed_at):
    provider = state.get("provider", "gemini")
    if provider == "muse":
        update_muse_progress(state, value, observed_at)
    elif provider == "claude":
        update_claude_progress(state, value, observed_at)
    elif provider == "deepseek":
        update_deepseek_progress(state, value, observed_at)
    elif provider == "openai":
        update_openai_progress(state, value, observed_at)
    else:
        update_gemini_progress(state, value, observed_at)


def update_openai_progress(state, value, observed_at):
    """Keep Codex JSONL telemetry separate from the legacy DeepSeek schema."""
    state["event_count"] += 1
    kind = value.get("type") if isinstance(value, dict) else None
    state["last_event"] = {"event": kind, "summary": compact_label(kind, default="unknown")}
    state["last_event_at"] = observed_at
    if kind in ("turn.completed", "turn.failed"):
        state["terminal"] = "SUCCESS" if kind == "turn.completed" else "FAILED"
    if kind == "thread.started" and isinstance(value.get("thread_id"), str):
        state["conversation_id"] = value["thread_id"]


def update_muse_progress(state, value, observed_at):
    """Retain only root-run evidence from native Muse exec JSONL v1."""
    state["event_count"] += 1
    event = value.get("payload_type")
    state["last_event"] = {"event": event, "summary": compact_label(event, default="unknown", limit=80)}
    state["last_event_at"] = observed_at
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1 or not isinstance(value.get("payload"), dict):
        return
    stream = value.get("stream")
    if not isinstance(stream, dict) or stream.get("kind") != "session":
        return
    payload = value["payload"]
    run_stream = payload.get("run_stream")
    run_id = run_stream.get("id") if isinstance(run_stream, dict) and run_stream.get("kind") == "run" else None
    command_id = payload.get("command_id")
    if event == "session.run.linked" and payload.get("kind") == "session_run_linked":
        if (type(value.get("payload_schema_version")) is int and value["payload_schema_version"] == 1 and
                isinstance(stream.get("id"), str) and stream["id"] and
                isinstance(run_id, str) and run_id and run_id == command_id and state["muse_root_run_id"] is None):
            state["muse_root_run_id"] = run_id
            state["muse_session_id"] = stream["id"]
        return
    if not isinstance(event, str) or not event.startswith("run.terminal."):
        return
    if (not state["muse_root_run_id"] or stream.get("id") != state["muse_session_id"] or
            run_id != state["muse_root_run_id"] or
            command_id != state["muse_root_run_id"] or payload.get("kind") != "run_terminal"):
        return
    terminal = payload.get("terminal")
    if (terminal not in ("completed", "failed", "cancelled") or event != "run.terminal." + terminal or
            type(value.get("payload_schema_version")) is not int or value["payload_schema_version"] != 1 or
            not isinstance(payload.get("text"), str) or
            "reason" not in payload or (payload["reason"] is not None and not isinstance(payload["reason"], str))):
        return
    text_value = payload.get("text")
    reason_value = payload.get("reason")
    if isinstance(reason_value, str) and len(reason_value) > 4096:
        return
    state["muse_terminal"] = {"terminal": terminal,
                              "text": text_value[:4096] if isinstance(text_value, str) else "",
                              "response_nonempty": bool(text_value.strip()),
                              "reason": reason_value or ""}
    state["terminal"] = {"status": terminal, "summary": "root " + terminal}


def progress_snapshot(state, started_wall, started_monotonic, adapter_state):
    now = time.time()
    snapshot = {
        "provider": state.get("provider", "unknown"),
        "adapter_state": adapter_state,
        "started_at": dt.datetime.fromtimestamp(started_wall, UTC).isoformat().replace("+00:00", "Z"),
        "updated_at": dt.datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "stream_offset_bytes": state["offset"],
        "stream_event_count": state["event_count"],
        "malformed_event_count": state["malformed_event_count"],
        "completed_step_count": len(state["completed_steps"]),
        "last_event_at": state["last_event_at"],
        "last_event": state["last_event"],
        "active_step": state["active_step"],
        "terminal": state["terminal"],
        "last_error": state.get("last_error"),
        "usage": state["usage"],
        "conversation_id": state["conversation_id"],
    }
    if state.get("provider") == "muse":
        for key in ("muse_profile", "model", "effort"):
            if state.get(key) is not None:
                snapshot[key] = state[key]
    return snapshot


def gemini_progress_snapshot(state, started_wall, started_monotonic, adapter_state):
    return progress_snapshot(state, started_wall, started_monotonic, adapter_state)


def _consume_progress_chunk(state, chunk, observed_at, final=False):
    """Consume one bounded stream chunk and retain at most one capped partial line."""
    if state.get("discarding_oversize_line"):
        newline = chunk.find(b"\n")
        if newline < 0:
            if final:
                state["discarding_oversize_line"] = False
            return 0
        else:
            chunk = chunk[newline + 1:]
            state["discarding_oversize_line"] = False

    data = state["remainder"] + chunk
    lines = data.split(b"\n")
    if final:
        state["remainder"] = b""
    else:
        state["remainder"] = lines.pop() if lines else data
        if len(state["remainder"]) > MAX_STREAM_LINE_BYTES:
            state["malformed_event_count"] += 1
            state["remainder"] = b""
            state["discarding_oversize_line"] = True

    new_events = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) > MAX_STREAM_LINE_BYTES:
            state["malformed_event_count"] += 1
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            state["malformed_event_count"] += 1
            continue
        if not isinstance(value, dict):
            state["malformed_event_count"] += 1
            continue
        update_progress(state, value, observed_at)
        new_events += 1
    return new_events


def refresh_progress(output, state, started_wall, started_monotonic,
                     adapter_state="running", final=False):
    """Incrementally consume provider JSONL/NDJSON and atomically persist a compact snapshot."""
    path = Path(output) / "stdout.log"
    observed_at = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
    new_events = 0
    malformed_before = state["malformed_event_count"]
    try:
        # Read from the saved offset every time. In particular, do not depend
        # on concurrently reported file-size metadata being fresh on Windows.
        # Bound each allocation even when a provider emits a huge line. During
        # a live run, cap work per poll; at process exit, drain to EOF in the
        # same bounded chunks so a terminal event cannot remain unread.
        with path.open("rb") as stream:
            stream.seek(state["offset"])
            budget = None if final else MAX_STREAM_LINE_BYTES + STREAM_READ_CHUNK_BYTES
            consumed = 0
            while budget is None or consumed < budget:
                read_size = STREAM_READ_CHUNK_BYTES
                if budget is not None:
                    read_size = min(read_size, budget - consumed)
                chunk = stream.read(read_size)
                if not chunk:
                    break
                state["offset"] += len(chunk)
                consumed += len(chunk)
                new_events += _consume_progress_chunk(state, chunk, observed_at)
    except FileNotFoundError:
        pass

    if final:
        new_events += _consume_progress_chunk(state, b"", observed_at, final=True)

    snapshot = progress_snapshot(state, started_wall, started_monotonic, adapter_state)
    if new_events or final or state["malformed_event_count"] != malformed_before:
        write_json(Path(output) / "progress.json", snapshot)
    return snapshot, new_events


def refresh_gemini_progress(*args, **kwargs):
    return refresh_progress(*args, **kwargs)


def safe_refresh_progress(result, *args, **kwargs):
    try:
        return refresh_progress(*args, **kwargs)
    except Exception as exc:
        result["progress_error"] = str(exc)
        return None, 0


def safe_refresh_gemini_progress(result, *args, **kwargs):
    return safe_refresh_progress(result, *args, **kwargs)


def emit_progress(snapshot):
    if not snapshot or not snapshot.get("last_event"):
        return
    last = snapshot["last_event"].get("summary", "unknown")
    provider = snapshot.get("provider", "provider")
    print(f"{provider} progress: events={snapshot['stream_event_count']}; last={last}; "
          f"completed_steps={snapshot['completed_step_count']}; no response text relayed",
          file=sys.stderr, flush=True)


def emit_gemini_progress(snapshot):
    emit_progress(snapshot)


def maybe_emit_progress(result, state, snapshot, force=False):
    """Relay bounded state changes without allowing diagnostic I/O to affect the run."""
    if not snapshot or not snapshot.get("last_event"):
        return
    last_event = snapshot["last_event"]
    terminal = last_event.get("event") in ("result", "turn.failed")
    step_transition = (
        (last_event.get("event") == "step_update" and last_event.get("step_state") in ("ACTIVE", "DONE")) or
        (last_event.get("event") in ("item.started", "item.completed", "assistant", "user") and
         last_event.get("step_state") in ("ACTIVE", "DONE")) or
        (last_event.get("event") in ("turn.started", "turn.completed"))
    )
    if not (force or terminal or step_transition):
        return
    now = time.monotonic()
    last_emitted = state.get("last_emitted_monotonic")
    if not (force or terminal) and last_emitted is not None and now - last_emitted < STREAM_PROGRESS_EMIT_SECONDS:
        return
    try:
        if state.get("provider") == "gemini" or snapshot.get("provider") in ("gemini", None):
            emit_gemini_progress(snapshot)
        else:
            emit_progress(snapshot)
        state["last_emitted_monotonic"] = now
    except Exception as exc:
        result["progress_error"] = str(exc)


def maybe_emit_gemini_progress(result, state, snapshot, force=False):
    maybe_emit_progress(result, state, snapshot, force=force)


def provider_timing(config, provider_name, provider=None):
    """Resolve a provider soft timeout and its adapter-owned termination grace."""
    if provider is None:
        provider = config["providers"][provider_name]
    default_timeout = 3600 if provider_name == "gemini" else 1800
    timeout = provider.get("timeout_seconds", config.get("timeout_seconds", default_timeout))
    grace_default = 120 if provider_name == "gemini" else 0
    grace = provider.get("termination_grace_seconds", grace_default)
    heartbeat_default = 60
    heartbeat = provider.get("heartbeat_seconds", config.get("heartbeat_seconds", heartbeat_default))
    try:
        timeout, grace, heartbeat = float(timeout), float(grace), float(heartbeat)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider timeout, termination grace, and heartbeat values must be numbers") from exc
    if (not all(math.isfinite(value) for value in (timeout, grace, heartbeat)) or
            not math.isfinite(timeout + grace) or timeout <= 0 or grace < 0 or heartbeat < 0):
        raise ValueError("provider timeout must be positive; termination grace and heartbeat must be non-negative")
    if 0 < heartbeat < MIN_HEARTBEAT_SECONDS:
        heartbeat = MIN_HEARTBEAT_SECONDS
    return timeout, grace, heartbeat


def heartbeat(output, provider_name, provider, process, state, started_wall, started_monotonic,
              progress=None):
    """Write diagnostic liveness only; process/log activity is not task progress."""
    now = time.time()
    muse_meta = {}
    if provider_name == "muse":
        if provider.get("muse_profile"):
            muse_meta["muse_profile"] = provider["muse_profile"]
        muse_meta["effort"] = provider.get("effort", "medium")
    data = {"provider": provider_name, "model": provider["model"], "pid": process.pid, **muse_meta,
            "state": state, "started_at": dt.datetime.fromtimestamp(started_wall, UTC).isoformat().replace("+00:00", "Z"),
            "updated_at": dt.datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
            "process_alive": process.poll() is None}
    activity = []
    for kind, name in (("provider", "provider.log"), ("stdout", "stdout.log")):
        try:
            stat = (Path(output) / name).stat()
        except OSError:
            continue
        observed = dt.datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z")
        data[f"{kind}_log_last_activity_at"] = observed
        data[f"{kind}_log_size_bytes"] = stat.st_size
        activity.append((stat.st_mtime, kind, observed))
    if activity:
        _, kind, observed = max(activity)
        data["activity_log_kind"] = kind
        data["activity_log_last_activity_at"] = observed
    if progress:
        data["progress"] = {
            key: progress.get(key) for key in
            ("stream_event_count", "completed_step_count", "last_event_at", "last_event",
             "active_step", "terminal", "last_error", "usage")
        }
    write_json(Path(output) / "heartbeat.json", data)
    return data


def emit_heartbeat(data):
    """Keep stdout reserved for the adapter's single structured result."""
    activity = data.get("activity_log_last_activity_at",
                        data.get("provider_log_last_activity_at", "unobserved"))
    progress = data.get("progress") or {}
    last = (progress.get("last_event") or {}).get("summary", "unobserved")
    print(f"adapter heartbeat: {data['provider']} local_process_alive={data['process_alive']}; "
          f"stream_events={progress.get('stream_event_count', 0)}; last_stream_event={last}; "
          f"output_activity={activity}; diagnostic only, not task completion",
          file=sys.stderr, flush=True)


def safe_heartbeat(result, *args):
    """Diagnostic evidence must never replace the provider's primary outcome."""
    try:
        data = heartbeat(*args)
        emit_heartbeat(data)
        return data
    except Exception as exc:
        result["heartbeat_error"] = str(exc)
        return None


def git_evidence(workspace, output, secrets=None):
    environment = dict(os.environ)
    secret_values = {value.strip() for value in (secrets or [])
                     if isinstance(value, str) and value.strip()}
    if secret_values:
        for name, value in list(environment.items()):
            if isinstance(value, str) and value.strip() in secret_values:
                environment.pop(name, None)
    paths = {}
    for name, args in (("status", ["status", "--short"]),
                       ("diff", ["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"]),
                       ("head", ["rev-parse", "HEAD"])):
        path = output / (name + ".txt")
        try:
            run = subprocess.run(["git", "-C", str(workspace), "-c", "core.fsmonitor=false"] + args,
                                 capture_output=True, timeout=30, check=False, env=environment)
            write_bytes(path, run.stdout + run.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            write_bytes(path, str(exc).encode("utf-8"))
        paths[name] = str(path)
    return paths


def execute(args):
    workspace = Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    config = read_json(args.config)
    workflow = config.get("workflow", {})
    if not isinstance(workflow, dict):
        raise ValueError("Routing workflow must be an object")
    require_jev = workflow.get("require_jev_evidence", False)
    if not isinstance(require_jev, bool):
        raise ValueError("Routing workflow.require_jev_evidence must be a boolean")
    quota_cooldown(config)
    activity = getattr(args, "activity", None)
    provider_name = getattr(args, "provider", None) or ("gemini" if args.role == "implement" else "claude")
    if activity:
        if getattr(args, "profile", None) not in config.get("profiles", {}):
            raise ValueError("Activity execution requires a configured profile")
        expected_role = "implement" if activity in WRITE_ACTIVITIES else "review"
        if args.role != expected_role or config["profiles"][args.profile]["provider"] != provider_name:
            raise ValueError("Activity role/profile/provider mismatch")
    elif getattr(args, "profile", None):
        raise ValueError("--profile is only valid with dispatch")
    if not activity and args.role == "review" and provider_name != "claude":
        raise ValueError(f"Review role is Claude-only; got {provider_name}")
    if not activity and args.role == "implement" and provider_name not in ("gemini", "deepseek", "muse"):
        raise ValueError(f"Implement role does not support provider {provider_name}")
    if provider_name not in config.get("providers", {}):
        raise ValueError(f"Provider '{provider_name}' is missing from config")
    muse_profile_arg = getattr(args, "muse_profile", None)
    require_muse_profile_allowed(muse_profile_arg, provider_name, role=args.role)
    muse_meta = {}
    if activity:
        _, provider = effective_activity_provider(config, args.profile)
    elif provider_name == "muse":
        provider, muse_meta = resolve_muse_profile(config, muse_profile_arg)
    else:
        provider = config["providers"][provider_name]
    timeout, termination_grace, heartbeat_seconds = provider_timing(config, provider_name, provider=provider)
    review_effort_arg = getattr(args, "review_effort", None)
    review_reason_arg = getattr(args, "review_reason", None)
    if activity:
        review_effort = provider.get("effort", "medium")
        review_reason = getattr(args, "review_reason", None)
    elif args.role == "implement":
        if review_effort_arg is not None or review_reason_arg is not None:
            raise ValueError("Review effort and reason options are only permitted for review role")
        review_effort = None
        review_reason = None
    else:
        configured_effort = provider.get("effort", "medium")
        if configured_effort != "medium":
            raise ValueError(f"Configured Claude effort must be 'medium'; got {configured_effort}")
        if review_effort_arg is None:
            review_effort = "medium"
        elif review_effort_arg in ("medium", "high"):
            review_effort = review_effort_arg
        else:
            raise ValueError(f"Claude review effort must be 'medium' or 'high'; got {review_effort_arg}")
        if review_effort == "high":
            if not review_reason_arg or not review_reason_arg.strip():
                raise ValueError("High review effort requires a non-empty review reason")
            review_reason = review_reason_arg.strip()
        else:
            review_reason = review_reason_arg.strip() if (review_reason_arg and review_reason_arg.strip()) else None
    task = read_json(args.task_file)
    orchestration_id = str(uuid.uuid4()) if activity == "orchestration" else None
    if orchestration_id and not task.get("allow_delegation"):
        raise ValueError("Orchestration requires allow_delegation=true")
    jev_meta = None
    if require_jev and (not activity or activity in ("implementation", "implementation_complex", "implementation_long_horizon",
                                                 "independent_review", "security_review", "deep_review")):
        jev_meta = validate_jev_task(task, workspace,
                                     "review" if activity in ("independent_review", "security_review", "deep_review") else "implement" if activity else args.role)
    owned_paths = normalized_owned_paths(workspace, task.get("owned_paths"))
    if orchestration_id and not owned_paths:
        raise ValueError("Orchestration requires explicit owned_paths")
    owned_claims = [os.path.normcase(str((workspace / path).resolve())) for path in owned_paths]
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state")).resolve()
    prompt = prompt_for(workspace, args.task_file, task, activity=activity)
    if activity == "orchestration":
        prompt += ("\nDISPATCH COMMAND: " + sys.executable + " " + str(Path(__file__).resolve()) +
                   " dispatch --activity <ACTIVITY> --workspace " + str(workspace) +
                   " --task-file <CHILD_CONTRACT_JSON> --config " + str(Path(args.config).resolve()) +
                   " --state-dir " + str(state_dir) +
                   " --parent-run-id " + orchestration_id +
                   "\nWrite each child contract under workspace .llm-output, assign explicit disjoint owned_paths, "
                   "call the dispatcher for authorized children, wait for terminal results, and inspect check evidence. "
                   "Stop if a child has unresolved pending ownership.\n")
    if getattr(args, "prior_context", None):
        prompt += "\nPrevious route attempts (preserve partial edits and finish remaining checks):\n" + json.dumps(args.prior_context)
    if args.dry_run:
        command = command_for(args.role, provider, prompt, timeout, workspace, review_effort or "medium", provider_name=provider_name,
                              output_dir=workspace / ".llm-output", activity=activity, state_dir=state_dir)
        dry_run_meta = {"status": "dry_run", "provider": provider_name, "model": provider["model"],
                        **muse_meta,
                        "cwd": str(workspace), "command": command, "state_dir": str(state_dir),
                        "soft_timeout_seconds": timeout, "termination_grace_seconds": termination_grace,
                        "outer_timeout_seconds": timeout + termination_grace,
                        "heartbeat_seconds": heartbeat_seconds}
        if args.role == "review":
            dry_run_meta["review_effort"] = review_effort
            dry_run_meta["review_reason"] = review_reason
        if jev_meta is not None:
            dry_run_meta["jev"] = jev_meta
        return dry_run_meta, 0

    secrets = [value.strip() for _, value in framework_secrets(config)]
    checked_deepseek_key = None
    if provider_name == "deepseek":
        ds_env = provider.get("api_key_env", "DEEPSEEK_API_KEY")
        key = os.environ.get(ds_env)
        if key and key.strip():
            checked_deepseek_key = key.strip()

        if provider.get("enabled", True) is False:
            output = workspace / ".llm-output" / "agent-framework" / str(uuid.uuid4())
            output.mkdir(parents=True, exist_ok=True)
            result = {
                "provider": "deepseek",
                "model": provider["model"],
                "logs": str(output),
                "status": "fallback_required",
                "fallback": luna_fallback(),
                "error": "DeepSeek provider is disabled in configuration (enabled=false)",
            }
            return finish(result, output, workspace, 20, secrets=secrets)

        output = workspace / ".llm-output" / "agent-framework" / str(uuid.uuid4())
        try:
            data, balance_err = query_deepseek_balance(config)
            record_balance_snapshot(state_dir, data, balance_err, config)
        except Exception as exc:
            output.mkdir(parents=True, exist_ok=True)
            result = {
                "provider": "deepseek",
                "model": provider["model"],
                "logs": str(output),
                "status": "balance_check_failed",
                "fallback": luna_fallback(),
                "error": f"DeepSeek balance preflight failed: {exc}",
            }
            return finish(result, output, workspace, 1, secrets=secrets)

        if balance_err is not None:
            output.mkdir(parents=True, exist_ok=True)
            kind, _ = classify_balance_error(balance_err)
            status = "fallback_required" if kind == "depleted" else "balance_check_failed"
            result = {
                "provider": "deepseek",
                "model": provider["model"],
                "logs": str(output),
                "status": status,
                "fallback": luna_fallback(),
                "error": f"DeepSeek balance preflight failed: {balance_err}",
            }
            if data:
                result["balance"] = data
            return finish(result, output, workspace, 20 if status == "fallback_required" else 1, secrets=secrets)
    else:
        output = workspace / ".llm-output" / "agent-framework" / str(uuid.uuid4())
        output.mkdir(parents=True, exist_ok=True)

    # Create the per-run output directory exactly once, after DeepSeek's live
    # preflight.  Command construction itself is deliberately side-effect free.
    output.mkdir(parents=True, exist_ok=True)
    if provider_name == "deepseek":
        (output / "output_schema.json").write_text(json.dumps(DEEPSEEK_OUTPUT_SCHEMA, indent=2), encoding="utf-8")
    # Avoid Windows' command-line length limit; providers have a Read tool.
    prompt_path = output / "prompt.txt"
    if provider_name == "muse" and activity != "orchestration":
        prompt += "\nExecute this bounded task yourself. Do not spawn subagents or invoke another AI provider.\n"
    prompt_path.write_text(prompt, encoding="utf-8")
    run_prompt = (f"Read the complete task contract and instructions in {prompt_path} and follow them. " +
                  ("Use the shared framework dispatcher for authorized child tasks and wait for their results." if activity == "orchestration" else
                   "Execute the task yourself; never spawn subagents or use another AI provider."))
    if provider_name == "deepseek":
        run_prompt += (
            " When complete, return your final response as a JSON object strictly conforming to the schema: "
            '{"status": "SUCCESS" | "FAILED" | "BLOCKED" | "ERROR", "response": "<summary>"}.'
        )
    command = command_for(args.role, provider, run_prompt,
                          timeout, workspace, review_effort or "medium", provider_name=provider_name, output_dir=output,
                          activity=activity, state_dir=state_dir)
    result = {"provider": provider_name, "model": provider["model"], **muse_meta, "logs": str(output),
              "soft_timeout_seconds": timeout, "termination_grace_seconds": termination_grace,
              "outer_timeout_seconds": timeout + termination_grace,
              "heartbeat_seconds": heartbeat_seconds,
              "progress_path": str(output / "progress.json")}
    if jev_meta is not None:
        result["jev"] = jev_meta
    if args.role == "review":
        result["review_effort"] = review_effort
        result["review_reason"] = review_reason
    workspace_state = workspace_state_dir(state_dir, workspace)
    # This lock covers only the check-and-claim transaction. Provider processes run
    # concurrently once their disjoint ownership claims have been persisted.
    claims_writer = args.role == "implement" or orchestration_id is not None
    lock = provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)),
                         "gemini.lock" if claims_writer else "quota.lock")
    try:
        with lock as release_claim_lock:
            state_path = quota_path(state_dir, provider_name)
            pending_path = workspace_state / "gemini-pending" / (str(uuid.uuid4()) + ".json")
            if args.role == "implement":
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                result["pending_path"] = str(pending_path)
            if claims_writer:
                legacy = legacy_pending_for(state_dir, workspace)
                if legacy is not None:
                    result.update(status="blocked_pending_run", pending=legacy,
                                  pending_path=str(state_dir / "gemini-pending.json"),
                                  fallback=luna_fallback(),
                                  error="Unresolved legacy run belongs to this workspace or has unknown ownership. Confirm it stopped before resolving its pending record.")
                    release_claim_lock()
                    return finish(result, output, workspace, 1, secrets=secrets)
            if claims_writer:
                for existing_path, pending in all_pending_records(state_dir):
                    if not isinstance(pending, dict) or pending.get("status") != "resolved":
                        pending_workspace, pending_owned_paths = pending_claims(pending)
                        if pending_workspace is None or claims_overlap(workspace, owned_claims, pending_workspace, pending_owned_paths):
                            result.update(status="blocked_pending_run", pending=pending,
                                          pending_path=str(existing_path),
                                          fallback=luna_fallback(),
                                          error="An unresolved provider run owns overlapping paths. Confirm it stopped, then mark its pending record resolved before retrying.")
                            release_claim_lock()
                            return finish(result, output, workspace, 1, secrets=secrets)
                registered_child = getattr(args, "_registered_child_path", None)
                for existing_path, parent in all_orchestration_records(state_dir):
                    if isinstance(parent, dict) and parent.get("status") == "resolved":
                        continue
                    parent_workspace, parent_owned_paths = pending_claims(parent)
                    if parent_workspace is not None and not claims_overlap(
                            workspace, owned_claims, parent_workspace, parent_owned_paths):
                        continue
                    child_of_parent = (registered_child is not None and
                                       Path(registered_child).parent.parent.with_suffix(".json") == existing_path and
                                       read_json(registered_child).get("status") == "running")
                    if not child_of_parent:
                        result.update(status="blocked_pending_run", pending=parent,
                                      pending_path=str(existing_path), fallback_authorized=False,
                                      error="An unresolved orchestrator owns overlapping paths")
                        release_claim_lock()
                        return finish(result, output, workspace, 1, secrets=secrets)
            if provider_name in ("gemini", "claude", "muse", "openai"):
                state, state_error = quota_state(state_dir, provider_name)
                if state_error:
                    result.update(status="state_error", fallback=luna_fallback() if provider_name == "gemini" else quota_fallback(provider_name, config),
                                  quota_path=str(state_path), error="Malformed or unreadable quota state: " + state_error)
                    release_claim_lock()
                    return finish(result, output, workspace, 1, secrets=secrets)
                if state and state.get("retry_at", 0) > time.time():
                    result.update(status="fallback_required", fallback=quota_fallback(provider_name, config), quota=state, cached=True)
                    release_claim_lock()
                    return finish(result, output, workspace, 20, secrets=secrets)
                if state:
                    result["expired_quota"] = state
            if not claims_writer:
                release_claim_lock()
            if orchestration_id:
                parent_path = orchestration_path(state_dir, workspace, orchestration_id)
                with provider_lock(state_dir, 30, "orchestration.lock"):
                    parent_path.parent.mkdir(parents=True, exist_ok=True)
                    for existing in parent_path.parent.glob("*.json"):
                        previous = settle_orchestration_parent(existing)
                        if previous.get("status") != "resolved":
                            old_scope = [os.path.normcase(str((workspace / path).resolve())) for path in previous.get("owned_paths", [])]
                            if claims_overlap(workspace, owned_claims, workspace, old_scope):
                                result.update(status="blocked_pending_run", pending_path=str(existing),
                                              error="An overlapping orchestrator or its child remains unresolved")
                                return finish(result, output, workspace, 1, secrets=secrets)
                    write_json(parent_path, {"status": "launching", "workspace": str(workspace),
                                             "owned_paths": owned_paths, "run_id": orchestration_id})
                result["_orchestration"] = {"path": str(parent_path), "state_dir": str(state_dir)}
                release_claim_lock()
            with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
                if args.role == "implement":
                    write_json(pending_path, {"status": "launching", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "provider": provider_name, **muse_meta})
                    result["_claim_unresolved"] = True
                process = None
                started_wall = None
                started_monotonic = None
                stream_state = new_progress_state(provider_name)
                if muse_meta:
                    stream_state.update(muse_meta)
                progress_snapshot = None
                try:
                    child_env = child_environment(config, provider_name, checked_deepseek_key)
                    if orchestration_id:
                        child_env["FRAMEWORK_PARENT_RUN_ID"] = orchestration_id
                    process = subprocess.Popen(command, cwd=workspace, shell=False, stdin=subprocess.DEVNULL,
                                               stdout=stdout, stderr=stderr, env=child_env,
                                               start_new_session=os.name != "nt")
                    result["_process_launched"] = True
                    started_wall, started_monotonic = time.time(), time.monotonic()
                    if orchestration_id:
                        with provider_lock(state_dir, 30, "orchestration.lock"):
                            parent = read_json(parent_path)
                            parent.update(status="running", pid=process.pid)
                            write_json(parent_path, parent)
                    if args.role == "implement":
                        write_json(pending_path, {"status": "running", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "pid": process.pid, "provider": provider_name, **muse_meta,
                                                  "started_at": started_wall, "soft_timeout_seconds": timeout, "termination_grace_seconds": termination_grace})
                        release_claim_lock()
                    progress_snapshot, _ = safe_refresh_progress(
                        result, output, stream_state, started_wall, started_monotonic)
                    if heartbeat_seconds:
                        safe_heartbeat(result, output, provider_name, provider, process, "running",
                                       started_wall, started_monotonic, progress_snapshot)
                    deadline = started_monotonic + timeout + termination_grace
                    next_heartbeat = started_monotonic + heartbeat_seconds if heartbeat_seconds else None
                    stream_poll_interval = (max(0.01, min(STREAM_POLL_SECONDS, heartbeat_seconds))
                                            if heartbeat_seconds else STREAM_POLL_SECONDS)
                    next_stream_poll = started_monotonic + stream_poll_interval
                    while True:
                        now_monotonic = time.monotonic()
                        remaining = deadline - now_monotonic
                        if remaining <= 0:
                            # A slow heartbeat/write can cross the boundary after the child exited.
                            if process.poll() is not None:
                                code = process.wait(timeout=0)
                                break
                            raise subprocess.TimeoutExpired(command, timeout + termination_grace)
                        wake_times = [deadline]
                        if next_heartbeat is not None:
                            wake_times.append(next_heartbeat)
                        if next_stream_poll is not None:
                            wake_times.append(next_stream_poll)
                        wait_for = max(0.01, min(wake_times) - now_monotonic)
                        try:
                            code = process.wait(timeout=wait_for)
                            break
                        except subprocess.TimeoutExpired:
                            now_monotonic = time.monotonic()
                            if next_stream_poll is not None and now_monotonic >= next_stream_poll:
                                progress_snapshot, new_events = safe_refresh_progress(
                                    result, output, stream_state, started_wall, started_monotonic)
                                if new_events:
                                    maybe_emit_progress(result, stream_state, progress_snapshot)
                                next_stream_poll = now_monotonic + stream_poll_interval
                            if next_heartbeat is not None and now_monotonic >= next_heartbeat:
                                safe_heartbeat(result, output, provider_name, provider, process, "running",
                                               started_wall, started_monotonic, progress_snapshot)
                                next_heartbeat = now_monotonic + heartbeat_seconds
                    result["_local_process_exited"] = True
                    progress_snapshot, new_events = safe_refresh_progress(
                        result, output, stream_state, started_wall, started_monotonic,
                        adapter_state="finished", final=True)
                    if new_events:
                        maybe_emit_progress(result, stream_state, progress_snapshot, force=True)
                except BaseException as exc:
                    if process is not None:
                        cleanup_error = None
                        cleanup_evidence = {"tree_termination_attempted": True, "tree_termination_outcome": "unknown", "tree_cessation_verified": False}
                        try:
                            cleanup_evidence = stop_process(process)
                        except BaseException as cleanup_exc:
                            cleanup_error = str(cleanup_exc)
                        if started_wall is not None:
                            result["started_at"] = dt.datetime.fromtimestamp(started_wall, UTC).isoformat().replace("+00:00", "Z")
                            result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
                        try:
                            alive_after_cleanup = process.poll() is None
                        except BaseException as poll_exc:
                            alive_after_cleanup = None
                            cleanup_error = cleanup_error or f"could not observe local process state: {poll_exc}"
                        result["cleanup"] = {**cleanup_evidence, "attempted": True, "error": cleanup_error,
                                             "process_alive_after_cleanup": alive_after_cleanup,
                                             "local_process_stopped": alive_after_cleanup is False,
                                             "direct_process_stopped": alive_after_cleanup is False}
                        if stream_state is not None and started_wall is not None:
                            adapter_state = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted"
                            progress_snapshot, new_events = safe_refresh_progress(
                                result, output, stream_state, started_wall, started_monotonic,
                                adapter_state=adapter_state, final=True)
                            if new_events:
                                maybe_emit_progress(result, stream_state, progress_snapshot, force=True)
                        if heartbeat_seconds and started_wall is not None:
                            safe_heartbeat(result, output, provider_name, provider, process,
                                           "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted",
                                           started_wall, started_monotonic, progress_snapshot)
                        if args.role == "implement" and isinstance(exc, (subprocess.TimeoutExpired, KeyboardInterrupt)):
                            try:
                                write_json(pending_path, {"status": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted",
                                                          "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "pid": process.pid,
                                                          "provider": provider_name, **muse_meta, "started_at": started_wall,
                                                          "duration_seconds": round(time.monotonic() - started_monotonic, 3) if started_monotonic else None,
                                                          "soft_timeout_seconds": timeout, "termination_grace_seconds": termination_grace,
                                                          "cleanup": result["cleanup"], "fallback_authorized": False,
                                                          "fallback_blocked_by_pending": True, "pending_path": str(pending_path)})
                            except Exception as pending_exc:
                                result["pending_write_error"] = str(pending_exc)
                    elif args.role == "implement" and isinstance(exc, OSError):
                        # Popen raised before returning a process: no writer was launched.
                        write_json(pending_path, {"status": "resolved", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "launch_error": str(exc), "provider": provider_name, **muse_meta})
                        result.pop("_claim_unresolved", None)
                    raise

            stdout_file = output / "stdout.log"
            if started_wall is not None:
                result["started_at"] = dt.datetime.fromtimestamp(started_wall, UTC).isoformat().replace("+00:00", "Z")
                result["finished_at"] = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
                result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
                if heartbeat_seconds:
                    safe_heartbeat(result, output, provider_name, provider, process, "finished",
                                   started_wall, started_monotonic, progress_snapshot)
            stderr_file = output / "stderr.log"
            stdout_text, stdout_truncated = read_text_tail(stdout_file)
            stderr_text, stderr_truncated = read_text_tail(stderr_file)
            stdout_text = _sanitize_all_secrets(stdout_text, secrets)
            stderr_text = _sanitize_all_secrets(stderr_text, secrets)

            is_terminal = False
            ds_output = None
            gemini_output = None
            claude_output = None
            muse_terminal = None
            muse_terminal_coherent = False
            gemini_result_event = False
            claude_result_event = False
            stream_errors = []
            provider_output_text = stdout_text
            if provider_name == "deepseek":
                ds_output, is_terminal, valid_success = parse_deepseek_final_output(
                    output, stdout_text, stdout_truncated=stdout_truncated)
                terminal_turn, terminal_event, has_terminal_402, _ = parse_codex_events(stdout_text)
            elif provider_name == "openai":
                codex_terminal, codex_seen, stream_errors = parse_openai_terminal(stdout_text, stdout_truncated)
                provider_output_text = json.dumps(codex_terminal) if codex_terminal else ""
                valid_success = bool(codex_terminal and codex_terminal.get("type") == "turn.completed" and code == 0)
                terminal_turn, has_terminal_402 = None, False
            elif provider_name == "gemini":
                gemini_output, gemini_result_event, stream_errors = parse_gemini_final_output(
                    stdout_text, stdout_truncated=stdout_truncated)
                provider_output_text = json.dumps(gemini_output) if gemini_output is not None else ""
                valid_success = successful_response(provider_output_text, "implement" if activity else args.role)
                terminal_turn, has_terminal_402 = None, False
            elif provider_name == "claude":
                claude_output, claude_result_event, stream_errors = parse_claude_final_output(stdout_text)
                provider_output_text = json.dumps(claude_output) if claude_output is not None else ""
                valid_success = successful_response(provider_output_text, "review")
                terminal_turn, has_terminal_402 = None, False
            elif provider_name == "muse":
                muse_terminal = stream_state.get("muse_terminal")
                if muse_terminal:
                    muse_terminal = {**muse_terminal,
                                     "text": _sanitize_all_secrets(muse_terminal["text"], secrets),
                                     "reason": _sanitize_all_secrets(muse_terminal["reason"], secrets)}
                muse_terminal_coherent = bool(muse_terminal and
                    ((muse_terminal.get("terminal") == "completed" and code == 0) or
                     (muse_terminal.get("terminal") in ("failed", "cancelled") and code != 0)))
                if muse_terminal_coherent:
                    if muse_terminal.get("terminal") == "completed":
                        provider_output_text = json.dumps({"status": "SUCCESS", "response": muse_terminal.get("text", "")})
                        valid_success = muse_terminal.get("response_nonempty") is True and code == 0
                    else:
                        provider_output_text = json.dumps({"status": "FAILED", "error": {
                            "message": muse_terminal.get("reason") or muse_terminal.get("text") or "Muse run failed"}})
                        valid_success = False
                else:
                    provider_output_text = ""
                    valid_success = False
                terminal_turn, has_terminal_402 = None, False
            else:
                valid_success = successful_response(stdout_text, args.role)
                terminal_turn, has_terminal_402 = None, False

            errors = error_objects(provider_output_text)
            if provider_name not in ("muse", "openai"):
                errors += error_objects(stderr_text)
            if provider_name in ("gemini", "claude", "openai") and (gemini_output is None if provider_name == "gemini" else claude_output is None if provider_name == "claude" else codex_terminal is None):
                errors.extend(error for error in stream_errors if isinstance(error, dict))
            # A truncated stderr suffix cannot prove that its remaining line
            # was the CLI's only diagnostic. Truncated stdout may hide a real
            # terminal event. Either case prevents stderr from establishing a
            # standalone terminal failure.
            plain_error = None if stderr_truncated or stdout_truncated else plain_terminal_error(stderr_text, code)
            if provider_name == "muse" and plain_error and plain_error.get("code") != "CLI_SETUP_ERROR":
                plain_error = None
            provider_without_terminal = (
                (provider_name == "openai" and codex_terminal is None) or
                (provider_name == "gemini" and gemini_output is None and not gemini_result_event) or
                (provider_name == "claude" and claude_output is None and not claude_result_event) or
                (provider_name == "muse" and not muse_terminal_coherent)
            )
            plain_error_applies = plain_error
            if provider_name == "muse" and stdout_text.strip():
                plain_error_applies = None
            if provider_name == "openai" and stdout_text.strip():
                plain_error_applies = None
            if provider_name in ("gemini", "claude", "muse", "openai") and stdout_text.strip() and not provider_without_terminal:
                # A malformed event=result is not repaired by unrelated stderr;
                # keep ownership unresolved instead of authorizing a fallback.
                plain_error_applies = None
            if plain_error_applies and (not stdout_text.strip() or provider_without_terminal):
                errors.append(plain_error_applies)
            raw_stdout_is_partial = provider_name == "deepseek" and stdout_truncated
            denied = [] if raw_stdout_is_partial else permission_denials(provider_output_text)
            complete_failure = False if raw_stdout_is_partial else complete_terminal_failure(provider_output_text)
            if (provider_name != "muse" and (not stdout_text.strip() or provider_without_terminal) and
                    not stderr_truncated and not stdout_truncated):
                complete_failure = complete_failure or complete_terminal_failure(stderr_text)
            terminal_established = (denied or valid_success or plain_error_applies is not None or
                                    (provider_name in ("gemini", "claude") and complete_failure) or
                                    (provider_name == "openai" and codex_terminal is not None) or
                                    (provider_name == "muse" and muse_terminal_coherent) or
                                    (provider_name == "deepseek" and (is_terminal or terminal_turn == "failed")))
            result["terminal_established"] = bool(terminal_established)
            if provider_name == "openai":
                result["stream_terminal_seen"] = codex_seen
            elif provider_name == "muse":
                result["stream_terminal_seen"] = muse_terminal is not None
            elif provider_name in ("gemini", "claude"):
                result["stream_terminal_seen"] = gemini_result_event if provider_name == "gemini" else claude_result_event
            elif provider_name == "deepseek":
                # turn.completed alone does not validate the required final
                # schema in last_message.txt, so it cannot claim a usable
                # terminal result.
                result["stream_terminal_seen"] = is_terminal or terminal_turn == "failed"
            if args.role == "implement":
                pending_data = {"status": "resolved" if terminal_established else "uncertain_exit", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "exit_code": code, "provider": provider_name, **muse_meta,
                                "started_at": started_wall, "finished_at": time.time(), "duration_seconds": result.get("duration_seconds"),
                                "soft_timeout_seconds": timeout, "termination_grace_seconds": termination_grace}
                if not terminal_established:
                    pending_data.update(fallback_authorized=False, fallback_blocked_by_pending=True, pending_path=str(pending_path))
                    result.update(pending_path=str(pending_path), fallback_authorized=False, fallback_blocked_by_pending=True)
                write_json(pending_path, pending_data)
                if terminal_established:
                    result.pop("_claim_unresolved", None)
            if denied:
                result.update(status="permission_denied", exit_code=code, denied_actions=denied)
                if args.role == "implement":
                    result["fallback"] = luna_fallback()
                    result["error"] = "Provider denied required tools or file access. Task is incomplete; retry with native Luna fallback."
                else:
                    result["error"] = "Provider denied required tools or file access. Task is incomplete; no automatic permission bypass."
                return finish(result, output, workspace, 1, secrets=secrets)

            if provider_name == "deepseek":
                if valid_success and code == 0 and terminal_turn != "failed":
                    result.update(status="completed", exit_code=code)
                    refresh_balance_safe(state_dir, config)
                    return finish(result, output, workspace, 0, secrets=secrets)

                # Only check for runtime 402/insufficient balance when there is no terminal success
                insufficient_balance = None
                if has_terminal_402:
                    insufficient_balance = {"code": "INSUFFICIENT_BALANCE", "message": "HTTP 402: Insufficient balance", "source": "codex_event"}
                else:
                    # Balance depletion must come from terminal evidence. An
                    # intermediate error event may be followed by a different
                    # terminal failure and must not permanently poison routing.
                    terminal_balance_errors = []
                    if terminal_turn == "failed" and isinstance(terminal_event, dict):
                        terminal_error = terminal_event.get("error")
                        if isinstance(terminal_error, dict):
                            terminal_balance_errors.append(terminal_error)
                        elif isinstance(terminal_error, str):
                            terminal_balance_errors.append({"message": terminal_error})
                    if is_terminal and isinstance(ds_output, dict):
                        terminal_balance_errors.extend(error_objects(json.dumps(ds_output)))
                    insufficient_balance = deepseek_insufficient_balance_error(terminal_balance_errors)

                if insufficient_balance and terminal_established:
                    record_balance_snapshot(state_dir, {"is_available": False, "balance_infos": []},
                                            error=insufficient_balance.get("message", "HTTP 402: Insufficient balance"), config=config)
                    result.update(status="fallback_required", fallback=luna_fallback(),
                                  error=insufficient_balance.get("message", "HTTP 402: Insufficient balance"), exit_code=code)
                    return finish(result, output, workspace, 20, secrets=secrets)

                result.update(status="uncertain_exit" if not terminal_established else "provider_error", exit_code=code if code != 0 else 1)
                if terminal_established:
                    result["fallback"] = luna_fallback()
                if errors and not is_terminal:
                    result["error"] = (structured_error_message(errors)
                                       or "DeepSeek provider reported an error without details.")
                elif not errors and not is_terminal:
                    result["error"] = "No structured terminal result from DeepSeek; output missing or malformed."
                elif is_terminal and not valid_success:
                    result["error"] = (ds_output.get("response")
                                       if ds_output and ds_output.get("response")
                                       else f"DeepSeek returned terminal non-success status: {ds_output.get('status') if ds_output else 'UNKNOWN'}")
                return finish(result, output, workspace, 1, secrets=secrets)

            quota = (muse_quota_error(muse_terminal) if provider_name == "muse" and muse_terminal_coherent else
                     quota_error(stream_errors if provider_name == "openai" else errors, provider_name)
                     if terminal_established and not valid_success else None)
            if quota:
                with provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)), "quota.lock"):
                    state = quota_record(provider_name, quota, state_dir, output, config,
                                         model=provider["model"],
                                         muse_profile=muse_meta.get("muse_profile"),
                                         effort=muse_meta.get("effort"))
                result.update(status="fallback_required", fallback=quota_fallback(provider_name, config), quota=state, cached=False)
                return finish(result, output, workspace, 20, secrets=secrets)
            result.update(status="completed" if code == 0 and not errors and valid_success else "provider_error", exit_code=code)
            if result["status"] == "provider_error" and errors:
                result["error"] = structured_error_message(errors)
                result["error_codes"] = [str(error.get("code", error.get("status"))) for error in errors if error.get("code", error.get("status")) is not None]
            if args.role == "implement" and not valid_success:
                if not terminal_established:
                    result.update(status="uncertain_exit", fallback_authorized=False, fallback_blocked_by_pending=True)
                else:
                    result["fallback"] = luna_fallback()
                if not errors:
                    result["error"] = "No structured terminal result; backend completion uncertain. Pending state blocks new implementation until confirmed stopped."
            elif args.role == "review" and not valid_success:
                if not terminal_established:
                    result.update(status="uncertain_exit")
                    if not errors:
                        result["error"] = "No structured terminal result; backend completion uncertain."
            return finish(result, output, workspace, 0 if result["status"] == "completed" else 1, secrets=secrets)
    except subprocess.TimeoutExpired:
        result["finished_at"] = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
        cleanup = result.get("cleanup", {})
        local_stopped = cleanup.get("local_process_stopped") is True
        termination = "Local CLI termination was attempted and observed stopped; provider backend completion remains unverified." if local_stopped else "Local CLI termination was attempted, but local cessation is unconfirmed; provider backend completion remains unverified."
        if args.role == "implement":
            result.update(status="timeout", fallback=luna_fallback(), fallback_authorized=False, fallback_blocked_by_pending=True,
                          error=termination + " Confirm provider session stopped before resolving pending state or continuing with Luna fallback.")
        else:
            result.update(status="timeout",
                          error=termination + " Confirm provider session stopped.")
    except (OSError, ValueError, TimeoutError) as exc:
        result.update(status="setup_error", error=str(exc))
        if args.role == "implement":
            result["fallback"] = luna_fallback()
    except KeyboardInterrupt:
        result["finished_at"] = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
        cleanup = result.get("cleanup", {})
        local_stopped = cleanup.get("local_process_stopped") is True
        termination = "Local CLI termination was attempted and observed stopped; provider backend completion remains unverified." if local_stopped else "Local CLI termination was attempted, but local cessation is unconfirmed; provider backend completion remains unverified."
        if args.role == "implement":
            result.update(status="cancelled", fallback=luna_fallback(), fallback_authorized=False, fallback_blocked_by_pending=True,
                          error=termination + " Confirm provider session stopped before resolving pending state or continuing with Luna fallback.")
        else:
            result.update(status="cancelled",
                          error=termination + " Confirm provider session stopped.")
    return finish(result, output, workspace, 1, secrets=secrets)


def finish(result, output, workspace, code, secrets=None):
    orchestration = result.pop("_orchestration", None)
    # Cleanup is only recorded after Popen returned a process. An interrupt in
    # the tiny interval before _process_launched was recorded is still launched.
    process_launched = result.pop("_process_launched", False) or "cleanup" in result
    local_process_exited = result.pop("_local_process_exited", False)
    if orchestration:
        parent_path = Path(orchestration["path"])
        state_dir = Path(orchestration["state_dir"])
        try:
            with provider_lock(state_dir, 30, "orchestration.lock"):
                children = orchestration_children(parent_path)
                unresolved = any(child.get("status") not in ("completed", "failed") for child in children)
                failed = any(child.get("status") == "failed" for child in children)
                if unresolved:
                    result.update(status="blocked_pending_run", fallback_authorized=False,
                                  fallback_blocked_by_pending=True,
                                  error="Orchestrator has unresolved child dispatches")
                    code = 1
                elif failed and result.get("status") == "completed":
                    result.update(status="provider_error", error="Orchestrator child dispatch failed")
                    code = 1
                elif not children and result.get("status") == "completed":
                    result.update(status="provider_error", error="Orchestrator completed without a child dispatch")
                    code = 1
                parent = read_json(parent_path)
                cleanup = result.get("cleanup", {})
                local_stopped = (local_process_exited or cleanup.get("local_process_stopped") is True or
                                 not process_launched)
                terminal = result.get("terminal_established") is True
                safe_terminal = (result.get("status") in ("completed", "provider_error", "permission_denied", "fallback_required")
                                 and terminal and local_stopped)
                safe_prelaunch = not process_launched and not children
                parent["status"] = "resolved" if not unresolved and (safe_terminal or safe_prelaunch) else "uncertain_exit"
                parent["result_status"] = result.get("status")
                parent["local_process_stopped"] = local_stopped
                parent["terminal_established"] = terminal
                write_json(parent_path, parent)
                result["orchestration_run_id"] = parent.get("run_id")
                result["orchestration_pending_path"] = str(parent_path)
                if parent["status"] != "resolved":
                    result["fallback_authorized"] = False
                    result["fallback_blocked_by_pending"] = True
        except (OSError, ValueError, TimeoutError) as exc:
            result.update(status="blocked_pending_run", fallback_authorized=False,
                          fallback_blocked_by_pending=True,
                          error=f"Orchestration child state could not be verified: {exc}")
            code = 1
    if result.pop("_claim_unresolved", False) or result.get("status") == "blocked_pending_run":
        result["fallback_authorized"] = False
        result["fallback_blocked_by_pending"] = True
    artifact_errors = []
    try:
        result["evidence"] = git_evidence(workspace, output, secrets)
    except Exception as exc:
        artifact_errors.append("git evidence: " + str(exc))
    try:
        for error in sanitize_run_artifacts(output, secrets):
            artifact_errors.append("artifact sanitization: " + error)
    except Exception as exc:
        artifact_errors.append("artifact sanitization: " + str(exc))
    if secrets:
        def clean(value):
            if isinstance(value, str):
                return _sanitize_all_secrets(value, secrets)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        result = clean(result)
    if artifact_errors:
        result["artifact_errors"] = artifact_errors
    try:
        write_json(output / "result.json", result)
    except Exception as exc:
        result.setdefault("artifact_errors", []).append("result persistence: " + str(exc))
    return result, code


def cli_quota_status(args):
    config = read_json(args.config)
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("Routing configuration must contain a providers object")
    muse_profile_arg = getattr(args, "muse_profile", None)
    require_muse_profile_allowed(muse_profile_arg, getattr(args, "provider", None))
    providers = [args.provider] if args.provider else [p for p in ("gemini", "deepseek", "claude", "muse", "openai") if p in config["providers"]]
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    output = {}
    code = 0
    check_live = getattr(args, "check_live", False)

    for provider in providers:
        if not isinstance(config["providers"].get(provider), dict):
            output[provider] = {"status": "state_error", "available_to_try": 0,
                                "error": "Provider is missing from config"}
            code = 1
            continue

        if provider == "deepseek":
            item = {
                "provider": "deepseek",
                "model": config["providers"]["deepseek"].get("model"),
                "balance_path": str(balance_path(state_dir)),
            }
            if config["providers"]["deepseek"].get("enabled", True) is False:
                item.update(
                    status="disabled",
                    available_to_try=0,
                    fallback=luna_fallback(),
                    error="DeepSeek provider is disabled in configuration (enabled=false)",
                )
                output[provider] = item
                continue

            if check_live:
                item["live"] = True
                item["cached"] = False
                try:
                    data, err = query_deepseek_balance(config)
                    snapshot = record_balance_snapshot(state_dir, data, err, config)
                except Exception as exc:
                    data, err = None, BalanceError(f"Balance check exception: {exc}", kind="unverified", code="CHECK_EXCEPTION")
                    snapshot = record_balance_snapshot(state_dir, None, err, config)

                item["observed_at"] = snapshot.get("observed_at")
                if data:
                    item["is_available"] = data.get("is_available")
                    item["balance_infos"] = data.get("balance_infos")
                    item["total_balance"] = snapshot.get("total_balance")
                if err:
                    item["error"] = str(err)
                    item["available_to_try"] = 0
                    item["fallback"] = luna_fallback()
                    kind, _ = classify_balance_error(err)
                    if kind == "depleted":
                        item["status"] = "fallback_required"
                        if code == 0:
                            code = 20
                    else:
                        item["status"] = "balance_check_failed"
                        code = 1
                else:
                    item["status"] = "available_to_try"
                    item["available_to_try"] = 1
            else:
                item["live"] = False
                snapshot, state_err = balance_state(state_dir)
                if state_err:
                    item.update(cached=False, status="state_error", available_to_try=0, fallback=luna_fallback(), error="Malformed or unreadable balance snapshot: " + state_err)
                    code = 1
                elif snapshot:
                    item["cached"] = True
                    item["observed_at"] = snapshot.get("observed_at")
                    item["is_available"] = snapshot.get("is_available")
                    item["balance_infos"] = snapshot.get("balance_infos", [])
                    item["total_balance"] = snapshot.get("total_balance")
                    item["last_check_error"] = snapshot.get("last_check_error") or snapshot.get("error")
                    item["last_check_error_at"] = snapshot.get("last_check_error_at")
                    item["status"] = "available_to_try"
                    item["available_to_try"] = 1
                    item["live_check_required"] = True
                    item["note"] = "Cached snapshot is reporting evidence only; live check is required and performed on execute (or pass --check-live)."
                else:
                    item.update(cached=False, status="available_to_try", available_to_try=1, observed_at=None, is_available=None, balance_infos=[])
                    item["live_check_required"] = True
                    item["note"] = "No cached snapshot; live check is required and performed on execute (or pass --check-live)."
            output[provider] = item
            continue

        muse_meta = {}
        if provider == "muse":
            try:
                _, muse_meta = resolve_muse_profile(config, muse_profile_arg)
            except ValueError as exc:
                output[provider] = {"provider": provider, "status": "state_error", "available_to_try": 0,
                                    "fallback": quota_fallback(provider, config), "error": str(exc)}
                code = 1
                continue
        state, error = quota_state(state_dir, provider)
        item = {"provider": provider, "model": muse_meta.get("model", config["providers"][provider].get("model")),
                **muse_meta,
                "quota_path": str(quota_path(state_dir, provider)), "available_to_try": 1}
        if error:
            fallback = quota_fallback(provider, config)
            item.update(status="state_error", available_to_try=0, fallback=fallback, error="Malformed or unreadable quota state: " + error)
            code = 1
        elif state:
            item["cached_evidence"] = state
            if state.get("retry_at", 0) > time.time():
                item.update(status="fallback_required", available_to_try=0, fallback=quota_fallback(provider, config),
                            observed_at=state.get("observed_at"), retry_at=state.get("retry_at"),
                            retry_at_iso=state.get("retry_at_iso"), reset_at=state.get("reset_at"))
                if code == 0:
                    code = 20
            else:
                item.update(status="available_to_try", observed_at=state.get("observed_at"),
                            retry_at=state.get("retry_at"), retry_at_iso=state.get("retry_at_iso"), reset_at=state.get("reset_at"))
        else:
            item.update(status="available_to_try", observed_at=None, retry_at=None, retry_at_iso=None, reset_at=None)
        output[provider] = item

    if args.provider:
        return output[args.provider], code
    return {"providers": output}, code


def cli_quota_set(args):
    config = read_json(args.config)
    quota_cooldown(config)
    provider = args.provider
    if provider not in ("gemini", "claude", "muse", "openai"):
        raise ValueError(f"quota-set only supports subscription quota providers; '{provider}' uses monetary balance preflight")
    if provider not in config.get("providers", {}):
        raise ValueError("Provider is missing from config")
    muse_profile_arg = getattr(args, "muse_profile", None)
    require_muse_profile_allowed(muse_profile_arg, provider)
    muse_meta = {}
    if provider == "muse":
        _, muse_meta = resolve_muse_profile(config, muse_profile_arg)
    reason = args.reason.strip() if isinstance(args.reason, str) else ""
    if not reason:
        raise ValueError("reason must be non-empty")
    secret_values = [value.strip() for _, value in framework_secrets(config)]
    error = {"code": "QUOTA_EXHAUSTED", "message": _sanitize_all_secrets(reason, secret_values), "source": "operator"}
    if args.reset_at:
        try:
            parsed = dt.datetime.fromisoformat(args.reset_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reset-at must be timezone-qualified ISO-8601") from exc
        if not parsed.tzinfo or parsed.timestamp() <= time.time():
            raise ValueError("reset-at must be a future timezone-qualified timestamp")
        error["reset_at"] = parsed.isoformat()
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    with provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)), "quota.lock"):
        state = quota_record(provider, error, state_dir, "operator", config,
                             model=muse_meta.get("model"),
                             muse_profile=muse_meta.get("muse_profile"),
                             effort=muse_meta.get("effort"))
    return {"status": "recorded", "provider": provider, **muse_meta, "quota": state, "quota_path": str(quota_path(state_dir, provider))}, 0


def activity_status(args):
    if getattr(args, "check_live", False) or getattr(args, "muse_profile", None):
        raise ValueError("Activity status is local only and does not accept --check-live/--muse-profile")
    if getattr(args, "profile", None) and getattr(args, "provider", None):
        raise ValueError("Choose one of --profile or --provider")
    config = read_json(args.config)
    chain = activity_routes(config, args.activity)
    if getattr(args, "profile", None):
        if args.profile not in chain:
            raise ValueError("Requested profile is not in the activity chain")
        chain = [args.profile]
    if getattr(args, "provider", None):
        chain = [name for name in chain if config["profiles"][name]["provider"] == args.provider]
    if not chain:
        raise ValueError("No profiles match the requested activity/provider")
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    blocked = set()
    entries = []
    for name in chain:
        provider_name, effective = effective_activity_provider(config, name)
        state, error = quota_state(state_dir, provider_name)
        status = ("state_error" if error else "disabled" if config["providers"][provider_name].get("enabled", True) is False
                  else "fallback_required" if state and state.get("retry_at", 0) > time.time() else "available_to_try")
        if status != "available_to_try":
            blocked.add(provider_name)
        entries.append({"profile": name, "provider": provider_name, "model": effective["model"],
                        "effort": effective["effort"], "status": status, "quota_path": str(quota_path(state_dir, provider_name)),
                        **({"error": error} if error else {})})
    return {"activity": args.activity, "routes": entries, "available_to_try": int(any(x["status"] == "available_to_try" for x in entries))}, 0


def _dispatch_activity_core(args):
    config = read_json(args.config)
    if not args.activity:
        raise ValueError("dispatch requires --activity")
    if getattr(args, "muse_profile", None) or getattr(args, "review_effort", None):
        raise ValueError("Legacy --muse-profile/--review-effort cannot be combined with activity dispatch")
    if args.activity not in ("independent_review", "security_review", "deep_review") and args.review_reason:
        raise ValueError("--review-reason is only for review activities")
    chain = activity_routes(config, args.activity)
    if args.profile and args.provider:
        raise ValueError("Choose one of --profile or --provider")
    if args.profile:
        if args.profile not in chain:
            raise ValueError("Requested profile is not in activity chain")
        chain = [args.profile]
    if args.provider:
        chain = [p for p in chain if config["profiles"][p]["provider"] == args.provider]
    if not chain:
        raise ValueError("No eligible route for selection")
    if args.activity in ("security_review", "deep_review") and not (args.review_reason and args.review_reason.strip()):
        raise ValueError("High/deep review requires --review-reason")
    if args.activity in WRITE_ACTIVITIES | {"orchestration"}:
        task = read_json(args.task_file)
        if not task.get("owned_paths"):
            raise ValueError("Writing activity or orchestration requires explicit owned_paths")
    if args.activity == "orchestration" and not read_json(args.task_file).get("allow_delegation"):
        raise ValueError("Orchestration task must explicitly authorize child dispatch")
    author = read_json(args.task_file).get("review", {}).get("author_provider") if args.activity in ("independent_review", "security_review", "deep_review") else None
    if author in PROVIDERS and len({config["profiles"][name]["provider"] for name in chain}) > 1:
        chain = [name for name in chain if config["profiles"][name]["provider"] != author] + [name for name in chain if config["profiles"][name]["provider"] == author]
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    attempts = []
    blocked = set()
    for name in chain:
        provider_name, effective = effective_activity_provider(config, name)
        if (args.activity in ("independent_review", "security_review", "deep_review") and
                effective.get("effort") in ("high", "xhigh", "max", "ultra") and
                not (args.review_reason and args.review_reason.strip())):
            attempts.append({"profile": name, "provider": provider_name, "status": "review_reason_required"})
            continue
        if provider_name in blocked:
            attempts.append({"profile": name, "provider": provider_name, "status": "skipped_account_block"})
            continue
        state, error = quota_state(state_dir, provider_name)
        if error or (state and state.get("retry_at", 0) > time.time()) or config["providers"][provider_name].get("enabled", True) is False:
            blocked.add(provider_name)
            attempts.append({"profile": name, "provider": provider_name,
                             "status": "state_error" if error else "fallback_required" if state else "disabled",
                             **({"error": error} if error else {})})
            continue
        route_args = copy.copy(args)
        route_args.role = "implement" if args.activity in WRITE_ACTIVITIES else "review"
        route_args.activity = args.activity
        route_args.profile = name
        route_args.provider = provider_name
        route_args.prior_context = attempts[-3:]
        route_args.muse_profile = None
        try:
            result, code = execute(route_args)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # execute's prelaunch validation cannot leave a writer behind.
            result, code = {"status": "setup_error", "error": str(exc)}, 1
        attempts.append({"profile": name, "provider": provider_name, "model": effective["model"],
                         "status": result.get("status"), "logs": result.get("logs"),
                         "error": result.get("error"), "evidence": result.get("evidence"),
                         "pending_path": result.get("pending_path")})
        if result.get("status") == "completed":
            return {"status": "completed", "activity": args.activity, "selected": name,
                    "same_provider_review": bool(author == provider_name), "attempts": attempts, "result": result}, 0
        if args.dry_run:
            return {"status": "dry_run", "activity": args.activity, "selected": name, "attempts": attempts, "result": result}, 0
        if result.get("status") == "fallback_required":
            blocked.add(provider_name)
        if result.get("status") in ("provider_error", "setup_error") and account_auth_error(result):
            blocked.add(provider_name)
        if (result.get("fallback_blocked_by_pending") or result.get("fallback_authorized") is False or
                result.get("status") in ("uncertain_exit", "timeout", "cancelled", "blocked_pending_run") or
                result.get("status") == "provider_error" and not result.get("terminal_established")):
            return {"status": "blocked_pending_run", "activity": args.activity, "attempts": attempts,
                    "error": "Previous route is not authoritatively resolved; inspect pending state before another writer."}, 1
        if result.get("status") not in ("fallback_required", "provider_error", "permission_denied", "setup_error", "state_error"):
            return {"status": "blocked", "activity": args.activity, "attempts": attempts}, 1
    return {"status": "all_routes_unavailable", "activity": args.activity, "attempts": attempts,
            "error": "No eligible route completed; inspect route logs and account state."}, 20


def dispatch_activity(args):
    run_id = getattr(args, "parent_run_id", None) or os.environ.get("FRAMEWORK_PARENT_RUN_ID")
    if not run_id or args.dry_run:
        return _dispatch_activity_core(args)
    workspace = Path(args.workspace).resolve(strict=True)
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state")).resolve()
    owned_paths = normalized_owned_paths(workspace, read_json(args.task_file).get("owned_paths"))
    child_path = register_child_dispatch(state_dir, workspace, run_id, owned_paths, args.activity)
    args = copy.copy(args)
    args._registered_child_path = str(child_path)
    try:
        result, code = _dispatch_activity_core(args)
    except BaseException:
        resolve_child_dispatch(state_dir, child_path, {"status": "unresolved"})
        raise
    resolve_child_dispatch(state_dir, child_path, result)
    return result, code


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "resolve-orchestration":
        parser = argparse.ArgumentParser(description="Confirm a stopped orchestrator or child after inspecting its evidence")
        parser.add_argument("--state-dir", required=True)
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--run-id", required=True)
        parser.add_argument("--child-id")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--confirm-stopped", action="store_true", required=True)
        try:
            args = parser.parse_args(sys.argv[2:])
            result = operator_resolve_orchestration(Path(args.state_dir).resolve(),
                                                    Path(args.workspace).resolve(strict=True),
                                                    args.run_id, args.reason, args.child_id)
            code = 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result, code = {"status": "state_error", "error": str(exc)}, 1
        print(json.dumps(result, indent=2))
        return code
    if len(sys.argv) > 1 and sys.argv[1] in ("status", "quota-set"):
        command = sys.argv[1]
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", required=True)
        parser.add_argument("--state-dir")
        if command == "status":
            parser.add_argument("--provider", choices=("gemini", "deepseek", "claude", "muse", "openai"))
            parser.add_argument("--activity")
            parser.add_argument("--profile")
            parser.add_argument("--muse-profile", choices=MUSE_PROFILE_NAMES, default=None)
            parser.add_argument("--check-live", "--live", dest="check_live", action="store_true")
        if command == "quota-set":
            parser.add_argument("--provider", choices=("gemini", "claude", "muse", "openai"), required=True)
            parser.add_argument("--muse-profile", choices=MUSE_PROFILE_NAMES, default=None)
            parser.add_argument("--reason", required=True)
            parser.add_argument("--reset-at")
        args = argparse.Namespace(provider=None)
        try:
            args = parser.parse_args(sys.argv[2:])
            result, code = (activity_status(args) if args.activity else cli_quota_status(args)) if command == "status" else cli_quota_set(args)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {"status": "state_error", "available_to_try": 0, "error": str(exc)}
            if getattr(args, "provider", None):
                result["fallback"] = luna_fallback() if args.provider in ("gemini", "deepseek", "muse") else {"model": "gpt-6-astra", "effort": "low"}
            code = 1
        print(json.dumps(result, indent=2))
        return code
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("implement", "review", "dispatch"))
    parser.add_argument("--provider", choices=("gemini", "deepseek", "claude", "muse", "openai"), default=None)
    parser.add_argument("--activity")
    parser.add_argument("--profile")
    parser.add_argument("--parent-run-id")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--muse-profile", choices=MUSE_PROFILE_NAMES, default=None)
    parser.add_argument("--review-effort", choices=("medium", "high"), default=None)
    parser.add_argument("--review-reason", default=None)
    args = parser.parse_args()
    try:
        if args.role != "dispatch" and (args.activity or args.profile or args.parent_run_id):
            raise ValueError("--activity, --profile and --parent-run-id require dispatch; legacy implement/review remain pinned")
        result, code = dispatch_activity(args) if args.role == "dispatch" else execute(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {"status": "setup_error", "error": str(exc)}, 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
