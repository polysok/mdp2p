"""Tests for the async review side of the naming server:
assignment inbox + post-publication review attachments."""

from contextlib import asynccontextmanager
import json
import secrets
import sys
import time
from pathlib import Path

import multiaddr
import pytest
import trio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import public_key_to_b64
from naming import (
    AssignmentStore,
    AttachmentStore,
    NameStore,
    NamingServer,
    ReviewerStore,
    client_attach_review,
    client_get_attachments,
    client_list_assignments,
    client_post_assignment,
)
from review import (
    build_review_assignment,
    build_review_record,
    sign_review_assignment,
    sign_review_record,
    verify_review_record,
)


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


def _make_pair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, public_key_to_b64(priv.public_key())


@asynccontextmanager
async def env(
    tmp_path: Path,
    with_assignments: bool = True,
    with_attachments: bool = True,
):
    server_host = _fresh_host()
    client_host = _fresh_host()
    server_port = find_free_port()
    client_port = find_free_port()

    async with (
        server_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{server_port}")]
        ),
        client_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{client_port}")]
        ),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(server_host.get_peerstore().start_cleanup_task, 60)
        nursery.start_soon(client_host.get_peerstore().start_cleanup_task, 60)

        store = NameStore(str(tmp_path / "names.json"))
        reviewer_store = ReviewerStore(str(tmp_path / "reviewers.json"))
        assignment_store = (
            AssignmentStore(str(tmp_path / "assignments.json"))
            if with_assignments
            else None
        )
        attachment_store = (
            AttachmentStore(str(tmp_path / "attachments.json"))
            if with_attachments
            else None
        )
        server = NamingServer(
            server_host,
            store,
            reviewer_store,
            assignment_store,
            attachment_store,
        )
        server.attach()

        server_maddr = (
            f"/ip4/127.0.0.1/tcp/{server_port}/p2p/{server_host.get_id().to_string()}"
        )
        server_info = info_from_p2p_addr(multiaddr.Multiaddr(server_maddr))

        try:
            yield client_host, server_info, assignment_store, attachment_store
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


# ─── Assignment inbox ──────────────────────────────────────────────────


def test_post_and_list_assignment(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer_pub_1 = _make_pair()
    _, reviewer_pub_2 = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    record = build_review_assignment(
        content_key="/mdp2p/abc",
        publisher_pubkey_b64=publisher_pub,
        reviewer_pubkeys_b64=[reviewer_pub_1, reviewer_pub_2],
        deadline=deadline,
    )
    signature = sign_review_assignment(record, publisher_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_post_assignment(client, server_info, record, signature)
            assert resp["type"] == "ok"
            assert resp["content_key"] == "/mdp2p/abc"

            for reviewer in (reviewer_pub_1, reviewer_pub_2):
                listing = await client_list_assignments(client, server_info, reviewer)
                assert listing["type"] == "assignments"
                assert len(listing["records"]) == 1
                assert listing["records"][0]["record"] == record

    _run(main)


def test_assignment_not_visible_to_non_selected_reviewer(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, selected = _make_pair()
    _, bystander = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    record = build_review_assignment(
        "/mdp2p/abc", publisher_pub, [selected], deadline
    )
    signature = sign_review_assignment(record, publisher_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            await client_post_assignment(client, server_info, record, signature)
            listing = await client_list_assignments(client, server_info, bystander)
            assert listing["records"] == []

    _run(main)


def test_post_assignment_rejects_tampered_signature(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    record = build_review_assignment(
        "/mdp2p/abc", publisher_pub, [reviewer], deadline
    )
    signature = sign_review_assignment(record, publisher_priv)
    record["reviewer_public_keys"] = [reviewer, "attacker"]  # post-sign tamper

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_post_assignment(client, server_info, record, signature)
            assert resp["type"] == "error"
            assert "invalid signature" in resp["msg"]

    _run(main)


def test_newer_assignment_replaces_older_for_same_content(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer = _make_pair()
    now = int(time.time())
    deadline = now + 3 * 86400

    first = build_review_assignment(
        "/mdp2p/abc", publisher_pub, [reviewer], deadline, timestamp=now
    )
    sig_first = sign_review_assignment(first, publisher_priv)
    second = build_review_assignment(
        "/mdp2p/abc",
        publisher_pub,
        [reviewer, "extra"],  # selection changed
        deadline,
        timestamp=now + 1,
    )
    sig_second = sign_review_assignment(second, publisher_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            await client_post_assignment(client, server_info, first, sig_first)
            await client_post_assignment(client, server_info, second, sig_second)

            listing = await client_list_assignments(client, server_info, reviewer)
            assert len(listing["records"]) == 1
            stored = listing["records"][0]["record"]
            assert stored["reviewer_public_keys"] == [reviewer, "extra"]

    _run(main)


def test_distinct_content_keys_coexist_in_inbox(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            for ck in ("/mdp2p/a", "/mdp2p/b", "/mdp2p/c"):
                r = build_review_assignment(ck, publisher_pub, [reviewer], deadline)
                sig = sign_review_assignment(r, publisher_priv)
                await client_post_assignment(client, server_info, r, sig)

            listing = await client_list_assignments(client, server_info, reviewer)
            assert len(listing["records"]) == 3
            keys = {e["record"]["content_key"] for e in listing["records"]}
            assert keys == {"/mdp2p/a", "/mdp2p/b", "/mdp2p/c"}

    _run(main)


def test_post_assignment_errors_without_inbox(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    record = build_review_assignment(
        "/mdp2p/abc", publisher_pub, [reviewer], deadline
    )
    signature = sign_review_assignment(record, publisher_priv)

    async def main():
        async with env(tmp_path, with_assignments=False) as (client, server_info, _, _):
            resp = await client_post_assignment(
                client, server_info, record, signature
            )
            assert resp["type"] == "error"
            assert "inbox disabled" in resp["msg"]

    _run(main)


# ─── Attachment store ──────────────────────────────────────────────────


def test_attach_and_get_review(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    content_key = "/mdp2p/abc"

    record = build_review_record(
        content_key=content_key,
        reviewer_pubkey_b64=reviewer_pub,
        verdict="warn",
        comment="needs sources",
    )
    signature = sign_review_record(record, reviewer_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_attach_review(client, server_info, record, signature)
            assert resp["type"] == "ok"

            listing = await client_get_attachments(client, server_info, content_key)
            assert listing["type"] == "attachments"
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"] == record

    _run(main)


def test_attachments_are_locally_verifiable(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    record = build_review_record("/mdp2p/xyz", reviewer_pub, "ok")
    signature = sign_review_record(record, reviewer_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            await client_attach_review(client, server_info, record, signature)
            listing = await client_get_attachments(client, server_info, "/mdp2p/xyz")
            for entry in listing["records"]:
                ok, err = verify_review_record(entry["record"], entry["signature"])
                assert ok, err

    _run(main)


def test_attach_rejects_tampered_signature(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    record = build_review_record("/mdp2p/abc", reviewer_pub, "ok")
    signature = sign_review_record(record, reviewer_priv)
    record["verdict"] = "reject"  # post-sign tamper

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_attach_review(client, server_info, record, signature)
            assert resp["type"] == "error"
            assert "invalid signature" in resp["msg"]

    _run(main)


def test_multiple_reviewers_coexist_per_content_key(tmp_path):
    content_key = "/mdp2p/abc"
    reviewers = [_make_pair() for _ in range(3)]

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            for priv, pub in reviewers:
                record = build_review_record(content_key, pub, "ok")
                sig = sign_review_record(record, priv)
                await client_attach_review(client, server_info, record, sig)

            listing = await client_get_attachments(client, server_info, content_key)
            pubkeys = {e["record"]["reviewer_public_key"] for e in listing["records"]}
            assert pubkeys == {pub for _, pub in reviewers}

    _run(main)


def test_same_reviewer_can_amend_with_newer_timestamp(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    content_key = "/mdp2p/abc"
    now = int(time.time())

    first = build_review_record(content_key, reviewer_pub, "ok", timestamp=now)
    sig_first = sign_review_record(first, reviewer_priv)
    amended = build_review_record(
        content_key, reviewer_pub, "reject", timestamp=now + 1
    )
    sig_amended = sign_review_record(amended, reviewer_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            await client_attach_review(client, server_info, first, sig_first)
            resp = await client_attach_review(client, server_info, amended, sig_amended)
            assert resp["type"] == "ok"

            listing = await client_get_attachments(client, server_info, content_key)
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"]["verdict"] == "reject"

    _run(main)


def test_older_attachment_is_ignored(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    content_key = "/mdp2p/abc"
    now = int(time.time())

    fresh = build_review_record(content_key, reviewer_pub, "reject", timestamp=now)
    sig_fresh = sign_review_record(fresh, reviewer_priv)
    stale = build_review_record(content_key, reviewer_pub, "ok", timestamp=now - 10)
    sig_stale = sign_review_record(stale, reviewer_priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _, _):
            await client_attach_review(client, server_info, fresh, sig_fresh)
            resp = await client_attach_review(client, server_info, stale, sig_stale)
            assert resp["type"] == "error"
            assert "existing attachment" in resp["msg"]

            listing = await client_get_attachments(client, server_info, content_key)
            assert listing["records"][0]["record"]["verdict"] == "reject"

    _run(main)


def test_get_attachments_returns_empty_without_store(tmp_path):
    async def main():
        async with env(tmp_path, with_attachments=False) as (client, server_info, _, _):
            listing = await client_get_attachments(client, server_info, "/mdp2p/abc")
            assert listing["type"] == "attachments"
            assert listing["records"] == []

    _run(main)


# ─── Persistence ───────────────────────────────────────────────────────


def test_assignment_inbox_survives_restart(tmp_path):
    publisher_priv, publisher_pub = _make_pair()
    _, reviewer = _make_pair()
    deadline = int(time.time()) + 3 * 86400

    record = build_review_assignment(
        "/mdp2p/abc", publisher_pub, [reviewer], deadline
    )
    signature = sign_review_assignment(record, publisher_priv)

    async def write():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_post_assignment(client, server_info, record, signature)
            assert resp["type"] == "ok"

    _run(write)

    assignments_file = tmp_path / "assignments.json"
    assert assignments_file.exists()
    raw = json.loads(assignments_file.read_text())
    assert reviewer in raw
    assert "/mdp2p/abc" in raw[reviewer]

    async def read_back():
        async with env(tmp_path) as (client, server_info, _, _):
            listing = await client_list_assignments(client, server_info, reviewer)
            assert len(listing["records"]) == 1

    _run(read_back)


def test_attachments_survive_restart(tmp_path):
    reviewer_priv, reviewer_pub = _make_pair()
    content_key = "/mdp2p/abc"
    record = build_review_record(content_key, reviewer_pub, "warn")
    signature = sign_review_record(record, reviewer_priv)

    async def write():
        async with env(tmp_path) as (client, server_info, _, _):
            resp = await client_attach_review(client, server_info, record, signature)
            assert resp["type"] == "ok"

    _run(write)

    attachments_file = tmp_path / "attachments.json"
    assert attachments_file.exists()

    async def read_back():
        async with env(tmp_path) as (client, server_info, _, _):
            listing = await client_get_attachments(client, server_info, content_key)
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"]["verdict"] == "warn"

    _run(read_back)
