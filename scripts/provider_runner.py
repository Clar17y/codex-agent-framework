"""Bounded external provider execution with shared Gemini/Claude quota preflight."""
import argparse
import contextlib
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UTC = dt.timezone.utc

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
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (str(uuid.uuid4()) + ".tmp")
    temporary.write_bytes(data)
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


def command_for(role, provider, prompt, timeout, workspace, effort="medium", provider_name=None, output_dir=None):
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

    if provider_name == "deepseek":
        command = direct_windows_codex_command(command)

    model = provider["model"]
    if role == "review":
        if provider_name != "claude":
            raise ValueError(f"Review role is Claude-only; got {provider_name}")
        expected = "claude-opus-5"
        if model != expected:
            raise ValueError(f"{role} requires pinned model {expected}; got {model}")
        if effort not in ("medium", "high"):
            raise ValueError(f"Claude review effort must be 'medium' or 'high'; got {effort}")
        return command + ["-p", "Read-only independent review. Do not change files.\n" + prompt,
                          "--model", model, "--effort", effort, "--output-format", "json", "--no-session-persistence",
                          "--dangerously-skip-permissions", "--safe-mode", "--tools", "Read,Glob,Grep", "--strict-mcp-config",
                          "--disable-slash-commands"]

    if role == "implement":
        if provider_name == "gemini":
            expected = "gemini-3.8-flash-medium"
            if model != expected:
                raise ValueError(f"{role} requires pinned model {expected}; got {model}")
            return command + ["--print", prompt, "--model", model, "--mode", "accept-edits",
                              "--dangerously-skip-permissions", "--add-dir", str(workspace), "--output-format", "json", "--print-timeout", f"{timeout}s"]
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
        raise ValueError(f"Unsupported implementation provider: {provider_name}")

    raise ValueError(f"Unsupported role: {role}")


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


def structured_error_message(errors):
    """Return the first useful message from normalized provider errors."""
    for error in errors:
        for field in ("message", "result", "response"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_deepseek_final_output(output_dir, stdout_text):
    """Parse and validate final JSON output object from DeepSeek.
    Success requires explicit success status plus a string response.
    Arbitrary messages, progress events, malformed output, or blocked/error status are not success."""
    raw_content = None
    if output_dir:
        last_msg_file = output_dir / "last_message.txt"
        if last_msg_file.exists():
            raw_content = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()

    obj = None
    if raw_content:
        try:
            parsed = json.loads(raw_content)
            if isinstance(parsed, dict):
                obj = parsed
        except ValueError:
            return None, False, False
    elif stdout_text:
        try:
            parsed = json.loads(stdout_text.strip())
            if isinstance(parsed, dict):
                obj = parsed
        except ValueError:
            for line in reversed(stdout_text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and "status" in parsed and "response" in parsed and "type" not in parsed:
                        obj = parsed
                        break
                except ValueError:
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
        except ValueError:
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


def successful_response(text, role, provider="gemini", output_dir=None):
    if provider == "deepseek":
        _, _, is_success = parse_deepseek_final_output(output_dir, text)
        return is_success

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


def balance_path(state_dir):
    return state_dir / "deepseek-balance.json"


def luna_fallback():
    return {"model": "gpt-5.6-luna", "effort": "medium"}


def quota_fallback(provider, config=None):
    if provider == "gemini":
        if config and isinstance(config.get("providers"), dict) and "deepseek" in config["providers"]:
            ds = config["providers"]["deepseek"]
            if isinstance(ds, dict) and ds.get("enabled", True) is not False:
                model = ds.get("model", "deepseek-flash")
                return {"provider": "deepseek", "model": model}
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
    """Return configured DeepSeek credentials present in this process, never their names or values in results."""
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    deepseek = providers.get("deepseek", {}) if isinstance(providers, dict) else {}
    configured = deepseek.get("api_key_env", "DEEPSEEK_API_KEY") if isinstance(deepseek, dict) else "DEEPSEEK_API_KEY"
    names = {"DEEPSEEK_API_KEY"}
    if isinstance(configured, str):
        names.add(configured)
    return [(name, os.environ[name]) for name in names if os.environ.get(name) and os.environ[name].strip()]


def child_environment(config, provider_name, checked_deepseek_key=None):
    """Explicit child environment: no DeepSeek credential crosses non-DeepSeek provider boundaries."""
    env = dict(os.environ)
    for name, _ in deepseek_secrets(config):
        env.pop(name, None)
    if provider_name == "deepseek" and checked_deepseek_key:
        # The default Codex profile consumes this canonical name.  Do not leave a
        # second configured name that could make it select a different account.
        env["DEEPSEEK_API_KEY"] = checked_deepseek_key
    return env


def sanitize_run_artifacts(output, secrets):
    if not secrets or not Path(output).exists():
        return
    for name in ("prompt.txt", "stdout.log", "stderr.log", "last_message.txt",
                 "status.txt", "diff.txt", "head.txt"):
        path = Path(output) / name
        try:
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            clean = _sanitize_all_secrets(raw, secrets)
            if clean != raw:
                # Atomic replacement avoids following a hard link while writing.
                write_bytes(path, clean.encode("utf-8"))
        except OSError:
            continue


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
    quota_cooldown(config)
    provider_name = getattr(args, "provider", None) or ("gemini" if args.role == "implement" else "claude")
    if args.role == "review" and provider_name != "claude":
        raise ValueError(f"Review role is Claude-only; got {provider_name}")
    if args.role == "implement" and provider_name not in ("gemini", "deepseek"):
        raise ValueError(f"Implement role does not support provider {provider_name}")
    if provider_name not in config.get("providers", {}):
        raise ValueError(f"Provider '{provider_name}' is missing from config")
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
    state_dir = Path(args.state_dir or (Path(args.config).resolve().parent / "state"))
    if args.dry_run:
        command = command_for(args.role, provider, prompt, timeout, workspace, review_effort or "medium", provider_name=provider_name)
        dry_run_meta = {"status": "dry_run", "provider": provider_name, "model": provider["model"],
                        "cwd": str(workspace), "command": command, "state_dir": str(state_dir)}
        if args.role == "review":
            dry_run_meta["review_effort"] = review_effort
            dry_run_meta["review_reason"] = review_reason
        return dry_run_meta, 0

    secrets = [value.strip() for _, value in deepseek_secrets(config)]
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
    prompt_path.write_text(prompt, encoding="utf-8")
    run_prompt = (
        f"Read the complete task contract and instructions in {prompt_path} and follow them. "
        "Execute the task yourself; never spawn subagents or use another AI provider."
    )
    if provider_name == "deepseek":
        run_prompt += (
            " When complete, return your final response as a JSON object strictly conforming to the schema: "
            '{"status": "SUCCESS" | "FAILED" | "BLOCKED" | "ERROR", "response": "<summary>"}.'
        )
    command = command_for(args.role, provider, run_prompt,
                          timeout, workspace, review_effort or "medium", provider_name=provider_name, output_dir=output)
    result = {"provider": provider_name, "model": provider["model"], "logs": str(output)}
    if args.role == "review":
        result["review_effort"] = review_effort
        result["review_reason"] = review_reason
    workspace_state = workspace_state_dir(state_dir, workspace)
    # This lock covers only the check-and-claim transaction. Provider processes run
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
                                  fallback=luna_fallback(),
                                  error="Unresolved legacy run belongs to this workspace or has unknown ownership. Confirm it stopped before resolving its pending record.")
                    release_claim_lock()
                    return finish(result, output, workspace, 1, secrets=secrets)
            if args.role == "implement":
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
            if provider_name in ("gemini", "claude"):
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
            if args.role != "implement":
                release_claim_lock()
            with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
                if args.role == "implement":
                    write_json(pending_path, {"status": "launching", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "provider": provider_name})
                process = None
                try:
                    process = subprocess.Popen(command, cwd=workspace, shell=False, stdout=stdout, stderr=stderr,
                                               env=child_environment(config, provider_name, checked_deepseek_key),
                                               start_new_session=os.name != "nt")
                    if args.role == "implement":
                        write_json(pending_path, {"status": "running", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "pid": process.pid, "provider": provider_name})
                        release_claim_lock()
                    code = process.wait(timeout=timeout)
                except BaseException as exc:
                    if process is not None:
                        stop_process(process)
                    elif args.role == "implement" and isinstance(exc, OSError):
                        # Popen raised before returning a process: no writer was launched.
                        write_json(pending_path, {"status": "resolved", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "launch_error": str(exc), "provider": provider_name})
                    raise

            stdout_file = output / "stdout.log"
            stderr_file = output / "stderr.log"
            raw_stdout = stdout_file.read_text(encoding="utf-8", errors="replace")
            raw_stderr = stderr_file.read_text(encoding="utf-8", errors="replace")
            clean_stdout = _sanitize_all_secrets(raw_stdout, secrets)
            clean_stderr = _sanitize_all_secrets(raw_stderr, secrets)
            if clean_stdout != raw_stdout:
                stdout_file.write_text(clean_stdout, encoding="utf-8")
            if clean_stderr != raw_stderr:
                stderr_file.write_text(clean_stderr, encoding="utf-8")
            stdout_text = clean_stdout
            stderr_text = clean_stderr

            errors = error_objects(stdout_text)
            errors += error_objects(stderr_text)
            plain_error = plain_terminal_error(stderr_text, code)
            if plain_error and not stdout_text.strip():
                errors.append(plain_error)
            is_terminal = False
            ds_output = None
            if provider_name == "deepseek":
                ds_output, is_terminal, valid_success = parse_deepseek_final_output(output, stdout_text)
            else:
                valid_success = successful_response(stdout_text, args.role)
            denied = permission_denials(stdout_text)
            # A local child that returned from wait has definitely stopped, even
            # when its output is denied, malformed, or otherwise unsuccessful.
            if args.role == "implement":
                write_json(pending_path, {"status": "resolved", "workspace": str(workspace), "owned_paths": owned_paths, "logs": str(output), "exit_code": code, "provider": provider_name})
            if denied:
                result.update(status="permission_denied", exit_code=code, denied_actions=denied)
                if args.role == "implement":
                    result["fallback"] = luna_fallback()
                    result["error"] = "Provider denied required tools or file access. Task is incomplete; retry with native Luna fallback."
                else:
                    result["error"] = "Provider denied required tools or file access. Task is incomplete; no automatic permission bypass."
                return finish(result, output, workspace, 1, secrets=secrets)

            if provider_name == "deepseek":
                terminal_turn, terminal_event, has_terminal_402, _ = parse_codex_events(stdout_text)

                if valid_success and code == 0 and terminal_turn != "failed":
                    result.update(status="completed", exit_code=code)
                    refresh_balance_safe(state_dir, config)
                    return finish(result, output, workspace, 0, secrets=secrets)

                # Only check for runtime 402/insufficient balance when there is no terminal success
                insufficient_balance = None
                if has_terminal_402:
                    insufficient_balance = {"code": "INSUFFICIENT_BALANCE", "message": "HTTP 402: Insufficient balance", "source": "codex_event"}
                else:
                    insufficient_balance = deepseek_insufficient_balance_error(errors, stderr_text)

                if insufficient_balance:
                    record_balance_snapshot(state_dir, {"is_available": False, "balance_infos": []},
                                            error=insufficient_balance.get("message", "HTTP 402: Insufficient balance"), config=config)
                    result.update(status="fallback_required", fallback=luna_fallback(),
                                  error=insufficient_balance.get("message", "HTTP 402: Insufficient balance"), exit_code=code)
                    return finish(result, output, workspace, 20, secrets=secrets)

                result.update(status="provider_error", exit_code=code if code != 0 else 1, fallback=luna_fallback())
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

            quota = quota_error(errors, provider_name) if not valid_success else None
            if quota:
                with provider_lock(state_dir, float(config.get("lock_timeout_seconds", 30)), "quota.lock"):
                    state = quota_record(provider_name, quota, state_dir, output, config)
                result.update(status="fallback_required", fallback=quota_fallback(provider_name, config), quota=state, cached=False)
                return finish(result, output, workspace, 20, secrets=secrets)
            result.update(status="completed" if code == 0 and not errors and valid_success else "provider_error", exit_code=code)
            if args.role == "implement" and not valid_success:
                result["fallback"] = luna_fallback()
                if not errors:
                    result["error"] = "No structured terminal result; backend completion uncertain. Pending state blocks new implementation until confirmed stopped."
            return finish(result, output, workspace, 0 if result["status"] == "completed" else 1, secrets=secrets)
    except subprocess.TimeoutExpired:
        if args.role == "implement":
            result.update(status="timeout", fallback=luna_fallback(),
                          error="CLI process tree stopped after timeout; provider backend completion remains unverified. Confirm provider session stopped before resolving pending state or continuing with Luna fallback.")
        else:
            result.update(status="timeout",
                          error="CLI process tree stopped after timeout; provider backend completion remains unverified. Confirm provider session stopped.")
    except (OSError, ValueError, TimeoutError) as exc:
        result.update(status="setup_error", error=str(exc))
        if args.role == "implement":
            result["fallback"] = luna_fallback()
    except KeyboardInterrupt:
        if args.role == "implement":
            result.update(status="cancelled", fallback=luna_fallback(),
                          error="CLI process tree stopped on interruption; provider backend completion remains unverified. Confirm provider session stopped before resolving pending state or continuing with Luna fallback.")
        else:
            result.update(status="cancelled",
                          error="CLI process tree stopped on interruption; provider backend completion remains unverified. Confirm provider session stopped.")
    return finish(result, output, workspace, 1, secrets=secrets)


def finish(result, output, workspace, code, secrets=None):
    result["evidence"] = git_evidence(workspace, output, secrets)
    sanitize_run_artifacts(output, secrets)
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
    write_json(output / "result.json", result)
    return result, code


def cli_quota_status(args):
    config = read_json(args.config)
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("Routing configuration must contain a providers object")
    providers = [args.provider] if args.provider else [p for p in ("gemini", "deepseek", "claude") if p in config["providers"]]
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

        state, error = quota_state(state_dir, provider)
        item = {"provider": provider, "model": config["providers"][provider].get("model"),
                "quota_path": str(quota_path(state_dir, provider)), "available_to_try": 1}
        if error:
            fallback = luna_fallback() if provider == "gemini" else quota_fallback(provider, config)
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
    if provider not in ("gemini", "claude"):
        raise ValueError(f"quota-set only supports subscription quota providers ('gemini', 'claude'); '{provider}' uses monetary balance preflight")
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
        parser.add_argument("--config", required=True)
        parser.add_argument("--state-dir")
        if command == "status":
            parser.add_argument("--provider", choices=("gemini", "deepseek", "claude"))
            parser.add_argument("--check-live", "--live", dest="check_live", action="store_true")
        if command == "quota-set":
            parser.add_argument("--provider", choices=("gemini", "claude"), required=True)
            parser.add_argument("--reason", required=True)
            parser.add_argument("--reset-at")
        args = argparse.Namespace(provider=None)
        try:
            args = parser.parse_args(sys.argv[2:])
            result, code = cli_quota_status(args) if command == "status" else cli_quota_set(args)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {"status": "state_error", "available_to_try": 0, "error": str(exc)}
            if getattr(args, "provider", None):
                result["fallback"] = luna_fallback() if args.provider in ("gemini", "deepseek") else {"model": "gpt-6-astra", "effort": "low"}
            code = 1
        print(json.dumps(result, indent=2))
        return code
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("implement", "review"))
    parser.add_argument("--provider", choices=("gemini", "deepseek", "claude"), default=None)
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
