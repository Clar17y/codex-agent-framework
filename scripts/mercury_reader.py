#!/usr/bin/env python3
"""Bounded read-only corpus reader for Mercury 3 exploration pilot.

Confined strictly to corpus root, excluding .git, .llm-output, output, vendor, .worktrees, node_modules.
Blocks symlink/reparse/hardlink escape, credential files, and binary files.
Provides CLI and importable functions:
  list_files(root, prefix)
  search(root, query, glob_pattern, limit)
  read_file(root, path, start, end)
"""

import argparse
from contextlib import contextmanager, ExitStack
import errno
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

LISTINGS_MAX_PATHS = 150
SEARCH_MAX_MATCHES = 60
SEARCH_MAX_CHARS = 18000
READ_MAX_LINES = 200
READ_MAX_CHARS = 24000
FILE_MAX_BYTES = 2 * 1024 * 1024
SCAN_MAX_ENTRIES = 10000
SCAN_MAX_BYTES = 16 * 1024 * 1024
SCAN_MAX_LINES = 100000
LINE_MAX_BYTES = 32768
READER_TIMEOUT_SECONDS = 15.0


class _CredentialBlocks:
    """Filter credential blocks before a reader selects ranges or matches."""

    assignment = re.compile(
        r'''(?ix)["']?\b[\w-]*(?:api[_-]?key|secret|password|passwd|token|credential|authorization)[\w-]*\b["']?[ \t]*[:=][ \t]*(.*)''')

    def __init__(self):
        self.quote = None
        self.quote_yaml = False
        self.indent = None
        self.pending = False
        self.pending_yaml = False
        self.pending_indent = 0
        self.plain_indent = None
        self.pending_comment = False
        self.container = []
        self.container_yaml = False
        self.container_quote = None
        self.container_comment = False

    @staticmethod
    def closing(body, quote, *, yaml=False):
        offset = 0
        while True:
            pos = body.find(quote, offset)
            if pos < 0:
                return -1
            if yaml and quote == "'":
                if body.startswith("''", pos):
                    offset = pos + 2
                    continue
                return pos
            preceding = len(body[:pos]) - len(body[:pos].rstrip('\\'))
            if preceding % 2 == 0:
                return pos
            offset = pos + len(quote)

    @staticmethod
    def opening(value, yaml=False):
        if value.startswith(('\\\"', "\\\'")):
            return value[:2], 2, False
        # YAML's three initial apostrophes are an opening quote plus an escaped
        # apostrophe, not a Python triple-quoted string.
        if yaml and value.startswith("'"):
            return "'", 1, True
        match = re.match(r"""(?i)[rubf]{0,2}("{3}|'{3}|"|')""", value)
        if match:
            return match[1], match.end(), yaml and value[0] in ('"', "'")
        return None

    @staticmethod
    def field_start(value):
        # YAML requires separation after ':'. URLs and colon/equals-bearing
        # scalar text inside an indented value are not sibling declarations.
        return re.match(r"""(?:-[ \t]+)?(?:["'][^"']+["']|[\w.-]+)[ \t]*(?::(?:[ \t]|$)|=(?!=))""", value)

    def line(self, line):
        body = line.rstrip('\r\n')
        ending = line[len(body):]
        if self.container:
            return self.container_value(body, ending)
        if self.quote:
            close = self.closing(body, self.quote, yaml=self.quote_yaml)
            if close < 0:
                return '[REDACTED]' + ending
            quote, self.quote = self.quote, None
            return '[REDACTED]' + quote + self.quote_tail(body[close + len(quote):], ending, yaml=self.quote_yaml, adjacent=len(quote) != 2)
        if self.plain_indent is not None:
            stripped = body.lstrip(' \t')
            indent = len(body) - len(stripped)
            if not stripped:
                return ending
            if (indent > self.plain_indent or
                    (not self.field_start(stripped) and not stripped.startswith(('}', ']', ','))
                     and stripped not in ('---', '...'))):
                return body[:indent] + '[REDACTED]' + ending
            self.plain_indent = None
        if self.indent is not None:
            indent = len(body) - len(body.lstrip(' \t'))
            if not body.strip():
                return ending
            if indent > self.indent:
                return body[:indent] + '[REDACTED]' + ending
            self.indent = None
        if self.pending:
            offset = 0
            while True:
                if self.pending_comment:
                    close = body.find('*/', offset)
                    if close < 0:
                        return line
                    self.pending_comment = False
                    offset = close + 2
                offset += len(body[offset:]) - len(body[offset:].lstrip(' \t'))
                if not body.startswith('/*', offset):
                    break
                self.pending_comment = True
                offset += 2
            value = body[offset:]
            if not value or value.startswith(('#', '//')):
                return line
            self.pending = False
            # Empty/malformed assignments must not consume the next field or
            # structural closer. A real value is filtered before selection.
            indent = len(body) - len(body.lstrip(' \t'))
            if (not value.startswith(('}', ']', ',')) and value not in ('---', '...') and
                    (indent > self.pending_indent or not self.field_start(value))):
                return self.value(body[:offset], value, ending, pending=True,
                                  plain_indent=self.pending_indent, yaml=self.pending_yaml)
        match = self.assignment.search(body)
        if not match:
            return line
        value = match[1]
        prefix = body[:match.start(1)]
        indent = len(body) - len(body.lstrip(' \t'))
        key_indent = match.start() if body[:match.start()].strip() == '-' else indent
        self.pending_indent = key_indent
        yaml_scalar = prefix.rstrip().endswith(':') and body[:match.start()].strip() in ('', '-')
        self.pending_yaml = yaml_scalar
        if not value or value.startswith(('#', '//')):
            self.pending = True
            return line
        if value.startswith('/*'):
            self.pending = True
            return prefix + self.line(value) + ending
        return self.value(prefix, value, ending, plain_indent=key_indent if yaml_scalar else None, yaml=yaml_scalar)

    def value(self, prefix, value, ending, *, pending=False, plain_indent=None, yaml=False):
        if re.fullmatch(r'[|>][1-9+\-]*[ \t]*(?:#.*)?', value):
            self.indent = len(prefix) - len(prefix.lstrip(' \t'))
            return prefix + value + ending
        if value.startswith(('[', '{', '(')):
            self.container_yaml = yaml
            return prefix + self.container_value(value, ending)
        if self.opening(value, yaml):
            return self.quoted_value(prefix, value, ending, yaml=yaml)
        if plain_indent is not None:
            self.plain_indent = plain_indent
        # Inline bare values retain the scalar sanitizer below. Detached plain
        # values and their continuation subtree stay hidden until a real field
        # or structural/document boundary at the credential key's indentation.
        return prefix + ('[REDACTED]' if pending or plain_indent is not None else value) + ending

    def quote_tail(self, tail, ending, *, yaml=False, adjacent=True):
        if not yaml and adjacent:
            if re.fullmatch(r'[ \t]*\\[ \t]*', tail):
                self.pending = True
                self.pending_yaml = False
                return tail + ending
            offset = len(tail) - len(tail.lstrip(' \t'))
            if self.opening(tail[offset:]):
                return self.quoted_value(tail[:offset], tail[offset:], ending)
        return self.line(tail) + ending

    def quoted_value(self, prefix, value, ending, *, yaml=False):
        parts = []
        while True:
            quote, end, quote_yaml = self.opening(value, yaml)
            parts.extend((value[:end], '[REDACTED]'))
            rest = value[end:]
            close = self.closing(rest, quote, yaml=quote_yaml)
            if close < 0:
                self.quote, self.quote_yaml = quote, quote_yaml
                return prefix + ''.join(parts) + ending
            parts.append(quote)
            tail = rest[close + len(quote):]
            offset = len(tail) - len(tail.lstrip(' \t'))
            if not quote_yaml and len(quote) != 2 and self.opening(tail[offset:]):
                # Adjacent literals belong to the same recognized value.
                # Iterate without recursive depth proportional to literal count.
                parts.append(tail[:offset])
                value = tail[offset:]
                continue
            return prefix + ''.join(parts) + self.quote_tail(tail, ending, yaml=quote_yaml, adjacent=len(quote) != 2)

    def container_value(self, body, ending):
        """Suppress balanced compound values without buffering the source."""
        pairs = {']': '[', '}': '{', ')': '('}
        i = 0
        while i < len(body):
            if self.container_comment:
                close = body.find('*/', i)
                if close < 0:
                    break
                self.container_comment = False
                i = close + 2
            elif self.container_quote:
                close = self.closing(body[i:], self.container_quote, yaml=self.container_yaml)
                if close < 0:
                    break
                i += close + len(self.container_quote)
                self.container_quote = None
            elif body.startswith('/*', i):
                self.container_comment = True
                i += 2
            elif body.startswith('//', i) or body[i] == '#':
                break
            elif body[i] in ('"', "'"):
                self.container_quote = self.opening(body[i:], self.container_yaml)[0]
                i += len(self.container_quote)
            elif body[i] in '[{(':
                self.container.append(body[i])
                if len(self.container) > 64:
                    raise ReaderLimitError('Credential nesting ceiling exceeded')
                i += 1
            elif body[i] in ']})':
                if not self.container or self.container.pop() != pairs[body[i]]:
                    raise ValueError('Malformed credential container')
                i += 1
                if not self.container:
                    return '[REDACTED]' + self.line(body[i:]) + ending
            else:
                i += 1
        return '[REDACTED]' + ending



def redact_text(text, secrets=(), fingerprints=()):
    """Preserve newlines/citation coordinates while filtering outbound text.

    Fingerprints let the runner filter known host credentials without passing
    those credentials into the isolated provider environment.
    """
    blocks = _CredentialBlocks()
    text = ''.join(blocks.line(line) for line in text.splitlines(keepends=True))
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]' + '\n' * secret.count('\n'))
    for length, hashes in fingerprints:
        if not isinstance(length, int) or not 1 <= length <= 4096:
            continue
        matches = []
        for pos in range(max(0, len(text) - length + 1)):
            candidate = text[pos:pos + length]
            if hashlib.sha256(candidate.encode()).hexdigest() in hashes:
                matches.append((pos, candidate))
        for pos, candidate in reversed(matches):
            text = text[:pos] + '[REDACTED]' + '\n' * candidate.count('\n') + text[pos + length:]
    text = re.sub(r'(?i)(Bearer\s+)[^\s,\x27"\\]+', r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}\b', '[REDACTED]', text)
    # Covers source assignments, JSON/YAML/env fields and quoted key names.
    text = re.sub(
        r'''(?ix)(["']?\b(?:[\w-]*(?:api[_-]?key|secret|password|passwd|token|credential|authorization)[\w-]*)\b["']?[ \t]*[:=][ \t]*)(\\?["'])([^\r\n]*?)\2''',
        lambda m: m[1] + m[2] + '[REDACTED]' + m[2], text)
    text = re.sub(
        r'(?im)(\b[\w-]*(?:api[_-]?key|secret|password|passwd|token|credential|authorization)[\w-]*[ \t]*[:=][ \t]*)(?![\\\x27"\s])[^\s,;\}\]]+',
        r'\1[REDACTED]', text)
    return text


class ReaderLimitError(ValueError):
    """A tool exhausted its bounded work allowance."""


class Budget:
    def __init__(self, deadline=None):
        self.deadline = min(float('inf') if deadline is None else deadline,
                            time.monotonic() + READER_TIMEOUT_SECONDS)
        self.entries = self.bytes = self.lines = 0

    def check(self, *, entries=0, size=0, lines=0):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Reader absolute deadline exceeded')
        self.entries += entries
        self.bytes += size
        self.lines += lines
        if (self.entries > SCAN_MAX_ENTRIES or self.bytes > SCAN_MAX_BYTES
                or self.lines > SCAN_MAX_LINES):
            raise ReaderLimitError('Reader scan resource ceiling exceeded')

EXCLUDED_DIR_NAMES = frozenset((".git", ".llm-output", "output", "vendor", ".worktrees", "node_modules"))
_EXCLUDED_DIR_NAMES_LOWER = frozenset(name.lower() for name in EXCLUDED_DIR_NAMES)

CREDENTIAL_EXTENSIONS = frozenset((
    ".pem", ".key", ".pkcs12", ".p12", ".pfx", ".kdbx", ".keystore", ".jks",
))

BINARY_EXTENSIONS = frozenset((
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".pyd",
    ".o", ".obj", ".class", ".zip", ".tar", ".gz", ".7z", ".bz2", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".tiff", ".webp",
    ".pdf", ".mp3", ".mp4", ".wav", ".avi", ".mov", ".flac", ".ogg",
    ".woff", ".woff2", ".ttf", ".eot",
))

CREDENTIAL_KEYWORDS = (
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credential", "secret",
    "token", "password", "auth_token", "access_token", "api_key",
)


def canonicalize_root(root: str | Path) -> Path:
    """Return canonical resolved directory path for root."""
    resolved = Path(root).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Root directory does not exist: {root}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Root is not a directory: {root}")
    return resolved


def is_path_excluded(rel_path: Path) -> bool:
    """Check if any path component belongs to excluded directories."""
    for part in rel_path.parts:
        if part in EXCLUDED_DIR_NAMES or part.lower() in _EXCLUDED_DIR_NAMES_LOWER:
            return True
    return False


def is_credential_path(rel_path: Path) -> bool:
    """Check if filename or relative path matches known credential patterns."""
    name = rel_path.name
    name_lower = name.lower()

    if name_lower.startswith(".env"):
        return True

    suffix_lower = rel_path.suffix.lower()
    if suffix_lower in CREDENTIAL_EXTENSIONS:
        return True

    if any(kw in name_lower for kw in CREDENTIAL_KEYWORDS):
        return True

    for part in rel_path.parts:
        part_lower = part.lower()
        if part_lower.startswith(".env") or any(kw in part_lower for kw in CREDENTIAL_KEYWORDS):
            return True

    return False


def is_symlink_or_reparse(path: Path) -> bool:
    st = os.lstat(path)
    return (stat.S_ISLNK(st.st_mode)
            or bool(getattr(st, 'st_file_attributes', 0) & 0x400)
            or bool(getattr(st, 'st_reparse_tag', 0)))


def _extract_glob_prefix(pattern: Optional[str]) -> str:
    """Extract literal path prefix before any wildcard in glob pattern if it contains slashes."""
    if not pattern:
        return ""
    norm = str(pattern).replace('\\', '/')
    if '/' not in norm:
        return ""
    wildcard_idx = len(norm)
    for ch in ('*', '?', '['):
        idx = norm.find(ch)
        if idx != -1 and idx < wildcard_idx:
            wildcard_idx = idx
    return norm[:wildcard_idx]


def _can_contain_prefix(dir_str: str, prefix: str) -> bool:
    """Return True if directory dir_str could contain files matching prefix."""
    if not prefix:
        return True
    return f"{dir_str}/".startswith(prefix) or prefix.startswith(f"{dir_str}/")


def _validate_scope(root: Path, raw: Optional[str], is_glob: bool = False) -> str:
    """Validate and normalize prefix or glob pattern; reject invalid/outside/excluded paths."""
    if not raw:
        return ""
    raw_str = str(raw) if is_glob else str(raw).strip()
    if not raw_str:
        return ""
    normalized = raw_str.replace('\\', '/')
    if (normalized.startswith('//') or any(c in raw_str for c in '\x00')
            or (':' in normalized[2:] and not is_glob)
            or (':' in normalized and not Path(raw_str).is_absolute())):
        raise ValueError('Windows special/drive/stream path rejected')

    target = Path(normalized)
    if target.is_absolute():
        try:
            rel = target.relative_to(root)
            normalized = rel.as_posix()
        except ValueError:
            raise ValueError('Parent traversal rejected')
    elif '..' in target.parts:
        raise ValueError('Parent traversal rejected')

    # The final component is a filter, not a path to open. For example, '.git'
    # selects '.github' and '.gitignore'; glob whitespace remains significant.
    scope_parts = normalized.split('/')[:-1]
    for part in scope_parts:
        if not part:
            continue
        if not is_glob or not any(c in part for c in '*?['):
            base = part.split('.')[0].rstrip(' ').upper()
            if (part.endswith((' ', '.')) or base in
                    {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
                    or re.fullmatch(r'(COM|LPT)[1-9¹²³]', base)):
                raise ValueError('Windows special path rejected')

    check_str = _extract_glob_prefix(normalized) if is_glob else normalized
    if check_str:
        for part in check_str.split('/')[:-1]:
            if not part:
                continue
            if part in EXCLUDED_DIR_NAMES or part.lower() in _EXCLUDED_DIR_NAMES_LOWER:
                raise ValueError('Access to excluded directory forbidden')

    return normalized


def _relative(root, path):
    raw = str(path)
    # Reject device namespaces, UNC paths, ADS and drive-relative paths before open.
    normalized = raw.replace('\\', '/')
    if (normalized.startswith('//') or any(c in raw for c in '\x00*?')
            or ':' in normalized[2:] or (':' in normalized and not Path(raw).is_absolute())):
        raise ValueError('Windows special/drive/stream path rejected')
    target = Path(normalized)
    if '..' in target.parts:
        raise ValueError('Parent traversal rejected')
    rel = target.relative_to(root) if target.is_absolute() else target
    if not rel.parts:
        raise ValueError('File path required')
    for part in rel.parts:
        base = part.split('.')[0].rstrip(' ').upper()
        if (':' in part or part.endswith((' ', '.')) or base in
                {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
                or re.fullmatch(r'(COM|LPT)[1-9¹²³]', base)):
            raise ValueError('Windows special path rejected')
    if is_path_excluded(rel):
        raise ValueError('Access to excluded directory forbidden')
    if is_credential_path(rel):
        raise ValueError('Access to credential file forbidden')
    if rel.suffix.lower() in BINARY_EXTENSIONS:
        raise ValueError('Access to binary file forbidden')
    return rel


@contextmanager
def _windows_handle(path, directory=False):
    # Keep ancestors open without FILE_SHARE_DELETE, so junction/parent swaps
    # cannot redirect subsequent opens. Files also deny writes while inspected.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x80000000, 1, None, 3,
                    0x00200000 | (0x02000000 if directory else 0), None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if is_symlink_or_reparse(path):
            raise ValueError('Symlink/reparse path rejected')
        yield handle
    finally:
        close(handle)


def _nofollow_open(path, flags, **kwargs):
    try:
        return os.open(path, flags, **kwargs)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError('Symlink or non-directory path rejected') from exc
        raise


@contextmanager
def _directory(root, parts=()):
    with ExitStack() as stack:
        if os.name == 'nt':
            current = root
            stack.enter_context(_windows_handle(current, True))
            for part in parts:
                current /= part
                stack.enter_context(_windows_handle(current, True))
            yield current
        else:
            fd = _nofollow_open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for part in parts:
                fd = _nofollow_open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            yield fd


@contextmanager
def _open_file(root, rel, budget):
    budget.check()
    with _directory(root, rel.parts[:-1]) as parent:
        with ExitStack() as stack:
            if os.name == 'nt':
                import msvcrt
                # Duplicate ownership for Python's fd without releasing the lock.
                import ctypes
                from ctypes import wintypes
                handle = stack.enter_context(_windows_handle(root / rel))
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.GetCurrentProcess.restype = wintypes.HANDLE
                kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                    wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                    wintypes.BOOL, wintypes.DWORD]
                duplicate = wintypes.HANDLE()
                process = kernel.GetCurrentProcess()
                if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
                    raise ctypes.WinError(ctypes.get_last_error())
                fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY | os.O_BINARY)
            else:
                fd = _nofollow_open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('Path is not a regular file')
            if st.st_nlink != 1:
                raise ValueError('Hardlink rejected')
            if st.st_size > FILE_MAX_BYTES:
                raise ReaderLimitError('File size ceiling exceeded')
            # Binary probe and actual read share the same validated descriptor.
            chunk = stream.read(1024)
            budget.check(size=len(chunk))
            if b'\0' in chunk:
                raise ValueError('Access to binary file forbidden')
            stream.seek(0)
            yield stream


def is_binary_file(resolved_file: Path) -> bool:
    try:
        with _open_file(resolved_file.parent, _relative(resolved_file.parent, resolved_file.name), Budget()):
            return False
    except ValueError:
        return True


def resolve_confined_path(root, relative_or_abs):
    root = canonicalize_root(root)
    rel = _relative(root, relative_or_abs)
    with _open_file(root, rel, Budget()):
        pass
    return root / rel, rel


def _safe_walk(root, budget=None, prefix='', glob_pattern=None):
    budget = budget or Budget()
    # Match fnmatch's platform-specific case handling when pruning a glob.
    glob_prefix = os.path.normcase(_extract_glob_prefix(glob_pattern)).replace('\\', '/')

    def walk(parts):
        budget.check()
        if len(parts) > 64:
            raise ReaderLimitError('Directory depth ceiling exceeded')
        with _directory(root, parts) as directory:
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget.check(entries=1)
                    rel = Path(*parts, entry.name)
                    try:
                        if is_path_excluded(rel) or is_credential_path(rel):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if (stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400):
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            dir_str = rel.as_posix()
                            if prefix and not _can_contain_prefix(dir_str, prefix):
                                continue
                            if glob_prefix and not _can_contain_prefix(
                                    os.path.normcase(dir_str).replace('\\', '/'), glob_prefix):
                                continue
                            yield from walk((*parts, entry.name))
                        elif stat.S_ISREG(st.st_mode):
                            file_str = rel.as_posix()
                            if prefix and not file_str.startswith(prefix):
                                continue
                            if glob_prefix and not os.path.normcase(file_str).replace('\\', '/').startswith(glob_prefix):
                                continue
                            _relative(root, rel)
                            yield root / rel, rel
                    except (ReaderLimitError, TimeoutError):
                        raise
                    except (OSError, ValueError):
                        continue
    yield from walk(())


def _lines(stream, budget):
    blocks = _CredentialBlocks()
    for number in range(1, SCAN_MAX_LINES + 2):
        budget.check()
        line = stream.readline(LINE_MAX_BYTES + 1)
        budget.check(size=len(line), lines=bool(line))
        if not line:
            return
        if len(line) > LINE_MAX_BYTES:
            raise ReaderLimitError('Line size ceiling exceeded')
        content = line.decode('utf-8-sig' if number == 1 else 'utf-8', errors='replace').rstrip('\r\n')
        yield number, blocks.line(content)


def list_files(root, prefix='', *, deadline=None):
    root = canonicalize_root(root)
    prefix = _validate_scope(root, prefix, is_glob=False)
    budget = Budget(deadline)
    visible = []
    truncated = False
    for _, rel in _safe_walk(root, budget, prefix):
        if not rel.as_posix().startswith(prefix):
            continue
        try:
            with _open_file(root, rel, budget):
                pass
        except (ReaderLimitError, TimeoutError):
            raise
        except (OSError, ValueError):
            continue
        if len(visible) == LISTINGS_MAX_PATHS:
            truncated = True
            break
        visible.append(rel.as_posix())
    visible.sort()
    text = '\n'.join(visible)
    if truncated:
        text += '\n[Truncated: path limit reached; total unknown]'
    return dict(paths=visible, total=None if truncated else len(visible), truncated=truncated, text=text)


def search(root, query, glob_pattern=None, limit=SEARCH_MAX_MATCHES, *, deadline=None):
    if not isinstance(query, str) or not query:
        raise ValueError('Search query cannot be empty')
    root = canonicalize_root(root)
    if glob_pattern:
        glob_pattern = _validate_scope(root, glob_pattern, is_glob=True)
    budget = Budget(deadline)
    max_matches = max(1, min(int(limit), SEARCH_MAX_MATCHES))
    matches, formatted = [], []
    chars = 0
    reason = None
    for _, rel in _safe_walk(root, budget, '', glob_pattern):
        name = rel.as_posix()
        if glob_pattern and not (fnmatch.fnmatch(name, glob_pattern) or fnmatch.fnmatch(rel.name, glob_pattern)):
            continue
        try:
            with _open_file(root, rel, budget) as stream:
                for number, content in _lines(stream, budget):
                    if query.lower() not in content.lower():
                        continue
                    line = f'{name}:{number}: {content}'
                    if chars + len(line) + 1 > SEARCH_MAX_CHARS:
                        reason = f'character limit of {SEARCH_MAX_CHARS} chars'
                        break
                    matches.append(dict(path=name, line=number, content=content))
                    formatted.append(line)
                    chars += len(line) + 1
                    if len(matches) >= max_matches:
                        reason = f'match limit of {max_matches}'
                        break
        except (ReaderLimitError, TimeoutError):
            raise
        except (OSError, ValueError):
            continue
        if reason:
            break
    if reason:
        formatted.append(f'[Truncated: {reason}; total unknown]')
    return dict(matches=matches, count=len(matches), total_found=len(matches),
                truncated=bool(reason), truncation_reason=reason, text='\n'.join(formatted))


def read_file(root, path, start=1, end=None, *, deadline=None):
    root = canonicalize_root(root)
    rel = _relative(root, path)
    budget = Budget(deadline)
    start = max(1, int(start))
    requested_end = int(end) if end is not None else start + READ_MAX_LINES - 1
    if requested_end < start:
        raise ValueError('End line before start line')
    target = min(requested_end, start + READ_MAX_LINES - 1)
    formatted, chars, actual_end = [], 0, start - 1
    char_truncated = line_truncated = False
    total_lines = None
    with _open_file(root, rel, budget) as stream:
        last = 0
        for number, content in _lines(stream, budget):
            last = number
            if number < start:
                continue
            if number > target:
                line_truncated = requested_end > target or end is None
                break
            line = f'{number:5d} | {content}'
            if chars + len(line) + 1 > READ_MAX_CHARS:
                char_truncated = True
                break
            formatted.append(line)
            chars += len(line) + 1
            actual_end = number
        else:
            total_lines = last
        if not last:
            return dict(path=rel.as_posix(), start=0, end=0, total_lines=0, truncated=False, text='[Empty file]')
        if last < start:
            raise ValueError('Requested start line exceeds total lines')
    truncated = char_truncated or line_truncated
    if truncated:
        reason = 'character limit' if char_truncated else 'line limit'
        formatted.append(f'[Truncated: lines {start}-{actual_end}; {reason}; total unknown]')
    return dict(path=rel.as_posix(), start=start, end=actual_end, total_lines=total_lines,
                truncated=truncated, char_truncated=char_truncated, line_truncated=line_truncated,
                text='\n'.join(formatted))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mercury 3 bounded read-only repository reader")
    parser.add_argument("--root", required=True, help="Corpus root directory")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list_files [PREFIX]
    p_list = subparsers.add_parser("list_files", help="List relative file paths in root")
    p_list.add_argument("prefix", nargs="?", default="", help="Optional path prefix filter")

    # search QUERY --glob GLOB --limit N
    p_search = subparsers.add_parser("search", help="Search substring across files in root")
    p_search.add_argument("query", help="Case-insensitive search query")
    p_search.add_argument("--glob", dest="glob_pattern", default=None, help="Optional glob pattern, e.g. *.py")
    p_search.add_argument("--limit", type=int, default=SEARCH_MAX_MATCHES, help=f"Max matches (up to {SEARCH_MAX_MATCHES})")

    # read_file PATH START END
    p_read = subparsers.add_parser("read_file", help="Read numbered line range from a file")
    p_read.add_argument("path", help="File path relative to root")
    p_read.add_argument("start", nargs="?", type=int, default=1, help="1-based start line (default 1)")
    p_read.add_argument("end", nargs="?", type=int, default=None, help="1-based end line (default start + 199)")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.subcommand == "list_files":
            result = list_files(args.root, prefix=args.prefix)
            print(result["text"])
        elif args.subcommand == "search":
            result = search(args.root, query=args.query, glob_pattern=args.glob_pattern, limit=args.limit)
            print(result["text"])
        elif args.subcommand == "read_file":
            result = read_file(args.root, path=args.path, start=args.start, end=args.end)
            print(result["text"])
        else:
            parser.error(f"Unknown subcommand {args.subcommand}")
        return 0
    except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
