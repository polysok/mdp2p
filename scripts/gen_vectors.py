"""Generate the MDP2P conformance test vectors from the Python reference.

Every implementation of the protocol (the Python reference, the Rust
``mdp2p-core`` crate, future clients) must reach the same verdict on every
case in ``spec/vectors/*.json``. Keys are derived from fixed seeds and the
clock is pinned, so regenerating is byte-for-byte reproducible:

    python scripts/gen_vectors.py          # rewrite spec/vectors/
    python scripts/gen_vectors.py --check  # fail if the files are stale

``tests/test_vectors.py`` replays the vectors against the Python code;
``rust/mdp2p-core/tests/vectors.rs`` replays them against the Rust core.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bundle import (
    build_name_record,
    compute_content_key,
    compute_manifest_ref,
    public_key_to_b64,
    sign_manifest,
    sign_name_record,
    validate_uri,
)
from bundle._canonical import _canonical_json
from review import (
    build_review_assignment,
    build_review_record,
    build_reviewer_opt_in,
    select_reviewers,
    sign_review_assignment,
    sign_review_record,
    sign_reviewer_opt_in,
)

VECTORS_DIR = Path(__file__).resolve().parent.parent / "spec" / "vectors"
NOW = 1_790_000_000  # pinned clock for every time-dependent case
VECTOR_VERSION = 1


def key(seed: int) -> Ed25519PrivateKey:
    """Deterministic test key. Never use such keys outside test vectors."""
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def pub(seed: int) -> str:
    return public_key_to_b64(key(seed).public_key())


ALICE, MALLORY, REVIEWER = 1, 2, 3


def at_now(fn: Callable, *args, **kwargs):
    """Call ``fn`` with time.time() pinned to NOW (record builders read it)."""
    with mock.patch("time.time", return_value=float(NOW)):
        return fn(*args, **kwargs)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def flip_sig(sig_b64: str) -> str:
    raw = bytearray(base64.b64decode(sig_b64))
    raw[0] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


# ─── canonical JSON ───────────────────────────────────────────────────


def gen_canonical_json() -> list[dict]:
    inputs = [
        ("sorted_keys", {"b": 1, "a": 2, "c": {"z": True, "y": None}}),
        ("nested_list", {"files": [{"path": "b.md"}, {"path": "a.md"}], "n": 0}),
        ("escapes", {"s": 'quote " backslash \\ nl \n tab \t cr \r'}),
        ("control_chars", {"s": "\x00\x01\x1f\x7f"}),
        ("non_ascii_bmp", {"s": "café — 中文 العربية"}),
        ("non_ascii_astral", {"s": "emoji 🙂 math 𝔸"}),
        ("non_ascii_key", {"é": 1, "e": 2, "z": 3}),
        ("negative_and_big_int", {"a": -5, "b": 9007199254740993}),
        ("empty_containers", {"a": {}, "b": [], "c": ""}),
        ("slash_not_escaped", {"uri": "/mdp2p/abc"}),
    ]
    return [
        {
            "name": name,
            "input": value,
            "canonical": _canonical_json(value).decode("ascii"),
        }
        for name, value in inputs
    ]


# ─── manifests ────────────────────────────────────────────────────────


def make_manifest(
    files: dict[str, str],
    uri: str = "blog.alice",
    seed: int = ALICE,
    ttl: int = 30 * 86400,
) -> tuple[dict, str]:
    entries = [
        {"path": p, "hash": sha(c), "size": len(c.encode("utf-8"))}
        for p, c in sorted(files.items())
    ]
    manifest = {
        "uri": uri,
        "author": "alice",
        "version": 1,
        "timestamp": NOW - 60,
        "expires_at": NOW - 60 + ttl,
        "categories": ["computing"],
        "files": entries,
        "total_size": sum(e["size"] for e in entries),
        "file_count": len(entries),
    }
    return sign_manifest(manifest, key(seed))


SITE = {"index.md": "# Hello\n\nWorld — café.\n", "posts/one.md": "# One\r\nCRLF\r\n"}


def gen_manifest_verify() -> list[dict]:
    manifest, sig = make_manifest(SITE)
    cases = [("valid", manifest, sig, pub(ALICE), True)]

    tampered = copy.deepcopy(manifest)
    tampered["version"] = 2
    cases.append(("field_tampered_after_signing", tampered, sig, pub(ALICE), False))
    cases.append(("signature_bit_flipped", manifest, flip_sig(sig), pub(ALICE), False))
    cases.append(("wrong_trusted_key", manifest, sig, pub(MALLORY), False))

    # Mallory re-signs Alice's manifest with her own key but keeps Alice's
    # public_key field: the embedded key no longer matches the signer.
    forged = dict(manifest)
    forged_sig = base64.b64encode(key(MALLORY).sign(_canonical_json(forged))).decode()
    cases.append(("signed_by_other_key", forged, forged_sig, pub(ALICE), False))

    # Signature valid for Mallory's own manifest, but the trusted key
    # (from the naming record) is Alice's.
    m_manifest, m_sig = make_manifest(SITE, seed=MALLORY)
    cases.append(("embedded_key_not_trusted", m_manifest, m_sig, pub(ALICE), False))

    return [
        {
            "name": name,
            "manifest": m,
            "signature": s,
            "trusted_public_key": t,
            "valid": v,
            "manifest_ref": compute_manifest_ref(m),
        }
        for name, m, s, t, v in cases
    ]


def gen_content_key() -> list[dict]:
    return [
        {"uri": uri, "public_key": pk, "content_key": compute_content_key(uri, pk)}
        for uri, pk in [("blog.alice", pub(ALICE)), ("x", pub(MALLORY))]
    ]


# ─── naming records ───────────────────────────────────────────────────


def gen_name_records() -> list[dict]:
    from bundle import verify_name_record

    def case(name, record, sig, max_drift):
        with mock.patch("time.time", return_value=float(NOW)):
            ok, _ = verify_name_record(record, sig, max_drift=max_drift)
        return {
            "name": name,
            "record": record,
            "signature": sig,
            "now": NOW,
            "max_drift": max_drift,
            "valid": ok,
        }

    record = build_name_record("blog.alice", "alice", pub(ALICE), "ab" * 32, NOW)
    sig = sign_name_record(record, key(ALICE))
    stale = build_name_record("blog.alice", "alice", pub(ALICE), "ab" * 32, NOW - 3600)
    stale_sig = sign_name_record(stale, key(ALICE))
    swapped = dict(record, uri="bank")
    missing = {k: v for k, v in record.items() if k != "manifest_ref"}
    missing_sig = base64.b64encode(key(ALICE).sign(_canonical_json(missing))).decode()
    return [
        case("valid", record, sig, 300),
        case("valid_no_drift_check", stale, stale_sig, None),
        case("stale_with_drift_check", stale, stale_sig, 300),
        case("uri_swapped", swapped, sig, 300),
        case("bad_signature", record, flip_sig(sig), 300),
        case("missing_field", missing, missing_sig, 300),
    ]


# ─── review records ───────────────────────────────────────────────────


def gen_reviews() -> list[dict]:
    from review import (
        verify_review_assignment,
        verify_review_record,
        verify_reviewer_opt_in,
    )

    verifiers = {
        "review_record": verify_review_record,
        "reviewer_opt_in": verify_reviewer_opt_in,
        "review_assignment": verify_review_assignment,
    }
    out = []

    def case(kind, name, record, sig, max_drift=300):
        with mock.patch("time.time", return_value=float(NOW)):
            ok, _ = verifiers[kind](record, sig, max_drift=max_drift)
        out.append(
            {
                "kind": kind,
                "name": name,
                "record": record,
                "signature": sig,
                "now": NOW,
                "max_drift": max_drift,
                "valid": ok,
            }
        )

    content_key = compute_content_key("blog.alice", pub(ALICE))
    review = build_review_record(content_key, pub(REVIEWER), "warn", "sources?", NOW)
    rsig = sign_review_record(review, key(REVIEWER))
    case("review_record", "valid", review, rsig)
    case("review_record", "verdict_tampered", dict(review, verdict="reject"), rsig)
    bad_verdict = dict(review, verdict="meh")
    case(
        "review_record",
        "unknown_verdict",
        bad_verdict,
        base64.b64encode(key(REVIEWER).sign(_canonical_json(bad_verdict))).decode(),
    )

    opt_in = build_reviewer_opt_in(
        pub(REVIEWER), "12D3KooWExample", ["/ip4/1.2.3.4/tcp/1707"], ["math"], NOW
    )
    osig = sign_reviewer_opt_in(opt_in, key(REVIEWER))
    case("reviewer_opt_in", "valid", opt_in, osig)
    case("reviewer_opt_in", "categories_tampered", dict(opt_in, categories=[]), osig)
    bad_addrs = dict(opt_in, addrs="not-a-list")
    case(
        "reviewer_opt_in",
        "addrs_not_list",
        bad_addrs,
        base64.b64encode(key(REVIEWER).sign(_canonical_json(bad_addrs))).decode(),
    )

    assignment = build_review_assignment(
        "blog.alice", pub(ALICE), [pub(REVIEWER)], NOW + 86400, NOW
    )
    asig = sign_review_assignment(assignment, key(ALICE))
    case("review_assignment", "valid", assignment, asig)
    # Mallory signs an assignment claiming Alice's content key: the
    # content_key no longer derives from (uri, publisher_public_key).
    hijack = build_review_assignment(
        "blog.alice", pub(MALLORY), [pub(REVIEWER)], NOW + 86400, NOW
    )
    hijack["content_key"] = content_key
    case(
        "review_assignment",
        "content_key_not_derived",
        hijack,
        sign_review_assignment(hijack, key(MALLORY)),
    )
    return out


# ─── URIs and paths ───────────────────────────────────────────────────


def gen_uris() -> list[dict]:
    candidates = [
        "blog.alice", "a", "my-site_2", "A.b-C",
        "", ".hidden", "-dash", "a/b", "a\\b", "..", "a..b", "café",
        "a b", "x" * 255, "x" * 256, "nul\x00byte", "blog\n", " blog",
    ]
    out = []
    for uri in candidates:
        try:
            validate_uri(uri)
            ok = True
        except ValueError:
            ok = False
        out.append({"uri": uri, "valid": ok})
    return out


def gen_paths() -> list[dict]:
    from bundle import validate_path

    base = Path("/nonexistent/mdp2p-vectors/site")
    candidates = [
        "index.md", "posts/one.md", "./index.md", "a//b.md", "a/./b.md",
        "a/../b.md", "dir/", "/".join(["d"] * 9) + "/x.md",
        "../escape.md", "a/../../escape.md", "/etc/passwd.md", "", ".", "a/..",
        "a\\b.md", "nul\x00.md", "/".join(["d"] * 10) + "/x.md",
    ]
    out = []
    for rel in candidates:
        try:
            validate_path(base, rel)
            ok = True
        except ValueError:
            ok = False
        out.append({"path": rel, "valid": ok})
    return out


# ─── downloaded bundle acceptance ─────────────────────────────────────


def gen_bundle_checks() -> list[dict]:
    from peer.peer import _check_downloaded_bundle
    from bundle import b64_to_public_key

    manifest, sig = make_manifest(SITE)
    ref = compute_manifest_ref(manifest)

    def bundle(m=manifest, s=sig, files=None):
        return {"type": "bundle", "manifest": m, "signature": s,
                "files": dict(SITE) if files is None else files}

    cases: list[tuple[str, dict, str, str, str]] = [
        # name, bundle, requested uri, expected_ref, reason
        ("valid", bundle(), "blog.alice", ref, ""),
        ("uri_mismatch", bundle(), "bank", ref, "uri_mismatch"),
        ("ref_mismatch", bundle(), "blog.alice", "00" * 32, "ref_mismatch"),
        ("bad_signature", bundle(s=flip_sig(sig)), "blog.alice", ref, "bad_signature"),
        ("corrupted_file",
         bundle(files=dict(SITE, **{"index.md": "# Pwned"})), "blog.alice", ref,
         "corrupted"),
        ("unlisted_file",
         bundle(files=dict(SITE, **{"extra.md": "x"})), "blog.alice", ref,
         "files_mismatch"),
        ("missing_file",
         bundle(files={"index.md": SITE["index.md"]}), "blog.alice", ref,
         "files_mismatch"),
        ("files_not_object", bundle(files=[]), "blog.alice", ref, "malformed"),
    ]
    expired, expired_sig = make_manifest(SITE, ttl=30)
    cases.append(("expired", bundle(expired, expired_sig), "blog.alice",
                  compute_manifest_ref(expired), "expired"))
    evil = {"../escape.md": "x"}
    ev_m, ev_s = make_manifest(evil)
    cases.append(("path_traversal", bundle(ev_m, ev_s, files=evil), "blog.alice",
                  compute_manifest_ref(ev_m), "bad_path"))
    non_md = {"script.js": "alert(1)"}
    nm_m, nm_s = make_manifest(non_md)
    cases.append(("not_markdown", bundle(nm_m, nm_s, files=non_md), "blog.alice",
                  compute_manifest_ref(nm_m), "not_markdown"))

    out = []
    trusted = b64_to_public_key(pub(ALICE))
    for name, b, uri, expected_ref, reason in cases:
        with mock.patch("time.time", return_value=float(NOW)):
            python_reason = _check_downloaded_bundle(
                copy.deepcopy(b), uri, trusted, expected_ref, "/nonexistent/site"
            )
        assert (python_reason is None) == (reason == ""), (name, python_reason)
        out.append({
            "name": name,
            "bundle": b,
            "uri": uri,
            "trusted_public_key": pub(ALICE),
            "expected_ref": expected_ref,
            "now": NOW,
            "accept": reason == "",
            "reason": reason or None,
        })
    return out


# ─── key pinning ──────────────────────────────────────────────────────


def gen_pins() -> list[dict]:
    from pinstore import check_pin

    entry = {"public_key": pub(ALICE), "author": "alice",
             "first_seen": NOW - 99, "last_seen": NOW - 9,
             "record_timestamp": NOW - 10}
    legacy = {k: v for k, v in entry.items() if k != "record_timestamp"}
    cases = [
        ("unknown", None, pub(ALICE), NOW),
        ("match", entry, pub(ALICE), NOW - 10),
        ("newer_record", entry, pub(ALICE), NOW),
        ("rollback", entry, pub(ALICE), NOW - 11),
        ("key_changed", entry, pub(MALLORY), NOW),
        ("legacy_entry_without_timestamp", legacy, pub(ALICE), 1),
    ]
    out = []
    for name, e, pk, ts in cases:
        store = {} if e is None else {"blog.alice": e}
        out.append({
            "name": name, "entry": e, "public_key": pk, "record_timestamp": ts,
            "status": check_pin(store, "blog.alice", pk, ts).value,
        })
    return out


# ─── reviewer selection ───────────────────────────────────────────────


def gen_selection() -> list[dict]:
    pool = [pub(s) for s in range(10, 20)]
    content_key = compute_content_key("blog.alice", pub(ALICE))
    cases = [
        ("pick_3_of_10", content_key, pool, 3),
        ("duplicates_ignored", content_key, pool[:3] * 3, 2),
        ("n_larger_than_pool", content_key, pool[:2], 5),
        ("n_zero", content_key, pool, 0),
        ("other_content", compute_content_key("x", pub(MALLORY)), pool, 3),
    ]
    return [
        {"name": n, "content_key": ck, "pool": p, "n": k,
         "selected": select_reviewers(ck, p, k)}
        for n, ck, p, k in cases
    ]


GENERATORS: dict[str, Callable[[], list[dict]]] = {
    "canonical_json": gen_canonical_json,
    "manifest_verify": gen_manifest_verify,
    "content_key": gen_content_key,
    "name_records": gen_name_records,
    "reviews": gen_reviews,
    "uris": gen_uris,
    "paths": gen_paths,
    "bundle_checks": gen_bundle_checks,
    "pins": gen_pins,
    "reviewer_selection": gen_selection,
}


def render(name: str) -> str:
    doc = {"version": VECTOR_VERSION, "cases": GENERATORS[name]()}
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--check", action="store_true", help="fail if stale")
    args = parser.parse_args(argv)

    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name in GENERATORS:
        path = VECTORS_DIR / f"{name}.json"
        content = render(name)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path.name)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print(f"stale vectors: {', '.join(stale)} — run scripts/gen_vectors.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
