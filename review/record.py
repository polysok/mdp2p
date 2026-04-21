"""
Review records and reviewer opt-in registrations.

Three signed structures live here, all following the pattern of
`bundle.name_records`:

  - ReviewerOptIn: a peer declares it accepts review requests, optionally
    restricting itself to a set of categories. Published to the naming
    server so publishers can discover and select reviewers.

  - ReviewAssignment: a publisher commits to having selected a set of
    reviewers for a piece of content, with a deadline. Signed by the
    publisher and posted to the naming server's inbox for each selected
    reviewer to pick up on their own schedule.

  - ReviewRecord: a single opinion on a piece of content, signed by the
    reviewer. Posted back to the naming server as an attachment on the
    content_key and consumed by the scorer as a Signal.

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
from bundle.manifest import compute_content_key
from review.taxonomy import validate_categories as _validate_categories


MAX_REVIEW_DRIFT_SECONDS = 300

VALID_VERDICTS = ("ok", "warn", "reject")


# ---------------------------------------------------------------------------
# Reviewer opt-in
# ---------------------------------------------------------------------------


def build_reviewer_opt_in(
    public_key_b64: str,
    peer_id: str,
    addrs: list[str],
    categories: Optional[list[str]] = None,
    timestamp: Optional[int] = None,
) -> dict:
    """Build an unsigned reviewer opt-in record.

    `peer_id` is the libp2p identity, stable across restarts. `addrs` is the
    reviewer's current dialable multiaddrs — this field is expected to be
    refreshed by the reviewer daemon on address changes and periodically
    as a heartbeat. `categories` is an optional interest filter — each
    entry must be a slug from ``review.taxonomy.CATEGORY_SLUGS``; an empty
    list means "accept any category".
    """
    normalized = list(categories) if categories else []
    _validate_categories(normalized)
    return {
        "public_key": public_key_b64,
        "peer_id": peer_id,
        "addrs": list(addrs),
        "categories": normalized,
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
    required = ("public_key", "peer_id", "addrs", "categories", "timestamp")
    missing = [f for f in required if f not in record]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    if not isinstance(record["categories"], list):
        return False, "categories must be a list"
    if not isinstance(record["addrs"], list) or not all(
        isinstance(a, str) for a in record["addrs"]
    ):
        return False, "addrs must be a list of strings"
    if not isinstance(record["peer_id"], str) or not record["peer_id"]:
        return False, "peer_id must be a non-empty string"

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
# Review assignment
# ---------------------------------------------------------------------------


def build_review_assignment(
    uri: str,
    publisher_pubkey_b64: str,
    reviewer_pubkeys_b64: list[str],
    deadline: int,
    timestamp: Optional[int] = None,
) -> dict:
    """Build an unsigned review assignment record.

    `uri` and `publisher_pubkey_b64` together identify the content the
    reviewer is expected to fetch; `content_key` is derived from them so
    assignments and attachments share a consistent primary key.

    `deadline` is the unix timestamp after which the assignment expires —
    reviewers should ignore assignments past this point. `reviewer_pubkeys`
    lists the selection in full so anyone can re-run the deterministic
    selection function to verify the publisher did not cherry-pick.
    """
    if deadline <= 0:
        raise ValueError("deadline must be a positive unix timestamp")
    if not uri:
        raise ValueError("uri is required")
    return {
        "uri": uri,
        "content_key": compute_content_key(uri, publisher_pubkey_b64),
        "publisher_public_key": publisher_pubkey_b64,
        "reviewer_public_keys": list(reviewer_pubkeys_b64),
        "deadline": int(deadline),
        "timestamp": timestamp if timestamp is not None else int(time.time()),
    }


def sign_review_assignment(record: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign an assignment. The record's publisher_public_key must match the signer."""
    expected_pub = public_key_to_b64(private_key.public_key())
    if record.get("publisher_public_key") != expected_pub:
        raise ValueError(
            "record publisher_public_key does not match signing private key"
        )
    signature = private_key.sign(_canonical_json(record))
    return base64.b64encode(signature).decode()


def verify_review_assignment(
    record: dict,
    signature_b64: str,
    max_drift: Optional[int] = MAX_REVIEW_DRIFT_SECONDS,
) -> Tuple[bool, str]:
    """Verify a review assignment against its embedded publisher public key."""
    required = (
        "uri",
        "content_key",
        "publisher_public_key",
        "reviewer_public_keys",
        "deadline",
        "timestamp",
    )
    missing = [f for f in required if f not in record]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    if not isinstance(record["reviewer_public_keys"], list) or not all(
        isinstance(p, str) for p in record["reviewer_public_keys"]
    ):
        return False, "reviewer_public_keys must be a list of strings"
    if not isinstance(record["deadline"], int) or record["deadline"] <= 0:
        return False, "deadline must be a positive integer"

    expected_content_key = compute_content_key(
        record.get("uri", ""), record.get("publisher_public_key", "")
    )
    if record.get("content_key") != expected_content_key:
        return False, "content_key does not match (uri, publisher_public_key)"

    try:
        if max_drift is not None:
            now = int(time.time())
            drift = abs(now - int(record["timestamp"]))
            if drift > max_drift:
                return False, f"timestamp drift too large ({drift}s)"

        pub_key = b64_to_public_key(record["publisher_public_key"])
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
