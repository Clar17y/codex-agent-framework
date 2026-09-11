"""Bounded external provider execution with shared Gemini/Claude quota preflight."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

UTC = dt.timezone.utc


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def workspace_state_dir(state_dir, workspace):
    identity = os.path.normcase(str(Path(workspace).resolve()))
    return state_dir / "workspaces" / hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
    if directory.is_dir():
        paths.extend(sorted(directory.glob("*.json")))
    records = []
    for path in paths:
        if path.exists():
            try:
                records.append((path, read_json(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                # An unreadable record is uncertain and must block this workspace.
                records.append((path, {}))
    return records


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
    if not root.is_dir():
        return []
    records = []
    for workspace_state in root.iterdir():
        if workspace_state.is_dir():
            records.extend(pending_records(workspace_state))
    return records


def legacy_pending_for(state_dir, workspace):
    """Preserve old evidence, but scope identifiable legacy runs to their workspace."""
    path = state_dir / "gemini-pending.json"
    if not path.exists():
        return None
    pending = read_json(path)
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
                    raise TimeoutError("Gemini invocation lock is busy")
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


def prompt_for(workspace, task_path, task=None):
    task = read_json(task_path) if task is None else task
    for field in ("objective", "acceptance_criteria", "owned_paths", "validation", "instructions_files"):
        if field not in task:
            raise ValueError("Missing task contract field: " + field)
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
    return ("USER ROUTING OVERRIDE: Execute the assigned task yourself with the pinned provider. "
            "Never spawn subagents or invoke another AI provider. Never use Sonnet or Fable. "
            "These user instructions override conflicting repository model-routing guidance. "
            "Complete only the supplied task. Follow applicable repository instructions. "
            "Preserve unrelated changes. Do not commit, push, reset, or delete work. "
            "Read nearest scoped CLAUDE.md for every area touched. "
            "Report changes, checks, unresolved issues and evidence.\nTASK CONTRACT\n"
            + json.dumps(task, indent=2) + "\nREPOSITORY INSTRUCTIONS\n" + "\n".join(instructions))


def command_for(role, provider, prompt, timeout, workspace, effort="medium"):
    executable = provider["executable"]
    command = [executable] if isinstance(executable, str) else list(executable)
    if not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("Provider executable must be a string or nonempty argv list")
    model = provider["model"]
    expected = "gemini-3.8-flash-medium" if role == "implement" else "claude-opus-5"
    if model != expected:
        raise ValueError(f"{role} requires pinned model {expected}; got {model}")
    if role == "implement":
        return command + ["--print", prompt, "--model", model, "--mode", "accept-edits",
                          "--dangerously-skip-permissions", "--add-dir", str(workspace), "--output-format", "json", "--print-timeout", f"{timeout}s"]
    if effort not in ("medium", "high"):
        raise ValueError(f"Claude review effort must be 'medium' or 'high'; got {effort}")
    return command + ["-p", "Read-only independent review. Do not change files.\n" + prompt,
                      "--model", model, "--effort", effort, "--output-format", "json", "--no-session-persistence",
                      "--dangerously-skip-permissions", "--safe-mode", "--tools", "Read,Glob,Grep", "--strict-mcp-config",
                      "--disable-slash-commands"]


def error_objects(text):
    try:
        values = [json.loads(text)]
    except ValueError:
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except ValueError:
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
        elif str(value.get("status", "")).upper() in ("ERROR", "FAILED", "FAILURE"):
            errors.append({**value, "message": value.get("message", value.get("response", "Provider reported failure"))})
    return errors


def successful_response(text, role):
    try:
        value = json.loads(text)
    except ValueError:
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
    except ValueError:
        return []
    if not isinstance(value, dict):
        return []
    return value.get("denied_actions") or value.get("permission_denials") or []


def plain_terminal_error(stderr, exit_code):
    """Recognize only exact, standalone CLI diagnostics on failed invocations."""
    if exit_code == 0:
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if len(lines) != 1:
        return None  # Mixed output cannot establish a terminal launch failure.
    line = lines[0]
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
        if provider == "claude" and "sonnet" in message and "opus" not in message:
            continue
        if code in ("QUOTA_EXHAUSTED", "INSUFFICIENT_QUOTA") or terminal_quota_message(raw_message, provider):
            found = dict(error)
            found["message"] = raw_message
            return found
    return None


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


def quota_fallback(provider):
    return {"model": "gpt-5.6-luna", "effort": "medium"} if provider == "gemini" else {"model": "gpt-6-astra", "effort": "low"}


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


def quota_record(provider, error, state_dir, logs, config):
    observed = time.time()
    retry, reason = retry_at(error, quota_cooldown(config), observed)
    reset = retry if reason == "provider_reset" else None
    state = {"version": 1, "provider": provider, "model": config["providers"][provider].get("model"),
             "observed_at": dt.datetime.fromtimestamp(observed, UTC).isoformat().replace("+00:00", "Z"),
             "retry_at": retry, "retry_at_iso": dt.datetime.fromtimestamp(retry, UTC).isoformat().replace("+00:00", "Z"),
             "reset_at": dt.datetime.fromtimestamp(reset, UTC).isoformat().replace("+00:00", "Z") if reset else None,
             "reason": reason, "provenance": "operator_report" if logs == "operator" else ("provider_reset" if reason == "provider_reset" else "probe_cooldown"),
             "source_logs": str(logs), "confirmed_error": error, "error": error}
    existing, state_error = quota_state(state_dir, provider)
    if state_error:
        raise ValueError("Refusing to overwrite malformed quota state: " + state_error)
    if existing and existing.get("retry_at", 0) > observed and existing.get("retry_at", 0) > retry:
        return existing
    write_json(quota_path(state_dir, provider), state)
    return state


def stop_process(process):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=30, check=False)
        finally:
            if process.poll() is None:
                process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def git_evidence(workspace, output):
    paths = {}
    for name, args in (("status", ["status", "--short"]), ("diff", ["diff", "HEAD", "--"]),
                       ("head", ["rev-parse", "HEAD"])):
        path = output / (name + ".txt")
        try:
            run = subprocess.run(["git", "-C", str(workspace)] + args,
                                 capture_output=True, timeout=30, check=False)
            path.write_bytes(run.stdout + run.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            path.write_text(str(exc), encoding="utf-8")
        paths[name] = str(path)
    return paths


def execute(args):
    workspace = Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    config = read_json(args.config)
    quota_cooldown(config)
    provider_name = "gemini" if args.role == "implement" else "claude"
    provider = config["providers"][provider_name]
    timeout = float(config.get("timeout_seconds", 1800))
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    review_effort_arg = getattr(args, "review_effort", None)
    review_reason_arg = getattr(args, "review_reason", None)
    if args.role == "implement":
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
    owned_paths = normalized_owned_paths(workspace, task.get("owned_paths"))
    owned_claims = [os.path.normcase(str((workspace / path).resolve())) for path in owned_paths]
    prompt = prompt_for(workspace, args.task_file, task)
    command = command_for(args.role, provider, prompt, timeout, workspace, review_effort or "medium")
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    if args.dry_run:
        dry_run_meta = {"status": "dry_run", "provider": provider_name, "model": provider["model"],
                        "cwd": str(workspace), "command": command, "state_dir": str(state_dir)}
        if args.role == "review":
            dry_run_meta["review_effort"] = review_effort
            dry_run_meta["review_reason"] = review_reason
        return dry_run_meta, 0
    output = workspace / ".llm-output" / "agent-framework" / str(uuid.uuid4())
    output.mkdir(parents=True)
    # Avoid Windows' command-line length limit; providers have a Read tool.
    prompt_path = output / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = command_for(args.role, provider,
                          f"Read the complete task contract and instructions in {prompt_path} and follow them. "
                          "Execute the task yourself; never spawn subagents or use another AI provider.", timeout, workspace, review_effort or "medium")
    result = {"provider": provider_name, "model": provider["model"], "logs": str(output)}
    if args.role == "review":
        result["review_effort"] = review_effort
        result["review_reason"] = review_reason
    workspace_state = workspace_state_dir(state_dir, workspace)
    # This lock covers only the check-and-claim transaction.  Provider processes run
    # concurrently once their disjoint ownership claims have been persisted.
    lock = provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)),
                         "gemini.lock" if args.role == "implement" else "quota.lock")
    try:
        with lock as release_claim_lock:
            state_path = quota_path(state_dir, provider_name)
            pending_path = workspace_state / "gemini-pending" / (str(uuid.uuid4()) + ".json")
            if args.role == "implement":
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                result["pending_path"] = str(pending_path)
                legacy = legacy_pending_for(state_dir, workspace)
                if legacy is not None:
                    result.update(status="blocked_pending_run", pending=legacy,
                                  pending_path=str(state_dir / "gemini-pending.json"),
                                  error="Unresolved legacy run belongs to this workspace or has unknown ownership. Confirm it stopped before resolving its pending record.")
                    release_claim_lock()
                    return finish(result, output, workspace, 1)
            if args.role == "implement":
                for existing_path, pending in all_pending_records(state_dir):
                    if not isinstance(pending, dict) or pending.get("status") != "resolved":
                        pending_workspace, pending_owned_paths = pending_claims(pending)
                        if pending_workspace is None or claims_overlap(workspace, owned_claims, pending_workspace, pending_owned_paths):
                            result.update(status="blocked_pending_run", pending=pending,
                                          pending_path=str(existing_path),
                                          error="An unresolved provider run owns overlapping paths. Confirm it stopped, then mark its pending record resolved before retrying.")
                            release_claim_lock()
                            return finish(result, output, workspace, 1)
            state, state_error = quota_state(state_dir, provider_name)
            if state_error:
                result.update(status="state_error", fallback=quota_fallback(provider_name), quota_path=str(state_path), error="Malformed or unreadable quota state: " + state_error)
                release_claim_lock()
                return finish(result, output, workspace, 1)
            if state and state.get("retry_at", 0) > time.time():
                result.update(status="fallback_required", fallback=quota_fallback(provider_name), quota=state, cached=True)
                release_claim_lock()
                return finish(result, output, workspace, 20)
            if state:
                result["expired_quota"] = state
            if args.role != "implement":
                release_claim_lock()
            with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
                if args.role == "implement":
                    write_json(pending_path, {"status": "launching", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output)})
                process = None
                try:
                    process = subprocess.Popen(command, cwd=workspace, shell=False, stdout=stdout, stderr=stderr,
                                               start_new_session=os.name != "nt")
                    if args.role == "implement":
                        write_json(pending_path, {"status": "running", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "pid": process.pid})
                        release_claim_lock()
                    code = process.wait(timeout=timeout)
                except BaseException as exc:
                    if process is not None:
                        stop_process(process)
                    elif args.role == "implement" and isinstance(exc, OSError):
                        # Popen raised before returning a process: no writer was launched.
                        write_json(pending_path, {"status": "resolved", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "launch_error": str(exc)})
                    raise
            errors = error_objects((output / "stdout.log").read_text(encoding="utf-8", errors="replace"))
            errors += error_objects((output / "stderr.log").read_text(encoding="utf-8", errors="replace"))
            plain_error = plain_terminal_error((output / "stderr.log").read_text(encoding="utf-8", errors="replace"), code)
            if plain_error and not (output / "stdout.log").read_text(encoding="utf-8", errors="replace").strip():
                errors.append(plain_error)
            valid_success = successful_response((output / "stdout.log").read_text(encoding="utf-8", errors="replace"), args.role)
            denied = permission_denials((output / "stdout.log").read_text(encoding="utf-8", errors="replace"))
            if args.role == "implement" and (errors or valid_success or denied):
                write_json(pending_path, {"status": "resolved", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "exit_code": code})
            if denied:
                result.update(status="permission_denied", exit_code=code, denied_actions=denied,
                              error="Provider denied required tools or file access. Task is incomplete; no automatic fallback or permission bypass.")
                return finish(result, output, workspace, 1)
            quota = quota_error(errors, provider_name) if not valid_success else None
            if quota:
                with provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)), "quota.lock"):
                    state = quota_record(provider_name, quota, state_dir, output, config)
                result.update(status="fallback_required", fallback=quota_fallback(provider_name), quota=state, cached=False)
                return finish(result, output, workspace, 20)
            result.update(status="completed" if code == 0 and not errors and valid_success else "provider_error", exit_code=code)
            if args.role == "implement" and not errors and not valid_success:
                result["error"] = "No structured terminal result; backend completion uncertain. Pending state blocks new implementation until confirmed stopped."
            return finish(result, output, workspace, 0 if result["status"] == "completed" else 1)
    except subprocess.TimeoutExpired:
        result.update(status="timeout", error="CLI process tree stopped after timeout; provider backend completion remains unverified. No automatic fallback. Confirm provider session stopped before resolving pending state.")
    except (OSError, ValueError, TimeoutError) as exc:
        result.update(status="setup_error", error=str(exc))
    except KeyboardInterrupt:
        result.update(status="cancelled", error="CLI process tree stopped on interruption; provider backend completion remains unverified. No automatic fallback. Confirm provider session stopped before resolving pending state.")
    return finish(result, output, workspace, 1)


def finish(result, output, workspace, code):
    result["evidence"] = git_evidence(workspace, output)
    write_json(output / "result.json", result)
    return result, code


def cli_quota_status(args):
    config = read_json(args.config)
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("Routing configuration must contain a providers object")
    providers = [args.provider] if args.provider else ["gemini", "claude"]
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    output = {}
    code = 0
    for provider in providers:
        if not isinstance(config["providers"].get(provider), dict):
            output[provider] = {"status": "state_error", "available_to_try": 0,
                                "error": "Provider is missing from config"}
            code = 1
            continue
        state, error = quota_state(state_dir, provider)
        item = {"provider": provider, "model": config["providers"][provider].get("model"),
                "quota_path": str(quota_path(state_dir, provider)), "available_to_try": 1}
        if error:
            item.update(status="state_error", available_to_try=0, fallback=quota_fallback(provider), error="Malformed or unreadable quota state: " + error)
            code = 1
        elif state:
            item["cached_evidence"] = state
            if state.get("retry_at", 0) > time.time():
                item.update(status="fallback_required", available_to_try=0, fallback=quota_fallback(provider),
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
    if provider not in config.get("providers", {}):
        raise ValueError("Provider is missing from config")
    reason = args.reason.strip() if isinstance(args.reason, str) else ""
    if not reason:
        raise ValueError("reason must be non-empty")
    error = {"code": "QUOTA_EXHAUSTED", "message": reason, "source": "operator"}
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
        state = quota_record(provider, error, state_dir, "operator", config)
    return {"status": "recorded", "provider": provider, "quota": state, "quota_path": str(quota_path(state_dir, provider))}, 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("status", "quota-set"):
        command = sys.argv[1]
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--provider", choices=("gemini", "claude"), required=(command == "quota-set"))
        parser.add_argument("--config", required=True)
        parser.add_argument("--state-dir")
        if command == "quota-set":
            parser.add_argument("--reason", required=True)
            parser.add_argument("--reset-at")
        args = argparse.Namespace(provider=None)
        try:
            args = parser.parse_args(sys.argv[2:])
            result, code = cli_quota_status(args) if command == "status" else cli_quota_set(args)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {"status": "state_error", "available_to_try": 0, "error": str(exc)}
            if getattr(args, "provider", None):
                result["fallback"] = quota_fallback(args.provider)
            code = 1
        print(json.dumps(result, indent=2))
        return code
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("implement", "review"))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review-effort", choices=("medium", "high"), default=None)
    parser.add_argument("--review-reason", default=None)
    args = parser.parse_args()
    try:
        result, code = execute(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {"status": "setup_error", "error": str(exc)}, 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
