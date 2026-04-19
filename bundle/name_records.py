"""Naming records and registration proofs.

A name record ties (uri, author, public_key, manifest_ref) together and is
signed by the author. Register proofs authenticate a key-ownership claim at
the moment of tracker registration.
"""

import base64
import time
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._canonical import _canonical_json
from .crypto import b64_to_public_key, public_key_to_b64

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
