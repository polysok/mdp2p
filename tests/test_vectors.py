"""Replay spec/vectors/*.json against the Python reference implementation.

If one of these fails, the Python code changed observable protocol
behaviour: either fix the regression or, for a deliberate change, update
SPEC.md, regenerate with ``python scripts/gen_vectors.py`` and make every
other implementation (rust/mdp2p-core) agree again.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bundle import (
    b64_to_public_key,
    validate_path,
    compute_content_key,
    compute_manifest_ref,
    validate_uri,
    verify_manifest,
    verify_name_record,
)
from bundle._canonical import _canonical_json
from peer.peer import _check_downloaded_bundle
from pinstore import check_pin
from review import (
    select_reviewers,
    verify_review_assignment,
    verify_review_record,
    verify_reviewer_opt_in,
)

VECTORS = ROOT / "spec" / "vectors"


def cases(name: str) -> list[dict]:
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))["cases"]


def ids(name: str) -> list[str]:
    return [c.get("name", str(i)) for i, c in enumerate(cases(name))]


def test_vectors_are_up_to_date():
    from scripts.gen_vectors import main

    assert main(["--check"]) == 0


@pytest.mark.parametrize("case", cases("canonical_json"), ids=ids("canonical_json"))
def test_canonical_json(case):
    assert _canonical_json(case["input"]).decode("ascii") == case["canonical"]


@pytest.mark.parametrize("case", cases("manifest_verify"), ids=ids("manifest_verify"))
def test_manifest_verify(case):
    trusted = b64_to_public_key(case["trusted_public_key"])
    assert verify_manifest(case["manifest"], case["signature"], trusted) is case["valid"]
    assert compute_manifest_ref(case["manifest"]) == case["manifest_ref"]


@pytest.mark.parametrize("case", cases("content_key"))
def test_content_key(case):
    assert compute_content_key(case["uri"], case["public_key"]) == case["content_key"]


@pytest.mark.parametrize("case", cases("name_records"), ids=ids("name_records"))
def test_name_records(case):
    with mock.patch("time.time", return_value=float(case["now"])):
        ok, _ = verify_name_record(
            case["record"], case["signature"], max_drift=case["max_drift"]
        )
    assert ok is case["valid"]


REVIEW_VERIFIERS = {
    "review_record": verify_review_record,
    "reviewer_opt_in": verify_reviewer_opt_in,
    "review_assignment": verify_review_assignment,
}


@pytest.mark.parametrize(
    "case",
    cases("reviews"),
    ids=[f"{c['kind']}-{c['name']}" for c in cases("reviews")],
)
def test_reviews(case):
    with mock.patch("time.time", return_value=float(case["now"])):
        ok, _ = REVIEW_VERIFIERS[case["kind"]](
            case["record"], case["signature"], max_drift=case["max_drift"]
        )
    assert ok is case["valid"]


@pytest.mark.parametrize("case", cases("uris"))
def test_uris(case):
    try:
        validate_uri(case["uri"])
        ok = True
    except ValueError:
        ok = False
    assert ok is case["valid"]


@pytest.mark.parametrize("case", cases("paths"))
def test_paths(case):
    try:
        validate_path(Path("/nonexistent/mdp2p-vectors/site"), case["path"])
        ok = True
    except ValueError:
        ok = False
    assert ok is case["valid"]


@pytest.mark.parametrize("case", cases("bundle_checks"), ids=ids("bundle_checks"))
def test_bundle_checks(case):
    with mock.patch("time.time", return_value=float(case["now"])):
        reason = _check_downloaded_bundle(
            case["bundle"],
            case["uri"],
            b64_to_public_key(case["trusted_public_key"]),
            case["expected_ref"],
            str(ROOT / "nonexistent-site"),
        )
    # Python reports free-form reasons; the stable reason codes are
    # asserted by the Rust core. Here only the verdict must match.
    assert (reason is None) is case["accept"], reason


@pytest.mark.parametrize("case", cases("pins"), ids=ids("pins"))
def test_pins(case):
    store = {} if case["entry"] is None else {"blog.alice": case["entry"]}
    status = check_pin(store, "blog.alice", case["public_key"], case["record_timestamp"])
    assert status.value == case["status"]


@pytest.mark.parametrize(
    "case", cases("reviewer_selection"), ids=ids("reviewer_selection")
)
def test_reviewer_selection(case):
    assert select_reviewers(case["content_key"], case["pool"], case["n"]) == (
        case["selected"]
    )
