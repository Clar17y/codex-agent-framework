"""Local diff facts plus optional bounded, advisory Jev questions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

try:
    from jev_client import JevClient
    from jev_search import (EXCLUDED_DIR_NAMES, contains_literal_secret, is_file_excluded,
                            is_remote_authorized, load_config, safe_regular_file)
except ImportError:
    from scripts.jev_client import JevClient
    from scripts.jev_search import (EXCLUDED_DIR_NAMES, contains_literal_secret, is_file_excluded,
                                    is_remote_authorized, load_config, safe_regular_file)

MAX_DIFF_BYTES = 1024 * 1024
MAX_DESCRIPTION_BYTES = 16000
MAX_HUNKS = 96
MAX_CONTEXT_CHARS = 6000
CATEGORIES = {
    "credentials": "Does this change mishandle credentials, secrets or subprocess environment filtering?",
    "authorization": "Does this change weaken authorization or allow an unauthorized action?",
    "coordination": "Does this change risk overlapping writers, shared-state races or ordering errors?",
    "cancellation": "Does this change mishandle cancellation, deadlines, retries or cleanup?",
    "persistence": "Does this change risk data loss, unsafe migration or non-atomic persistence?",
    "compatibility": "Does this change break a public interface or established error behavior?",
    "test_coverage": "Does this change leave coupled state transitions without discriminating tests?",
}
HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _read_inside(workspace, path, limit):
    target = Path(path)
    if ".." in target.parts:
        raise ValueError("Parent traversal rejected")
    if not target.is_absolute():
        target = workspace / target
    if not safe_regular_file(target, workspace):
        raise ValueError("Input must be a regular file inside workspace without links or hardlinks")
    with target.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
    current = target.stat()
    if not safe_regular_file(target, workspace) or (opened.st_dev, opened.st_ino, opened.st_size) != (current.st_dev, current.st_ino, current.st_size):
        raise ValueError("Input changed during read")
    if len(raw) > limit:
        raise ValueError("Input exceeds size limit")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeError:
        raise ValueError("Input must be UTF-8") from None


def _header_path(raw, prefix):
    """Parse a Git file marker; escaped names need a fuller Git decoder."""
    raw = raw.rstrip("\r\n").split("\t", 1)[0]
    if "\\" in raw:
        return None
    if raw.startswith('"'):
        try:
            parts = shlex.split(raw)
        except ValueError:
            return None
        if len(parts) != 1:
            return None
        raw = parts[0]
    if not raw.startswith(prefix):
        return None
    return raw[len(prefix):]


def _excluded_changed_path(paths):
    for path in paths:
        parts = Path(path.replace("\\", "/")).parts
        if any(part in EXCLUDED_DIR_NAMES for part in parts) or is_file_excluded(parts[-1] if parts else ""):
            return True
    return False


def _header_matches(line, old_path, new_path):
    """Require the diff header to name the same paths as the authoritative markers."""
    old_header = old_path or new_path
    new_header = new_path or old_path
    if not old_header or not new_header or "\\" in line:
        return False
    raw = line.rstrip("\r\n")
    if raw.startswith('diff --git "'):
        try:
            parts = shlex.split(raw)
        except ValueError:
            return False
        return parts == ["diff", "--git", f"a/{old_header}", f"b/{new_header}"]
    return raw == f"diff --git a/{old_header} b/{new_header}"


def _parse_block(lines, reasons):
    """A file block owns its hunks regardless of malformed hunk counts."""
    header = lines[0]
    old_marker = new_marker = None
    seen_old_marker = seen_new_marker = False
    rename_old = rename_new = None
    hunks = []
    current = None
    invalid = False
    binary = False
    for line in lines[1:]:
        if line.startswith("@@"):
            if current:
                hunks.append(current)
            match = HEADER.match(line)
            if not match:
                reasons.append("unsupported_hunk")
                invalid = True
                current = None
                continue
            old_start, old_count, new_start, new_count = [int(v) if v is not None else 1 for v in match.groups()]
            current = {"old_start": old_start, "new_start": new_start,
                       "old_count": old_count, "new_count": new_count, "added": 0, "deleted": 0,
                       "text": line, "old_seen": 0, "new_seen": 0}
            continue
        if current is not None:
            # A lone marker can conceal a cut/pasted file boundary too. Keep
            # ambiguous literal header-shaped content local as an unknown.
            if (line.startswith(("--- ", "+++ ")) and
                    line[4:].lstrip().startswith(("a/", "b/", '"', "/dev/null"))):
                invalid = True
                reasons.append("ambiguous_embedded_file_header")
            current["text"] += line
            if line.startswith("+"):
                current["added"] += 1
                current["new_seen"] += 1
            elif line.startswith("-"):
                current["deleted"] += 1
                current["old_seen"] += 1
            elif line.startswith(" ") or line in ("\n", "\r\n"):
                current["old_seen"] += 1
                current["new_seen"] += 1
            elif not line.startswith("\\ No newline"):
                invalid = True
                reasons.append("unsupported_hunk_line")
            continue
        if line.startswith("--- "):
            if seen_old_marker:
                invalid = True
            seen_old_marker = True
            old_marker = None if line[4:].strip() == "/dev/null" else _header_path(line[4:], "a/")
            if old_marker is None and line[4:].strip() != "/dev/null":
                invalid = True
        elif line.startswith("+++ "):
            if seen_new_marker:
                invalid = True
            seen_new_marker = True
            new_marker = None if line[4:].strip() == "/dev/null" else _header_path(line[4:], "b/")
            if new_marker is None and line[4:].strip() != "/dev/null":
                invalid = True
        elif line.startswith("rename from "):
            rename_old = _header_path(line[len("rename from "):], "")
            invalid |= rename_old is None
        elif line.startswith("rename to "):
            rename_new = _header_path(line[len("rename to "):], "")
            invalid |= rename_new is None
        elif line.startswith(("Binary files ", "GIT binary patch")):
            binary = True
            reasons.append("binary_diff")
        elif line.startswith(("index ", "new file mode ", "deleted file mode ", "old mode ",
                              "new mode ", "similarity index ", "dissimilarity index ")):
            pass
        elif line.strip():
            invalid = True
            reasons.append("unsupported_metadata")
    if current:
        hunks.append(current)
    if old_marker is None and new_marker is None and rename_old and rename_new:
        old_marker, new_marker = rename_old, rename_new
    if not _header_matches(header, old_marker, new_marker) or (rename_old is not None and rename_old != old_marker) or (rename_new is not None and rename_new != new_marker):
        invalid = True
        reasons.append("unsupported_path_header")
    if binary:
        invalid = True
    if any(h["old_seen"] != h["old_count"] or h["new_seen"] != h["new_count"] for h in hunks):
        invalid = True
        reasons.append("incomplete_hunk")
    if invalid:
        reasons.append("unsupported_file_block")
    path = new_marker or old_marker
    paths = tuple(sorted({p for p in (old_marker, new_marker, rename_old, rename_new) if p}))
    for h in hunks:
        h.update(path=path, paths=paths, invalid=invalid)
    return path, hunks


def parse_diff(text, totals=None):
    """Split on every file header first, then validate and count each block independently."""
    files, kept, reasons = set(), [], []
    blocks = []
    current = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current is None:
            if line.strip():
                reasons.append("content_before_diff")
        else:
            current.append(line)
    if current:
        blocks.append(current)
    hunk_total = added_total = deleted_total = 0
    for block in blocks:
        path, hunks = _parse_block(block, reasons)
        if path is not None:
            files.add(path)
        for h in hunks:
            hunk_total += 1
            added_total += h["added"]
            deleted_total += h["deleted"]
            if len(kept) < MAX_HUNKS:
                kept.append(h)
    omitted = hunk_total - len(kept)
    if omitted:
        reasons.append("hunk_limit")
    if totals is not None:
        totals.update(hunks_total=hunk_total, hunks_omitted=omitted,
                      added_lines=added_total, deleted_lines=deleted_total)
    return files, kept, sorted(set(reasons))


def run_review(workspace, diff_file, description_file=None, config_path=None, allow_remote=False, client=None):
    workspace = Path(os.path.abspath(workspace))
    if not workspace.is_dir():
        return {"command": "review", "status": "error", "error": "Invalid workspace"}
    try:
        text, raw = _read_inside(workspace, diff_file, MAX_DIFF_BYTES)
        description = _read_inside(workspace, description_file, MAX_DESCRIPTION_BYTES)[0] if description_file else None
        config, _ = load_config(config_path)
    except (OSError, ValueError, TypeError):
        return {"command": "review", "status": "error", "error": "Invalid review input or configuration"}
    diff_hash = hashlib.sha256(raw).hexdigest()
    totals = {}
    files, hunks, reasons = parse_diff(text, totals)
    facts = {"changed_files": len(files), "hunks": totals["hunks_total"], **totals}
    signals, unknowns = [], []
    questions, mapping = {}, {}
    if not text.startswith("diff --git "):
        reasons.append("not_git_diff")
    if not hunks:
        reasons.append("no_supported_hunks")
    for idx, h in enumerate(hunks):
        # Changed source paths follow the search exclusion policy; diff container path does not.
        if h["invalid"]:
            unknowns.append({"hunk": idx, "reason": "unsupported_file_block"})
            continue
        if _excluded_changed_path(h["paths"]):
            reasons.append("excluded_path")
            unknowns.append({"hunk": idx, "reason": "excluded_path"})
            continue
        if len(h["path"]) > 240:
            reasons.append("long_path_omitted")
            unknowns.append({"hunk": idx, "reason": "path_limit"})
            continue
        if contains_literal_secret((h["path"] + h["text"]).encode()):
            reasons.append("secret_hunk_omitted")
            unknowns.append({"hunk": idx, "reason": "secret_content_filtered"})
            continue
        if len(h["text"]) > MAX_CONTEXT_CHARS:
            reasons.append("oversized_hunk_omitted")
            unknowns.append({"hunk": idx, "reason": "context_limit"})
            continue
        for category, prompt in CATEGORIES.items():
            qid = f"h{idx}_{category}"
            questions[qid] = {"type": "noul", "instructions": f"{h['path']} new lines {h['new_start']}-{max(h['new_start'], h['new_start'] + h['new_count'] - 1)}\n{h['text']}\n{prompt}",
                              "criteria": {"true": "The shown change presents a concrete reason for focused human review in this category.",
                                           "false": "No concrete concern in the shown change for this category."}}
            mapping[qid] = (idx, category)
        if description and not contains_literal_secret(description.encode()) and len(description) <= 4000:
            qid = f"h{idx}_intent"
            questions[qid] = {"type": "noul", "instructions": f"Description: {description}\nDiff hunk: {h['text']}\nDoes the change contradict the stated intent?",
                              "criteria": {"true": "A specific intent mismatch is visible in the diff.", "false": "No visible contradiction."}}
            mapping[qid] = (idx, "intent")
    if description and contains_literal_secret(description.encode()):
        reasons.append("secret_description_omitted")
    elif description and len(description) > 4000:
        reasons.append("long_description_omitted")
    usage = {"requests_made": 0, "estimated_input_tokens": 0, "actual_input_tokens": 0, "output_tokens": 0}
    authorized = is_remote_authorized(workspace, config, allow_remote)
    has_key = bool(os.environ.get("TYPESAFE_API_KEY", "").strip()) or client is not None and client.transport is not None
    if questions and authorized and has_key:
        if client is None:
            client = JevClient(model=config["model"], timeout_seconds=config["timeout_seconds"],
                               deadline_seconds=config["deadline_seconds"], max_requests=config["max_requests"])
        result = client.evaluate_questions("Advisory code review signals; answer only from shown diff context, not assumed surrounding code.", questions,
                                           max_input_tokens=config["max_input_tokens"])
        usage = {"requests_made": result["requests_made"], "estimated_input_tokens": result["estimated_input_tokens"],
                 "actual_input_tokens": result["usage"]["input_tokens"], "output_tokens": result["usage"]["output_tokens"]}
        if not result["completed"]:
            reasons.append(result["stop_reason"] or "semantic_incomplete")
        for qid, answer in result["answers"].items():
            if qid not in mapping:
                continue
            idx, category = mapping[qid]
            h = hunks[idx]
            signals.append({"category": category, "probability": answer["noul"], "path": h["path"][:200],
                            "hunk": idx, "new_start": h["new_start"], "new_end": (h["new_start"] + h["new_count"] - 1 if h["new_count"] else None),
                            "old_start": h["old_start"], "old_end": (h["old_start"] + h["old_count"] - 1 if h["old_count"] else None),
                            "diff_sha256": diff_hash})
        for qid in set(questions) - set(result["answers"]):
            idx, category = mapping[qid]
            unknowns.append({"hunk": idx, "category": category, "reason": result.get("unknown_reasons", {}).get(qid, "missing_signal")})
    else:
        missing_reason = "remote_disabled" if not authorized else "missing_api_key" if not has_key else "no_safe_context"
        reasons.append(missing_reason)
        for qid, (idx, category) in mapping.items():
            unknowns.append({"hunk": idx, "category": category, "reason": missing_reason})
    if totals["hunks_omitted"]:
        unknowns.append({"hunk": len(hunks), "reason": "hunk_limit", "count": totals["hunks_omitted"]})
    signals.sort(key=lambda x: (-x["probability"], x["hunk"], x["category"]))
    unknowns.sort(key=lambda x: (x["hunk"], x.get("category", ""), x["reason"]))
    focus = [signal for signal in signals if signal["probability"] >= 0.5]
    reasons = sorted(set(reasons))
    return {"command": "review", "status": "complete" if not reasons and not unknowns else "partial",
            "advisory_only": True, "diff_sha256": diff_hash, "facts": facts,
            "focus": focus[:20], "focus_count": len(focus), "focus_truncated": len(focus) > 20,
            "signals_count": len(signals), "provisional_focus_threshold": 0.5,
            "unknowns": unknowns[:24], "unknown_count": len(unknowns) + max(0, totals["hunks_omitted"] - 1),
            "unknowns_truncated": len(unknowns) > 24 or totals["hunks_omitted"] > 1, "usage": usage,
            "coverage": {"complete": not reasons and not unknowns, "reasons": reasons}}


def main():
    parser = argparse.ArgumentParser(description="Bounded advisory Jev diff review")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--description-file")
    parser.add_argument("--config")
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    try:
        result = run_review(args.workspace, args.diff_file, args.description_file, args.config, args.allow_remote)
    except Exception:
        result = {"command": "review", "status": "error", "error": "Invalid input or processing failure"}
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    if result["status"] == "error":
        sys.exit(2)


if __name__ == "__main__":
    main()
