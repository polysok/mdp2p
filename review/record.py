"""
Review records and reviewer opt-in registrations.

Two signed structures live here, both following the pattern of
`bundle.name_records`:

  - ReviewerOptIn: a peer declares it accepts review requests, optionally
    restricting itself to a set of categories. Published to the naming
    server so publishers can discover and select reviewers.

  - ReviewRecord: a single opinion on a piece of content, signed by the
    reviewer. Attached to the manifest and consumed by the scorer as a
    Signal.

Records are plain dicts (for easy JSON serialization) and signatures are
Ed25519 over canonical JSON, matching the convention used for naming
records and manifests.
"""

import base64
import time
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bundle._canonical import _canonical_json
from bundle.crypto import b64_to_public_key, public_key_to_b64


MAX_REVIEW_DRIFT_SECONDS = 300

VALID_VERDICTS = ("ok", "warn", "reject")


# ---------------------------------------------------------------------------
# Reviewer opt-in
# ---------------------------------------------------------------------------


def build_reviewer_opt_in(
    public_key_b64: str,
    categories: Optional[list[str]] = None,
    timestamp: Optional[int] = None,
) -> dict:
    """Build an unsigned reviewer opt-in record.

    `categories` is an optional filter — an empty or missing list means the
    reviewer accepts requests for any category.
    """
    return {
        "public_key": public_key_b64,
        "categories": list(categories) if categories else [],
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }


def sign_reviewer_opt_in(record: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign an opt-in record. The record's public_key must match the signer."""
    expected_pub = public_key_to_b64(private_key.public_key())
    if record.get("public_key") != expected_pub:
        raise ValueError("record public_key does not match signing private key")
    signature = private_key.sign(_canonical_json(record))
    return base64.b64encode(signature).decode()


def verify_reviewer_opt_in(
    record: dict,
    signature_b64: str,
    max_drift: Optional[int] = MAX_REVIEW_DRIFT_SECONDS,
) -> Tuple[bool, str]:
    """Verify an opt-in record against its embedded public key."""
    required = ("public_key", "categories", "timestamp")
    missing = [f for f in required if f not in record]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    if not isinstance(record["categories"], list):
        return False, "categories must be a list"

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


# ---------------------------------------------------------------------------
# Review record
# ---------------------------------------------------------------------------


def build_review_record(
    content_key: str,
    reviewer_pubkey_b64: str,
    verdict: str,
    comment: str = "",
    timestamp: Optional[int] = None,
) -> dict:
    """Build an unsigned review record.

    Raises ValueError for an unknown verdict — this catches typos at build
    time rather than letting them silently score as zero severity.
    """
    if verdict not in VALID_VERDICTS:
        raise ValueError(
            f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}"
        )
    return {
        "content_key": content_key,
        "reviewer_public_key": reviewer_pubkey_b64,
        "verdict": verdict,
        "comment": comment,
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }


def sign_review_record(record: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign a review record. The record's reviewer_public_key must match the signer."""
    expected_pub = public_key_to_b64(private_key.public_key())
    if record.get("reviewer_public_key") != expected_pub:
        raise ValueError(
            "record reviewer_public_key does not match signing private key"
        )
    signature = private_key.sign(_canonical_json(record))
    return base64.b64encode(signature).decode()


def verify_review_record(
    record: dict,
    signature_b64: str,
    max_drift: Optional[int] = MAX_REVIEW_DRIFT_SECONDS,
) -> Tuple[bool, str]:
    """Verify a review record against its embedded reviewer public key."""
    required = (
        "content_key",
        "reviewer_public_key",
        "verdict",
        "comment",
        "timestamp",
    )
    missing = [f for f in required if f not in record]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    if record["verdict"] not in VALID_VERDICTS:
        return False, f"invalid verdict: {record['verdict']!r}"

    try:
        if max_drift is not None:
            now = int(time.time())
            drift = abs(now - int(record["timestamp"]))
            if drift > max_drift:
                return False, f"timestamp drift too large ({drift}s)"

        pub_key = b64_to_public_key(record["reviewer_public_key"])
        signature = base64.b64decode(signature_b64)
        pub_key.verify(signature, _canonical_json(record))
        return True, ""
    except InvalidSignature:
        return False, "invalid signature"
    except Exception as e:
        return False, f"verification error: {e}"
