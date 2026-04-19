"""Internal helpers shared across the bundle package.

Canonical JSON encoding and file hashing are used by the manifest, naming
record, and serialization modules alike. Keeping them here avoids cycles and
duplication.
"""

import hashlib
import json
from pathlib import Path


def _canonical_json(obj: dict) -> bytes:
    """Canonical JSON for deterministic signing."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _hash_file(path: Path) -> str:
    """Hex SHA-256 of a file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
