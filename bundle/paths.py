"""URI and filesystem path validation for bundles.

Centralises the rules that keep hostile inputs from escaping a bundle
directory or poisoning lookup keys.
"""

import re
from pathlib import Path

MAX_URI_LENGTH = 255
_VALID_URI_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
MAX_PATH_DEPTH = 10


def validate_uri(uri: str) -> str:
    """Validate and return a safe URI string.

    Accepted characters: alphanumeric, dots, hyphens, underscores.
    Rejects empty strings, path traversal, path separators, and excessive length.
    """
    if not uri or not isinstance(uri, str):
        raise ValueError("URI must be a non-empty string")
    if len(uri) > MAX_URI_LENGTH:
        raise ValueError(f"URI too long ({len(uri)} chars, max {MAX_URI_LENGTH})")
    if "/" in uri or "\\" in uri or "\x00" in uri:
        raise ValueError(f"URI contains forbidden characters: {uri!r}")
    if uri in (".", "..") or ".." in uri:
        raise ValueError(f"URI contains path traversal: {uri!r}")
    if not _VALID_URI_RE.match(uri):
        raise ValueError(
            f"URI contains invalid characters (allowed: a-z, 0-9, '.', '-', '_'): {uri!r}"
        )
    return uri


def validate_path(base_dir: Path, relative_path: str) -> Path:
    """Resolve and validate that a path stays within base_dir.

    Raises ValueError on path traversal or excessive depth.
    """
    resolved = (base_dir / relative_path).resolve()
    base_resolved = base_dir.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise ValueError(f"Path traversal detected: {relative_path}")
    if resolved == base_resolved:
        raise ValueError(f"Path resolves to base directory: {relative_path}")
    if len(Path(relative_path).parts) > MAX_PATH_DEPTH:
        raise ValueError(
            f"Path too deep ({len(Path(relative_path).parts)} levels): {relative_path}"
        )
    return resolved


def make_key_name(author: str, uri: str) -> str:
    """Generate a stable key file name from author and URI."""
    return f"{author}_{uri.replace('://', '_').replace('/', '_')}"
