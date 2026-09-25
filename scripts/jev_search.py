"""Bounded semantic code search helper using TypeSafe Jev SystemOne.

CLI commands:
  doctor: Inspect environment, configuration, and API readiness without contacting remote.
  inspect: Enumerate and chunk candidates within workspace/scopes without contacting remote.
  search: Bounded semantic search with caching, line-accurate evidence, and deterministic fallback.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# Client import
try:
    from jev_client import (
        DEFAULT_DEADLINE_SECONDS,
        DEFAULT_ENDPOINT,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT_SECONDS,
        JevAuthError,
        JevBudgetExceededError,
        JevClient,
        JevError,
        JevRateLimitError,
        JevServerError,
        JevUnprocessableError,
        JevValidationError,
    )
except ImportError:
    from scripts.jev_client import (
        DEFAULT_DEADLINE_SECONDS,
        DEFAULT_ENDPOINT,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT_SECONDS,
        JevAuthError,
        JevBudgetExceededError,
        JevClient,
        JevError,
        JevRateLimitError,
        JevServerError,
        JevUnprocessableError,
        JevValidationError,
    )

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "allowed_roots": [],
    "model": DEFAULT_MODEL,
    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    "deadline_seconds": DEFAULT_DEADLINE_SECONDS,
    "max_requests": 8,
    "max_input_tokens": 120000,
    "max_candidates": 96,
    "max_output_chars": 12000,
    "cache_ttl_seconds": 604800.0,  # 7 days
}

EXCLUDED_DIR_NAMES = frozenset((
    ".git",
    ".llm-output",
    "state",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".tox",
    "target",
    "bin",
    "obj",
))

EXCLUDED_FILE_PATTERNS = (
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r"^.*id_(rsa|ed25519|dsa|ecdsa).*$", re.IGNORECASE),
    re.compile(r"^.*\.(pem|key|p12|pfx|pkcs12)$", re.IGNORECASE),
    re.compile(r"^(credentials|token|secrets?)\.json$", re.IGNORECASE),
    re.compile(r"^\.(npmrc|netrc|pypirc|git-credentials)$", re.IGNORECASE),
)

LITERAL_SECRET_PATTERNS = (
    re.compile(r"AIzaSy[A-Za-z0-9_\-]{33}"),
    re.compile(r"sk-(?:proj-|ant-[A-Za-z0-9]+-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xox[bp]-[A-Za-z0-9-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/]{20,}"),
    re.compile(r"\bTYPESAFE_API_KEY\s*[=:]\s*[\'\"]?[^\s\'\"]{8,}"),
)

STOPWORDS = frozenset((
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "cannot", "could", "couldn't",
    "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during",
    "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "isn't", "it", "it's",
    "its", "itself", "let's", "me", "more", "most", "mustn't", "my", "myself",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought",
    "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "should", "shouldn't", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "wasn't", "we", "were", "weren't", "what", "when", "where", "which", "while",
    "who", "whom", "why", "with", "won't", "would", "wouldn't", "you", "your",
    "yours", "yourself", "yourselves",
))

RUBRIC_LAYOUT_VERSION = "jev-code-search-v1"
RUBRIC_TEMPLATE = """You are an expert code relevance evaluator.
Query: {query}

Rubric:
Assess whether the code excerpt defines, implements, or directly controls the behavior asked about in the query.
An excerpt is RELEVANT if it contains actual implementation, function/class definitions, or substantive logic for the queried behavior.
An excerpt is IRRELEVANT if it only mentions related terms incidentally, in comments, imports, or boilerplate without substantive logic."""


def is_symlink_or_reparse(path: Path) -> bool:
    """Check if path is a symlink or Windows reparse point (junction)."""
    try:
        if path.is_symlink():
            return True
        if not path.exists():
            return False
        st = os.lstat(path)
        if hasattr(st, "st_file_attributes") and (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return True
        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag != 0:
            return True
    except OSError:
        return False
    return False


def is_safe_contained_path(path: Path, root: Path) -> bool:
    """Check lexical ancestry before resolving; reject links anywhere below root."""
    try:
        root = Path(os.path.abspath(root))
        path = Path(os.path.abspath(path))
        if path != root and root not in path.parents:
            return False
        current = path
        while True:
            if is_symlink_or_reparse(current):
                return False
            if current == root:
                break
            current = current.parent
        ancestor = root.parent
        while ancestor != ancestor.parent:
            if is_symlink_or_reparse(ancestor):
                return False
            ancestor = ancestor.parent
        resolved = path.resolve()
        resolved_root = root.resolve()
        return resolved == resolved_root or resolved_root in resolved.parents
    except (OSError, RuntimeError):
        return False


def safe_regular_file(path: Path, workspace: Path) -> bool:
    if not is_safe_contained_path(path, workspace):
        return False
    try:
        st = path.lstat()
        return stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    except OSError:
        return False


def validate_config_dict(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate numeric bounds and types for Jev configuration."""
    validated = dict(DEFAULT_CONFIG)

    if "enabled" in cfg:
        val = cfg["enabled"]
        if not isinstance(val, bool):
            raise ValueError(f"config.enabled must be a boolean, got {type(val).__name__}")
        validated["enabled"] = val

    if "allowed_roots" in cfg:
        val = cfg["allowed_roots"]
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ValueError("config.allowed_roots must be a list of string paths")
        if len(val) > 32 or any(not x.strip() or not Path(x).is_absolute() or len(x) > 1024 for x in val):
            raise ValueError("config.allowed_roots exceeds limit")
        validated["allowed_roots"] = list(val)

    if "model" in cfg:
        val = cfg["model"]
        if val != DEFAULT_MODEL:
            raise ValueError("config.model must be pinned jev-1.13.0")
        validated["model"] = val.strip()

    num_fields = {
        "timeout_seconds": (float, 0.0, None),
        "deadline_seconds": (float, 0.0, None),
        "max_requests": (int, 1, None),
        "max_input_tokens": (int, 1, None),
        "max_candidates": (int, 1, None),
        "max_output_chars": (int, 1, None),
        "cache_ttl_seconds": (float, 0.0, None),
    }

    for field, (expected_type, min_val, max_val) in num_fields.items():
        if field in cfg:
            val = cfg[field]
            if isinstance(val, bool):
                raise ValueError(f"config.{field} cannot be a boolean")
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"config.{field} must be numeric, got {type(val).__name__}")
            if expected_type is int:
                if not isinstance(val, int):
                    raise ValueError(f"config.{field} must be an integer")
            else:
                val = float(val)
            if val > {"timeout_seconds": 120, "deadline_seconds": 300, "max_requests": 32, "max_input_tokens": 500000, "max_candidates": 512, "max_output_chars": 20000, "cache_ttl_seconds": 2592000}[field]:
                raise ValueError(f"config.{field} exceeds limit")
            if min_val is not None and val < min_val:
                raise ValueError(f"config.{field} ({val}) must be >= {min_val}")
            if max_val is not None and val > max_val:
                raise ValueError(f"config.{field} ({val}) must be <= {max_val}")
            validated[field] = val

    return validated


def load_config(config_path: Optional[str] = None, script_dir: Optional[Path] = None) -> Tuple[Dict[str, Any], str]:
    """Load configuration from explicit path or discover ../routing.json."""
    if script_dir is None:
        script_dir = Path(__file__).resolve().parent

    if config_path:
        cp = Path(config_path).resolve()
        if not cp.is_file():
            raise FileNotFoundError(f"Specified config file does not exist: {config_path}")
        try:
            content = cp.read_text(encoding="utf-8-sig")
            data = json.loads(content)
        except Exception as e:
            raise ValueError(f"Malformed config JSON at {config_path}: {e}") from None

        jev_obj = data.get("capabilities", {}).get("jev") if isinstance(data, dict) and "capabilities" in data else None
        if jev_obj is None and isinstance(data, dict) and "jev" in data:
            jev_obj = data.get("jev")
        if jev_obj is None and isinstance(data, dict):
            jev_obj = data

        if not isinstance(jev_obj, dict):
            raise ValueError(f"Config at {config_path} has malformed capabilities.jev object")
        return validate_config_dict(jev_obj), str(cp)

    # Discover ../routing.json relative to script
    default_routing = (script_dir / "../routing.json").resolve()
    if default_routing.is_file():
        try:
            content = default_routing.read_text(encoding="utf-8-sig")
            data = json.loads(content)
        except Exception as e:
            raise ValueError(f"Malformed discovered routing.json at {default_routing}: {e}") from None

        if isinstance(data, dict):
            jev_obj = data.get("capabilities", {}).get("jev", {})
            if isinstance(jev_obj, dict):
                return validate_config_dict(jev_obj), str(default_routing)

    # Absent file means defaults
    return dict(DEFAULT_CONFIG), "defaults"


def is_remote_authorized(workspace: Path, config: Dict[str, Any], allow_remote: bool) -> bool:
    """Check if remote search is authorized."""
    if allow_remote:
        return True
    if not config.get("enabled", False):
        return False

    resolved_ws = workspace.resolve()
    allowed_roots = [Path(r).resolve() for r in config.get("allowed_roots", [])]
    return any(resolved_ws == root for root in allowed_roots)


class CandidateChunk:
    """A bounded line-range chunk of a source file."""

    def __init__(
        self,
        relpath: str,
        start_line: int,
        end_line: int,
        text: str,
        content_hash: str,
        file_hash: str,
        lexical_score: float = 0.0,
    ):
        self.relpath = relpath
        self.start_line = start_line
        self.end_line = end_line
        self.text = text
        self.content_hash = content_hash
        self.file_hash = file_hash
        self.lexical_score = lexical_score
        self.score: Optional[float] = None
        self.unscored: bool = True
        self.unknown_reason: Optional[str] = None


def is_file_excluded(filename: str) -> bool:
    """Check if filename matches credential or ignored file patterns."""
    for pat in EXCLUDED_FILE_PATTERNS:
        if pat.match(filename):
            return True
    return False


def contains_literal_secret(content_bytes: bytes) -> bool:
    """Scan content for literal secret patterns."""
    try:
        text = content_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return False
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if key and len(key) >= 8 and key in text:
        return True
    for pat in LITERAL_SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


MAX_CHUNK_SERIALIZED_BYTES = 8000


def _iter_chunk_file_lines(relpath: str, lines: List[str], file_hash: str,
                           window_lines: int = 50, overlap_lines: int = 15):
    """Yield byte-bounded line windows. Indivisible long lines have no candidate."""
    start = 0
    while start < len(lines):
        end, byte_count = start, 0
        while end < len(lines) and end - start < window_lines:
            line_bytes = len(json.dumps(lines[end], ensure_ascii=True).encode("utf-8"))
            if byte_count + line_bytes > MAX_CHUNK_SERIALIZED_BYTES:
                break
            byte_count += line_bytes
            end += 1
        if end == start:
            start += 1
            continue
        text = "".join(lines[start:end])
        yield CandidateChunk(relpath, start + 1, end, text,
                             hashlib.sha256(text.encode()).hexdigest(), file_hash)
        if end >= len(lines):
            break
        count = end - start
        overlap = min(overlap_lines, count * overlap_lines // max(1, window_lines))
        start += max(1, count - overlap)


def chunk_file_lines(relpath: str, lines: List[str], file_hash: str,
                     window_lines: int = 50, overlap_lines: int = 15) -> List[CandidateChunk]:
    return list(_iter_chunk_file_lines(relpath, lines, file_hash, window_lines, overlap_lines))


def compute_lexical_score(query_terms: List[str], relpath: str, text: str) -> float:
    """Compute lexical score for candidate chunk ordering and local fallback."""
    if not query_terms:
        return 0.0

    score = 0.0
    text_lower = text.lower()
    path_lower = relpath.lower()

    for term in query_terms:
        # Path match bonus
        if term in path_lower:
            score += 5.0
        # Text occurrences
        count = text_lower.count(term)
        if count > 0:
            score += 1.0 + math.log1p(count)

    # Exact query substring bonus
    full_query = " ".join(query_terms)
    if full_query in text_lower:
        score += 10.0

    return score


def extract_query_terms(query: str) -> List[str]:
    """Extract lowercased query terms excluding stopwords."""
    raw_tokens = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
    return [t for t in raw_tokens if t not in STOPWORDS]


MAX_ENUMERATED_FILES = 4000
MAX_TOTAL_READ_BYTES = 12 * 1024 * 1024
MAX_SKIPPED_DETAILS = 24
INVENTORY_DEADLINE_SECONDS = 10
INVENTORY_CLEANUP_SECONDS = 2


def _inventory_command(cmd: List[str], max_paths: int = MAX_ENUMERATED_FILES,
                       cwd: Optional[Path] = None, deadline: Optional[float] = None) -> Tuple[List[bytes], bool, bool]:
    """Scan to one shared deadline, then allow a bounded cleanup grace."""
    import threading
    paths, pending = [], b""
    incomplete = failed = False
    stopped = threading.Event()
    state_lock = threading.Lock()
    deadline = deadline if deadline is not None else time.monotonic() + 10
    if time.monotonic() >= deadline or max_paths <= 0:
        return [], True, True
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return [], True, False

    def consume():
        nonlocal pending, incomplete, failed
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                with state_lock:
                    if stopped.is_set():
                        break
                    pending += chunk
                    if len(pending) > 32768 and b"\0" not in pending:
                        incomplete = True
                        break
                    parts = pending.split(b"\0")
                    pending = parts.pop()
                    for part in parts:
                        if part:
                            paths.append(part)
                        if len(paths) >= max_paths:
                            incomplete = True
                            break
                    if incomplete:
                        break
        except (OSError, ValueError):
            with state_lock:
                failed = True
        finally:
            # The reader owns the stream. Closing it from the caller could
            # block behind an unfinished read even after the deadline.
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                with state_lock:
                    failed = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(max(0, deadline - time.monotonic()))
    cleanup_deadline = time.monotonic() + INVENTORY_CLEANUP_SECONDS
    with state_lock:
        should_kill = reader.is_alive() or incomplete
        if should_kill:
            incomplete = True
            stopped.set()
    if should_kill:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        code = proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        with state_lock:
            incomplete = True
            stopped.set()
        try:
            proc.kill()
        except OSError:
            pass
        # A second timeout is a bounded cleanup failure, not an exception to caller.
        try:
            code = proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            code = None
            with state_lock:
                failed = True
    reader.join(max(0, cleanup_deadline - time.monotonic()))
    valid = code in (0, 1) if Path(cmd[0]).name.lower().startswith("rg") else code == 0
    with state_lock:
        stopped.set()
        if reader.is_alive():
            incomplete = failed = True
        return list(paths), incomplete, (valid or incomplete) and not failed


def _rg_base(rg: str) -> List[str]:
    cmd = [rg, "--files", "--hidden", "--no-require-git", "--sort", "path", "-0"]
    for dirname in sorted(EXCLUDED_DIR_NAMES):
        cmd += ["-g", f"!**/{dirname}/**", "-g", f"!{dirname}/**"]
    for pattern in (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.pkcs12",
                    "id_rsa*", "id_ed25519*", "id_dsa*", "id_ecdsa*",
                    "credentials.json", "secrets.json", "token.json", ".npmrc", ".netrc", ".pypirc", ".git-credentials"):
        cmd += ["-g", f"!{pattern}"]
    return cmd


def enumerate_files(workspace: Path, scopes: List[str]) -> Tuple[List[Path], List[Dict[str, str]], List[str]]:
    """Ignore-aware NUL inventory; errors never trigger an unsafe manual walk."""
    files, skipped, reasons = [], [], []
    if len(scopes) > 32 or any(not isinstance(scope, str) or len(scope) > 240 for scope in scopes):
        return [], [], ["scope_limit"]
    workspace = Path(os.path.abspath(workspace))
    if not is_safe_contained_path(workspace, workspace) or not workspace.is_dir():
        return [], [], ["invalid_workspace"]
    rg, git = shutil.which("rg"), shutil.which("git")
    seen = set()
    inventory_deadline = time.monotonic() + INVENTORY_DEADLINE_SECONDS
    for scope in scopes or ["."]:
        if time.monotonic() >= inventory_deadline:
            reasons.append("inventory_incomplete")
            break
        scope_path = Path(scope)
        if scope_path.is_absolute() or ".." in scope_path.parts:
            reasons.append("scope_escape_or_symlink_rejected")
            if len(skipped) < MAX_SKIPPED_DETAILS:
                skipped.append({"path": str(scope)[:160], "reason": "scope_escape_or_symlink"})
            continue
        target = workspace / scope
        if not is_safe_contained_path(target, workspace):
            reasons.append("scope_escape_or_symlink_rejected")
            if len(skipped) < MAX_SKIPPED_DETAILS:
                skipped.append({"path": str(scope)[:160], "reason": "scope_escape_or_symlink"})
            continue
        if not target.exists():
            reasons.append("scope_not_found")
            continue
        rel_target = target.relative_to(workspace)
        if any(part in EXCLUDED_DIR_NAMES for part in rel_target.parts) or is_file_excluded(target.name):
            reasons.append("excluded_scope")
            continue
        if target.is_file():
            if not rg:
                reasons.append("scope_file_inventory_unavailable")
                continue
            # Only one directory level, so an explicit root file never inventories the repository.
            cmd = _rg_base(rg) + ["--max-depth", "1", str(target.parent)]
            names, incomplete, valid = _inventory_command(cmd, MAX_ENUMERATED_FILES, cwd=workspace, deadline=inventory_deadline)
            if incomplete:
                reasons.append("inventory_incomplete")
            if not valid or os.fsencode(str(target)) not in names:
                reasons.append("scope_file_ignored_or_unavailable")
                continue
            names = [os.fsencode(str(target))]
            base = workspace
        elif target.is_dir():
            if rg:
                cmd, base = _rg_base(rg) + [str(target)], workspace
            elif git and (workspace / ".git").exists():
                cmd = [git, "-C", str(workspace), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", str(rel_target)]
                base = workspace
            else:
                reasons.append("inventory_tool_unavailable")
                continue
            names, incomplete, valid = _inventory_command(cmd, MAX_ENUMERATED_FILES - len(seen), cwd=workspace, deadline=inventory_deadline)
            if incomplete:
                reasons.append("inventory_incomplete")
            if not valid:
                reasons.append("inventory_error")
                continue
        else:
            reasons.append("nonregular_scope")
            continue
        for name in names:
            path = base / os.fsdecode(name)
            if len(seen) >= MAX_ENUMERATED_FILES:
                reasons.append("file_enumeration_limit")
                break
            if not is_safe_contained_path(path, workspace):
                reasons.append("linked_or_escaped_file")
                continue
            rel = path.relative_to(workspace)
            if any(part in EXCLUDED_DIR_NAMES for part in rel.parts) or is_file_excluded(path.name):
                continue
            if not safe_regular_file(path, workspace) or contains_literal_secret(str(rel).encode()):
                reasons.append("nonregular_linked_or_secret_path")
                continue
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files, skipped, sorted(set(reasons))


def scan_candidates(workspace: Path, scopes: List[str], query: str, max_candidates: int = 96,
                    max_file_size_bytes: int = 512 * 1024) -> Tuple[List[CandidateChunk], List[Dict[str, str]], List[str]]:
    files, skipped, reasons = enumerate_files(workspace, scopes)
    terms, candidates, total_read = extract_query_terms(query), [], 0
    for path in files:
        rel = str(path.relative_to(workspace)).replace("\\", "/")
        try:
            if not safe_regular_file(path, workspace):
                reasons.append("linked_or_changed_file")
                continue
            size = path.stat().st_size
            if size > max_file_size_bytes or total_read + size > MAX_TOTAL_READ_BYTES:
                reasons.append("file_size_exceeded" if size > max_file_size_bytes else "total_read_limit")
                if len(skipped) < MAX_SKIPPED_DETAILS:
                    skipped.append({"path": rel[:160], "reason": "oversized_file" if size > max_file_size_bytes else "total_read_limit"})
                continue
            with path.open("rb") as f:
                opened_stat = os.fstat(f.fileno())
                content = f.read(max_file_size_bytes + 1)
            if not safe_regular_file(path, workspace) or (opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size) != (path.stat().st_dev, path.stat().st_ino, path.stat().st_size):
                reasons.append("linked_or_changed_file")
                continue
            total_read += len(content)
            if len(content) > max_file_size_bytes:
                reasons.append("file_size_exceeded")
                continue
            if b"\0" in content[:8192]:
                reason = "binary_content"
            elif contains_literal_secret(content):
                reason = "detected_literal_secret"
            else:
                try:
                    decoded = content.decode("utf-8")
                except UnicodeError:
                    reason = "non_utf8"
                else:
                    reason = None
            if reason:
                reasons.append(reason)
                if len(skipped) < MAX_SKIPPED_DETAILS:
                    skipped.append({"path": rel[:160], "reason": reason})
                continue
            file_hash = hashlib.sha256(content).hexdigest()
            lines = decoded.splitlines(keepends=True)
            if any(len(json.dumps(line, ensure_ascii=True).encode("utf-8")) > MAX_CHUNK_SERIALIZED_BYTES for line in lines):
                reasons.append("long_source_line_omitted")
            for chunk in _iter_chunk_file_lines(rel, lines, file_hash):
                chunk.lexical_score = compute_lexical_score(terms, rel, chunk.text)
                if len(candidates) < max_candidates:
                    candidates.append(chunk)
                else:
                    # Bounded shortlist; keep stronger lexical hits while retaining scan coverage caveat.
                    weakest = min(range(len(candidates)), key=lambda i: candidates[i].lexical_score)
                    if chunk.lexical_score > candidates[weakest].lexical_score:
                        candidates[weakest] = chunk
                    reasons.append("max_candidates_reached")
        except OSError:
            reasons.append("read_error")
    candidates.sort(key=lambda c: c.lexical_score, reverse=True)
    return candidates, skipped, sorted(set(reasons))


class ScoreCache:
    """Small score-only cache; link ancestors are checked before each access."""
    MAX_CACHE_BYTES = 1024 * 1024

    def __init__(self, workspace: Path, cache_dir: Optional[Path] = None, ttl_seconds: float = 604800.0):
        self.workspace = workspace
        self.cache_dir = cache_dir or workspace / ".llm-output" / "jev-cache"
        self.cache_file = self.cache_dir / "scores.json"
        self.ttl_seconds = ttl_seconds
        self._loaded = None

    def compute_key(self, relpath, start_line, end_line, content_hash, query, rubric, model):
        ident = json.dumps([str(self.workspace), relpath, start_line, end_line, content_hash, query, rubric, model])
        return hashlib.sha256(ident.encode()).hexdigest()

    def _safe(self):
        return is_safe_contained_path(self.cache_dir, self.workspace) and is_safe_contained_path(self.cache_file, self.workspace)

    def _load(self):
        if self._loaded is not None:
            return self._loaded
        self._loaded = {}
        if not self._safe() or not self.cache_file.exists():
            return self._loaded
        try:
            if not safe_regular_file(self.cache_file, self.workspace):
                return self._loaded
            with self.cache_file.open("rb") as f:
                raw = f.read(self.MAX_CACHE_BYTES + 1)
            if len(raw) <= self.MAX_CACHE_BYTES:
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._loaded = {k: v for k, v in list(data.items())[:10000] if isinstance(k, str) and len(k) == 64}
        except (OSError, ValueError, UnicodeError):
            pass
        return self._loaded

    def get_score(self, cache_key):
        entry = self._load().get(cache_key)
        if not isinstance(entry, dict):
            return None
        score, ts = entry.get("noul"), entry.get("timestamp")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            return None
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts > time.time() + 60 or time.time() - ts > self.ttl_seconds:
            return None
        return float(score)

    def store_scores(self, new_entries):
        if not new_entries or not self._safe():
            return
        tmp = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            if not self._safe():
                return
            data = self._load().copy()
            now = time.time()
            for key, score in new_entries.items():
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                    continue
                data[key] = {"noul": float(score), "timestamp": now}
            data = {key: entry for key, entry in data.items()
                    if isinstance(entry, dict) and isinstance(entry.get("timestamp"), (int, float))
                    and not isinstance(entry.get("timestamp"), bool) and math.isfinite(entry["timestamp"])
                    and now - entry["timestamp"] <= self.ttl_seconds and entry["timestamp"] <= now + 60}
            ordered = sorted(data.items(), key=lambda pair: pair[1]["timestamp"], reverse=True)
            data = {}
            byte_count = 2  # JSON braces
            for key, entry in ordered:
                record = json.dumps(key, separators=(",", ":")) + ":" + json.dumps(entry, separators=(",", ":"))
                addition = len(record.encode()) + (1 if data else 0)
                if len(data) >= 10000 or byte_count + addition > self.MAX_CACHE_BYTES:
                    break
                data[key] = entry
                byte_count += addition
            raw = json.dumps(data, separators=(",", ":")).encode()
            import tempfile
            fd, name = tempfile.mkstemp(prefix="scores-", suffix=".tmp", dir=self.cache_dir)
            tmp = Path(name)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            if self._safe():
                os.replace(tmp, self.cache_file)
                self._loaded = data
        except (OSError, ValueError):
            pass
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def verify_source_integrity(workspace: Path, candidate: CandidateChunk) -> bool:
    target = workspace / candidate.relpath
    if not safe_regular_file(target, workspace):
        return False
    try:
        with target.open("rb") as f:
            content = f.read(512 * 1024 + 1)
        return len(content) <= 512 * 1024 and not contains_literal_secret(content) and hashlib.sha256(content).hexdigest() == candidate.file_hash
    except OSError:
        return False


def deduplicate_and_bound_results(
    candidates: List[CandidateChunk],
    top_k: int,
    max_output_chars: int,
    omissions: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Deduplicate overlapping line ranges from the same file and enforce total output char budget."""
    # Deduplicate overlapping ranges per file
    deduped: List[CandidateChunk] = []
    for cand in candidates:
        overlap = False
        for kept in deduped:
            if kept.relpath == cand.relpath:
                # Overlap condition: start_A <= end_B and start_B <= end_A
                if cand.start_line <= kept.end_line and kept.start_line <= cand.end_line:
                    overlap = True
                    break
        if not overlap:
            deduped.append(cand)

    results: List[Dict[str, Any]] = []
    total_chars = 0

    for cand in deduped:
        if len(results) >= top_k:
            break

        cand_len = len(cand.text)
        if total_chars + cand_len <= max_output_chars:
            results.append({
                "path": cand.relpath,
                "start_line": cand.start_line,
                "end_line": cand.end_line,
                "score": cand.score,
                "unscored": cand.unscored,
                "unknown_reason": cand.unknown_reason,
                "excerpt": cand.text,
                "content_hash": cand.content_hash,
                "file_hash": cand.file_hash,
            })
            total_chars += cand_len
        else:
            if omissions is not None:
                omissions.append("output_budget_truncated")
            remaining_budget = max_output_chars - total_chars
            if remaining_budget >= 100:
                # Truncate lines to fit remaining budget
                lines = cand.text.splitlines(keepends=True)
                truncated_lines = []
                cur = 0
                for line in lines:
                    if cur + len(line) <= remaining_budget:
                        truncated_lines.append(line)
                        cur += len(line)
                    else:
                        break
                if truncated_lines:
                    results.append({
                        "path": cand.relpath,
                        "start_line": cand.start_line,
                        "end_line": cand.start_line + len(truncated_lines) - 1,
                        "score": cand.score,
                        "unscored": cand.unscored,
                        "unknown_reason": cand.unknown_reason,
                        "excerpt": "".join(truncated_lines),
                        "content_hash": hashlib.sha256("".join(truncated_lines).encode()).hexdigest(),
                        "file_hash": cand.file_hash,
                        "truncated": True,
                    })
                    total_chars += cur
            if total_chars >= max_output_chars:
                break
            continue

    return results


def run_doctor(workspace: Path, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Run offline doctor diagnostics without reading keys or contacting remote."""
    ws = Path(os.path.abspath(workspace))
    ws_valid = ws.is_dir() and is_safe_contained_path(ws, ws)
    config, cfg_src = load_config(config_path)

    api_key_present = bool(os.environ.get("TYPESAFE_API_KEY", "").strip())
    remote_auth = is_remote_authorized(ws, config, allow_remote=False)

    cache_dir = ws / ".llm-output" / "jev-cache"
    cache_writable = False
    try:
        cache_writable = ws_valid and is_safe_contained_path(cache_dir, ws) and is_safe_contained_path(cache_dir / "scores.json", ws)
    except Exception:
        cache_writable = False

    status = "complete" if ws_valid else "unavailable"

    return {
        "command": "doctor",
        "status": status,
        "workspace": str(ws),
        "workspace_valid": ws_valid,
        "config_source": cfg_src,
        "enabled": config.get("enabled", False),
        "allowed_roots": config.get("allowed_roots", []),
        "remote_authorized": remote_auth,
        "api_key_present": api_key_present,
        "rg_available": bool(shutil.which("rg")),
        "git_available": bool(shutil.which("git")),
        "cache_dir": str(cache_dir),
        "cache_dir_writable": cache_writable,
    }


def run_inspect(workspace: Path, scopes: List[str]) -> Dict[str, Any]:
    """Enumerate candidate files and line windows without contacting remote."""
    ws = Path(os.path.abspath(workspace))
    if not ws.is_dir() or not is_safe_contained_path(ws, ws):
        return {
            "command": "inspect",
            "status": "error",
            "workspace": str(ws),
            "error": "Workspace directory does not exist or is not a directory",
            "coverage": {"complete": False, "reasons": ["workspace_not_found"]},
        }

    candidates, skipped_files, coverage_reasons = scan_candidates(ws, scopes, query="", max_candidates=DEFAULT_CONFIG["max_candidates"])
    status = "complete" if not coverage_reasons else "partial"

    return {
        "command": "inspect",
        "status": status,
        "workspace": str(ws),
        "scopes": [scope[:240] for scope in scopes[:32]] or ["."],
        "files_scanned": len(set(c.relpath for c in candidates)),
        "candidate_count": len(candidates),
        "skipped_files": skipped_files,
        "coverage": {
            "complete": len(coverage_reasons) == 0,
            "reasons": list(set(coverage_reasons)),
        },
    }


def run_search(
    workspace: Path,
    query: str,
    scopes: Optional[List[str]] = None,
    top_k: int = 6,
    max_output_chars: Optional[int] = None,
    allow_remote: bool = False,
    config_path: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    client: Optional[JevClient] = None,
) -> Dict[str, Any]:
    """Execute bounded semantic search or local deterministic fallback."""
    ws = Path(os.path.abspath(workspace))
    scopes = scopes or []
    if not isinstance(query, str) or not query.strip() or len(query) > 4000 or contains_literal_secret(query.encode("utf-8")):
        return {"command": "search", "status": "error", "error": "Invalid or sensitive query"}

    if not ws.is_dir() or not is_safe_contained_path(ws, ws):
        return {
            "command": "search",
            "status": "error",
            "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "workspace": str(ws),
            "results": [],
            "error": "Workspace directory does not exist",
            "coverage": {"complete": False, "reasons": ["invalid_workspace"]},
        }

    try:
        config, cfg_src = load_config(config_path)
    except Exception as e:
        return {
            "command": "search",
            "status": "error",
            "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "workspace": str(ws),
            "results": [],
            "error": "Invalid configuration",
            "coverage": {"complete": False, "reasons": ["config_error"]},
        }

    if not isinstance(query, str) or not query.strip() or len(query) > 4000 or contains_literal_secret(query.encode("utf-8")):
        return {"command": "search", "status": "error", "error": "Invalid or sensitive query"}
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        return {"command": "search", "status": "error", "error": "Invalid top-k"}
    out_chars_bound = max_output_chars if max_output_chars is not None else config["max_output_chars"]
    if isinstance(out_chars_bound, bool) or not isinstance(out_chars_bound, int) or not 100 <= out_chars_bound <= 20000:
        return {"command": "search", "status": "error", "error": "Invalid output limit"}
    model = config["model"]
    cache = ScoreCache(ws, cache_dir=cache_dir, ttl_seconds=config["cache_ttl_seconds"])

    # Scan and rank candidates
    candidates, skipped_files, coverage_reasons = scan_candidates(
        ws,
        scopes,
        query=query,
        max_candidates=config["max_candidates"],
    )

    if not candidates:
        return {
            "command": "search",
            "status": "partial" if coverage_reasons else "complete",
            "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "workspace": str(ws),
            "model": model,
            "top_k": top_k,
            "results": [],
            "stats": {
                "files_scanned": 0,
                "candidates_total": 0,
                "candidates_scored": 0,
                "cache_hits": 0,
                "requests_made": 0,
                "estimated_input_tokens": 0,
                "actual_input_tokens": 0,
                "output_tokens": 0,
            },
            "coverage": {
                "complete": len(coverage_reasons) == 0,
                "reasons": list(set(coverage_reasons)),
            },
        }

    # Authorization and API Key readiness
    authorized = is_remote_authorized(ws, config, allow_remote)
    api_key_set = bool(os.environ.get("TYPESAFE_API_KEY", "").strip()) or (client is not None and client.transport is not None)

    # Local fallback if remote not authorized or missing key
    if not authorized or not api_key_set:
        reason = "remote_disabled" if not authorized else "missing_api_key"
        coverage_reasons.append(reason)

        # Candidates already ordered by lexical score
        for c in candidates:
            c.score = None
            c.unscored = True
            c.unknown_reason = reason

        # Re-verify source integrity
        verified_files = {}
        valid_candidates = []
        for c in candidates:
            if c.relpath not in verified_files:
                verified_files[c.relpath] = verify_source_integrity(ws, c)
            if verified_files[c.relpath]:
                valid_candidates.append(c)
            else:
                coverage_reasons.append("stale_source_dropped")

        results = deduplicate_and_bound_results(valid_candidates, top_k, out_chars_bound, coverage_reasons)

        return {
            "command": "search",
            "status": "unavailable",
            "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "workspace": str(ws),
            "model": model,
            "top_k": top_k,
            "results": results,
            "stats": {
                "files_scanned": len(set(c.relpath for c in candidates)),
                "candidates_total": len(candidates),
                "candidates_scored": 0,
                "cache_hits": 0,
                "requests_made": 0,
                "estimated_input_tokens": 0,
                "actual_input_tokens": 0,
                "output_tokens": 0,
            },
            "coverage": {
                "complete": False,
                "reasons": list(set(coverage_reasons)),
            },
        }

    # Remote search is authorized and key is available
    state_prompt = RUBRIC_TEMPLATE.format(query=query)
    cache_hits = 0
    new_cache_entries: Dict[str, float] = {}
    to_evaluate: Dict[str, Tuple[CandidateChunk, str]] = {}

    for idx, c in enumerate(candidates):
        ckey = cache.compute_key(
            relpath=c.relpath,
            start_line=c.start_line,
            end_line=c.end_line,
            content_hash=c.content_hash,
            query=query,
            rubric=RUBRIC_LAYOUT_VERSION,
            model=model,
        )
        cached_score = cache.get_score(ckey)
        if cached_score is not None:
            c.score = cached_score
            c.unscored = False
            cache_hits += 1
        else:
            qid = f"c_{idx}"
            to_evaluate[qid] = (c, ckey)

    requests_made = 0
    est_tokens = 0
    actual_in_tokens = 0
    actual_out_tokens = 0
    client_completed = True
    stop_reason = None

    if to_evaluate:
        if client is None:
            client = JevClient(
                model=model,
                endpoint=DEFAULT_ENDPOINT,
                timeout_seconds=config["timeout_seconds"],
                deadline_seconds=config["deadline_seconds"],
                max_requests=config["max_requests"],
            )

        questions_payload = {}
        for qid, (c, _) in to_evaluate.items():
            questions_payload[qid] = {
                "type": "noul",
                "instructions": (
                    f"File: {c.relpath}:{c.start_line}-{c.end_line}\n\n"
                    f"```\n{c.text}\n```\n\n"
                    f"Assess whether this code excerpt is directly relevant to the query."
                ),
                "criteria": {
                    "true": "The excerpt contains direct implementation, definition, or primary logic relevant to the query.",
                    "false": "The excerpt is irrelevant, only mentions related terms incidentally, or lacks substantive logic for the query.",
                },
            }

        eval_res = client.evaluate_questions(
            state=state_prompt,
            questions=questions_payload,
            max_input_tokens=config["max_input_tokens"],
        )

        requests_made = eval_res["requests_made"]
        est_tokens = eval_res["estimated_input_tokens"]
        actual_in_tokens = eval_res["usage"]["input_tokens"]
        actual_out_tokens = eval_res["usage"]["output_tokens"]
        client_completed = eval_res["completed"]
        stop_reason = eval_res.get("stop_reason")

        if stop_reason:
            coverage_reasons.append(stop_reason)
        for qid, why in eval_res.get("unknown_reasons", {}).items():
            if qid in to_evaluate:
                to_evaluate[qid][0].unknown_reason = why

        for qid, ans in eval_res["answers"].items():
            if qid in to_evaluate:
                cand, ckey = to_evaluate[qid]
                score = ans.get("noul")
                if score is not None:
                    cand.score = float(score)
                    cand.unscored = False
                    new_cache_entries[ckey] = float(score)

        if new_cache_entries:
            cache.store_scores(new_cache_entries)

    if any(c.score is None for c in candidates):
        coverage_reasons.append("unscored_candidates")
    # Sort candidates: scored candidates first by score descending, then unscored by lexical score
    def sort_key(c: CandidateChunk):
        if c.score is not None:
            return (1, c.score, c.lexical_score)
        return (0, 0.0, c.lexical_score)

    candidates.sort(key=sort_key, reverse=True)

    # Re-verify source integrity
    verified_files = {}
    valid_candidates = []
    stale_count = 0
    for c in candidates:
        if c.relpath not in verified_files:
            verified_files[c.relpath] = verify_source_integrity(ws, c)
        if verified_files[c.relpath]:
            valid_candidates.append(c)
        else:
            stale_count += 1

    if stale_count > 0:
        coverage_reasons.append("stale_source_dropped")

    results = deduplicate_and_bound_results(valid_candidates, top_k, out_chars_bound, coverage_reasons)

    # Determine status
    if not client_completed:
        status = "partial"
    elif stale_count > 0:
        status = "partial"
    elif coverage_reasons:
        status = "partial"
    else:
        status = "complete"

    scored_count = sum(1 for c in candidates if c.score is not None)

    return {
        "command": "search",
        "status": status,
        "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "workspace": str(ws),
        "model": model,
        "top_k": top_k,
        "results": results,
        "stats": {
            "files_scanned": len(set(c.relpath for c in candidates)),
            "candidates_total": len(candidates),
            "candidates_scored": scored_count,
            "cache_hits": cache_hits,
            "requests_made": requests_made,
            "estimated_input_tokens": est_tokens,
            "actual_input_tokens": actual_in_tokens,
            "output_tokens": actual_out_tokens,
        },
        "coverage": {
            "complete": (status == "complete"),
            "reasons": list(set(coverage_reasons)),
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Build canonical argparse parser with documented help."""
    parser = argparse.ArgumentParser(
        description="TypeSafe Jev bounded semantic code search helper.",
        prog="jev_search",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True, help="Subcommands")

    # doctor
    doctor_p = subparsers.add_parser("doctor", help="Inspect environment, config, and API readiness offline.")
    doctor_p.add_argument("--workspace", required=True, type=str, help="Workspace root path.")
    doctor_p.add_argument("--config", type=str, help="Optional path to framework routing config JSON.")

    # inspect
    inspect_p = subparsers.add_parser("inspect", help="Enumerate and chunk candidate files offline.")
    inspect_p.add_argument("--workspace", required=True, type=str, help="Workspace root path.")
    inspect_p.add_argument("--scope", action="append", default=[], help="Repeatable relative scope within workspace.")

    # search
    search_p = subparsers.add_parser("search", help="Perform bounded semantic search with local fallback.")
    search_p.add_argument("--workspace", required=True, type=str, help="Workspace root path.")
    search_p.add_argument("--query", type=str, help="Search query string.")
    search_p.add_argument("--query-file", type=str, help="File containing search query string.")
    search_p.add_argument("--scope", action="append", default=[], help="Repeatable relative scope within workspace.")
    search_p.add_argument("--top-k", type=int, default=6, help="Maximum number of excerpts to return (default 6).")
    search_p.add_argument("--max-output-chars", type=int, help="Maximum total excerpt characters returned.")
    search_p.add_argument("--allow-remote", action="store_true", help="Explicitly authorize remote Jev API invocation.")
    search_p.add_argument("--config", type=str, help="Optional path to framework routing config JSON.")

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    workspace_path = Path(args.workspace)

    try:
        res = _dispatch(args, workspace_path)
    except Exception:
        res = {"command": args.subcommand, "status": "error", "error": "Invalid input or processing failure"}
    sys.stdout.write(json.dumps(res, separators=(",", ":"), sort_keys=True) + "\n")
    if res.get("status") == "error":
        sys.exit(2)


def _dispatch(args, workspace_path):
    if args.subcommand == "doctor":
        res = run_doctor(workspace_path, config_path=args.config)
    elif args.subcommand == "inspect":
        res = run_inspect(workspace_path, scopes=args.scope)
    elif args.subcommand == "search":
        query_text = args.query or ""
        if args.query_file:
            try:
                qf = Path(args.query_file)
                if not safe_regular_file(qf, workspace_path) or qf.stat().st_size > 4000:
                    raise ValueError("Invalid query file")
                file_query = qf.read_text(encoding="utf-8")
                query_text = f"{query_text} {file_query}".strip()
            except (OSError, UnicodeError, ValueError):
                query_text = ""
        if not query_text:
            res = {
                "command": "search",
                "status": "error",
                "error": "Either --query or --query-file must provide non-empty query",
                "coverage": {"complete": False, "reasons": ["empty_query"]},
            }
        else:
            res = run_search(
                workspace=workspace_path,
                query=query_text,
                scopes=args.scope,
                top_k=args.top_k,
                max_output_chars=args.max_output_chars,
                allow_remote=args.allow_remote,
                config_path=args.config,
            )
    else:
        res = {"status": "error", "error": f"Unknown subcommand {args.subcommand}"}

    return res


if __name__ == "__main__":
    main()
