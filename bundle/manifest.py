"""Manifest creation, signing, verification, and content-key derivation.

A manifest lists every file in a bundle with its SHA-256 hash and carries
metadata (uri, author, version, expiry). Signed manifests are the unit of
trust exchanged between peers.
"""

import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ._canonical import _canonical_json, _hash_file
from .crypto import public_key_to_b64
from .paths import validate_path

logger = logging.getLogger("mdp2p.bundle")

MAX_BUNDLE_FILES = 1000
MAX_BUNDLE_TOTAL_SIZE = 50 * 1024 * 1024  # 50 MB
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def create_manifest(
    site_dir: str,
    uri: str = "",
    author: str = "",
    version: int = 1,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> dict:
    """Scan a directory of .md files and create the manifest."""
    site_path = Path(site_dir)
    files: List[Dict[str, object]] = []
    for md_file in sorted(site_path.rglob("*.md")):
        rel = md_file.relative_to(site_path).as_posix()
        files.append(
            {
                "path": rel,
                "hash": _hash_file(md_file),
                "size": md_file.stat().st_size,
            }
        )

    if len(files) > MAX_BUNDLE_FILES:
        raise ValueError(f"Too many files: {len(files)} (max {MAX_BUNDLE_FILES})")
    total_size = sum(f["size"] for f in files)
    if total_size > MAX_BUNDLE_TOTAL_SIZE:
        raise ValueError(
            f"Bundle too large: {total_size} bytes (max {MAX_BUNDLE_TOTAL_SIZE})"
        )

    now = int(time.time())
    manifest: Dict[str, object] = {
        "uri": uri,
        "author": author,
        "version": version,
        "timestamp": now,
        "expires_at": now + ttl,
        "files": files,
        "total_size": total_size,
        "file_count": len(files),
    }
    return manifest


def is_manifest_expired(manifest: dict) -> bool:
    """Check if a manifest has expired based on its expires_at field."""
    expires_at = manifest.get("expires_at")
    if expires_at is None:
        return False
    return int(time.time()) > expires_at


def sign_manifest(manifest: dict, private_key: Ed25519PrivateKey) -> Tuple[dict, str]:
    """Sign the manifest without mutating the input. Returns (signed manifest copy, b64 signature)."""
    signed = dict(manifest)
    signed["public_key"] = public_key_to_b64(private_key.public_key())
    canonical = _canonical_json(signed)
    signature = private_key.sign(canonical)
    return signed, base64.b64encode(signature).decode()


def verify_manifest(
    manifest: dict, signature_b64: str, trusted_public_key: Ed25519PublicKey
) -> bool:
    """Verify the manifest signature using a trusted external public key.

    The trusted_public_key must come from a source independent of the bundle
    (e.g. the tracker). The embedded key in the manifest is checked against it.
    """
    try:
        embedded_key_b64 = manifest.get("public_key", "")
        if embedded_key_b64 != public_key_to_b64(trusted_public_key):
            logger.warning("Embedded public key does not match trusted key")
            return False

        signature = base64.b64decode(signature_b64)
        canonical = _canonical_json(manifest)
        trusted_public_key.verify(signature, canonical)
        return True
    except InvalidSignature:
        logger.warning("Manifest signature verification failed")
        return False
    except KeyError as e:
        logger.error("Manifest missing required field: %s", e)
        raise
    except Exception as e:
        logger.error("Unexpected error during verification: %s", e)
        raise


def verify_files(manifest: dict, site_dir: str) -> List[str]:
    """Verify file integrity. Returns a list of errors.

    Checks for missing files, corrupted hashes, path traversal,
    and unauthorized files not listed in the manifest.
    """
    site_path = Path(site_dir).resolve()
    errors = []
    manifest_paths: set = set()

    for entry in manifest["files"]:
        manifest_paths.add(entry["path"])
        try:
            fpath = validate_path(site_path, entry["path"])
        except ValueError as e:
            errors.append(f"INVALID_PATH: {entry['path']} ({e})")
            continue
        if not fpath.exists():
            errors.append(f"MISSING: {entry['path']}")
        elif _hash_file(fpath) != entry["hash"]:
            errors.append(f"CORRUPTED: {entry['path']}")

    for md_file in site_path.rglob("*.md"):
        rel = md_file.relative_to(site_path).as_posix()
        if rel not in manifest_paths:
            errors.append(f"UNAUTHORIZED: {rel}")

    return errors


def compute_content_key(uri: str, public_key_b64: str) -> str:
    """Stable DHT content key for a (uri, author) pair.

    The key is stable across manifest versions, so the swarm of providers
    stays coherent when the author publishes v2 — newer seeders simply
    replace older ones, without forcing rediscovery.
    """
    digest = hashlib.sha256(f"{uri}:{public_key_b64}".encode("utf-8")).hexdigest()
    return f"/mdp2p/{digest}"


def compute_manifest_ref(manifest: dict) -> str:
    """Hex SHA-256 of the canonical JSON of a signed manifest.

    Used as the stable content identifier in the naming layer: the author
    publishes (uri, public_key, manifest_ref) and seeders serve the bundle
    whose manifest hashes to exactly that ref.
    """
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()
