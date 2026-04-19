"""Ed25519 key management for MDP2P bundles.

Provides keypair generation on disk, PEM load helpers, and raw-base64
round-tripping for use in manifests and naming records.
"""

import base64
import os
import stat
from pathlib import Path
from typing import Optional, Tuple, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


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
