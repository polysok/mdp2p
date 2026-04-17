"""
MDP2P Bundle — Creation, signing and verification of Markdown sites.

A bundle is a directory containing:
  - manifest.json  : list of files, SHA-256 hashes, version, public key
  - manifest.sig   : ed25519 signature of the manifest (base64)
  - *.md           : the site files

The public key serves as the site's identity. The human-readable URI is just an alias.
"""

import base64
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger("mdp2p.bundle")

MAX_BUNDLE_FILES = 1000
MAX_BUNDLE_TOTAL_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_PATH_DEPTH = 10
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def make_key_name(author: str, uri: str) -> str:
    """Generate a stable key file name from author and URI."""
    return f"{author}_{uri.replace('://', '_').replace('/', '_')}"


def generate_keypair(
    key_dir: str, name: str, passphrase: Optional[str] = None
) -> Tuple[str, str]:
    """Generate an ed25519 key pair. Returns (private key path, public key path)."""
    key_path = Path(key_dir)
    key_path.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()

    priv_path = key_path / f"{name}.key"
    pub_path = key_path / f"{name}.pub"

    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(priv_pem)
    os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    pub_path.write_bytes(pub_pem)

    return str(priv_path), str(pub_path)


def load_private_key(
    path: str, passphrase: Optional[str] = None
) -> Ed25519PrivateKey:
    """Load a private key from a PEM file."""
    password = passphrase.encode() if passphrase else None
    return cast(
        Ed25519PrivateKey,
        serialization.load_pem_private_key(Path(path).read_bytes(), password=password),
    )


def load_public_key(path: str) -> Ed25519PublicKey:
    """Load a public key from a PEM file."""
    return cast(
        Ed25519PublicKey,
        serialization.load_pem_public_key(Path(path).read_bytes()),
    )


def public_key_to_b64(pub_key: Ed25519PublicKey) -> str:
    """Export the public key as raw base64 (32 bytes)."""
    raw = pub_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


def b64_to_public_key(b64: str) -> Ed25519PublicKey:
    """Import a public key from raw base64."""
    raw = base64.b64decode(b64)
    return Ed25519PublicKey.from_public_bytes(raw)


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


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(obj: dict) -> bytes:
    """Canonical JSON for deterministic signing."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


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


MAX_TIMESTAMP_DRIFT_SECONDS = 300  # 5 minutes


def create_register_proof(
    uri: str,
    author: str,
    private_key: Ed25519PrivateKey,
    timestamp: Optional[int] = None,
) -> Tuple[str, int]:
    """Create a proof of key ownership for tracker registration.

    Returns (proof_b64, timestamp). The author, URI, and timestamp are all
    signed together to prevent falsification of any field.
    """
    if timestamp is None:
        timestamp = int(time.time())

    message = f"REGISTER:{author}:{uri}:{timestamp}".encode("utf-8")
    signature = private_key.sign(message)
    return base64.b64encode(signature).decode(), timestamp


def verify_register_proof(
    uri: str,
    author: str,
    public_key_b64: str,
    proof_b64: str,
    timestamp: int,
    max_drift: Optional[int] = MAX_TIMESTAMP_DRIFT_SECONDS,
) -> Tuple[bool, str]:
    """Verify a registration proof of key ownership with timestamp and author.

    Returns (is_valid, error_message).
    max_drift=None skips the drift check (used for federation imports).
    """
    try:
        now = int(time.time())
        if max_drift is not None and abs(now - timestamp) > max_drift:
            return False, f"Timestamp too old or in future (drift: {abs(now - timestamp)}s)"

        pub_key = b64_to_public_key(public_key_b64)
        proof = base64.b64decode(proof_b64)
        message = f"REGISTER:{author}:{uri}:{timestamp}".encode("utf-8")
        pub_key.verify(proof, message)
        return True, ""
    except InvalidSignature:
        return False, "Invalid signature"
    except Exception as e:
        return False, f"Verification error: {e}"


def compute_manifest_ref(manifest: dict) -> str:
    """Hex SHA-256 of the canonical JSON of a signed manifest.

    Used as the stable content identifier in the naming layer: the author
    publishes (uri, public_key, manifest_ref) and seeders serve the bundle
    whose manifest hashes to exactly that ref.
    """
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()


def build_name_record(
    uri: str,
    author: str,
    public_key_b64: str,
    manifest_ref: str,
    timestamp: Optional[int] = None,
) -> dict:
    """Build a naming record (unsigned). Pair with sign_name_record()."""
    return {
        "uri": uri,
        "author": author,
        "public_key": public_key_b64,
        "manifest_ref": manifest_ref,
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }


def sign_name_record(record: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign a naming record. Returns base64 signature over canonical JSON.

    The record's public_key must match the signing key.
    """
    expected_pub = public_key_to_b64(private_key.public_key())
    if record.get("public_key") != expected_pub:
        raise ValueError("record public_key does not match signing private key")
    signature = private_key.sign(_canonical_json(record))
    return base64.b64encode(signature).decode()


def verify_name_record(
    record: dict,
    signature_b64: str,
    max_drift: Optional[int] = MAX_TIMESTAMP_DRIFT_SECONDS,
) -> Tuple[bool, str]:
    """Verify a naming record against its embedded public key.

    Returns (is_valid, error_message). The record is self-contained: the
    signature is checked against record["public_key"]. max_drift=None skips
    the drift check (used for federation or replay scenarios).
    """
    required = ("uri", "author", "public_key", "manifest_ref", "timestamp")
    missing = [field for field in required if field not in record]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    try:
        if max_drift is not None:
            now = int(time.time())
            drift = abs(now - int(record["timestamp"]))
            if drift > max_drift:
                return False, f"timestamp drift too large ({drift}s)"

        pub_key = b64_to_public_key(record["public_key"])
        signature = base64.b64decode(signature_b64)
        pub_key.verify(signature, _canonical_json(record))
        return True, ""
    except InvalidSignature:
        return False, "invalid signature"
    except Exception as e:
        return False, f"verification error: {e}"


def save_bundle(site_dir: str, manifest: dict, signature: str) -> None:
    """Save the manifest and signature to the site directory."""
    site_path = Path(site_dir)
    (site_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (site_path / "manifest.sig").write_text(signature, encoding="utf-8")


def load_bundle(site_dir: str) -> Tuple[dict, str]:
    """Load the manifest and signature from a directory."""
    site_path = Path(site_dir)
    manifest = json.loads(
        (site_path / "manifest.json").read_text(encoding="utf-8")
    )
    signature = (site_path / "manifest.sig").read_text(encoding="utf-8").strip()
    return manifest, signature


def bundle_to_dict(site_dir: str) -> dict:
    """Serialize a complete bundle (manifest + signature + files) to a dict.
    Used for peer-to-peer network transfer."""
    site_path = Path(site_dir).resolve()
    manifest, signature = load_bundle(site_dir)
    files = {}
    for entry in manifest["files"]:
        fpath = validate_path(site_path, entry["path"])
        files[entry["path"]] = fpath.read_text(encoding="utf-8")
    return {
        "manifest": manifest,
        "signature": signature,
        "files": files,
    }


def dict_to_bundle(data: dict, output_dir: str) -> str:
    """Reconstruct a bundle from a dict received over the network.

    Validates path safety, file count, and total size before writing anything.
    """
    manifest = data["manifest"]
    files_data = data["files"]

    file_count = manifest.get("file_count", len(files_data))
    if file_count > MAX_BUNDLE_FILES:
        raise ValueError(f"Too many files: {file_count} (max {MAX_BUNDLE_FILES})")

    out = Path(output_dir).resolve()

    validated_files: List[Tuple[Path, str]] = []
    actual_total_size = 0
    for path, content in files_data.items():
        fpath = validate_path(out, path)
        actual_total_size += len(content.encode("utf-8"))
        if actual_total_size > MAX_BUNDLE_TOTAL_SIZE:
            raise ValueError(
                f"Actual content exceeds size limit ({MAX_BUNDLE_TOTAL_SIZE} bytes)"
            )
        validated_files.append((fpath, content))

    out.mkdir(parents=True, exist_ok=True)
    for fpath, content in validated_files:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    save_bundle(output_dir, data["manifest"], data["signature"])
    return str(out)