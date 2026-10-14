"""Anti-abuse rules of the naming server (strict_reviews + pagination + limits)."""

import sys
import time
from pathlib import Path

import trio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import naming
from bundle import build_name_record, compute_content_key, sign_name_record
from naming import (
    MAX_REVIEWERS_PER_ASSIGNMENT,
    NAMING_PROTOCOL,
    _rpc,
    client_attach_review,
    client_get_attachments,
    client_list,
    client_post_assignment,
    client_register,
    client_register_reviewer,
)
from review import (
    build_review_assignment,
    build_review_record,
    build_reviewer_opt_in,
    sign_review_assignment,
    sign_review_record,
    sign_reviewer_opt_in,
)
from tests.test_naming_async_review import _make_pair, _run, env

URI = "blog.alice"


async def _register_reviewer(client, server_info, priv, pub) -> None:
    record = build_reviewer_opt_in(pub, "12D3KooWfake", ["/ip4/127.0.0.1/tcp/1"])
    resp = await client_register_reviewer(
        client, server_info, record, sign_reviewer_opt_in(record, priv)
    )
    assert resp["type"] == "ok", resp


async def _post_assignment(client, server_info, publisher, reviewers, deadline):
    pub_priv, pub_b64 = publisher
    record = build_review_assignment(URI, pub_b64, reviewers, deadline)
    return await client_post_assignment(
        client, server_info, record, sign_review_assignment(record, pub_priv)
    )


async def _attach(
    client, server_info, reviewer, content_key, verdict="reject", ts=None
):
    priv, pub = reviewer
    record = build_review_record(content_key, pub, verdict, timestamp=ts)
    return await client_attach_review(
        client, server_info, record, sign_review_record(record, priv)
    )


def test_unassigned_sybil_reviews_are_rejected(tmp_path):
    """Throwaway keys must not be able to pile 'reject' reviews on content."""
    _, publisher_pub = publisher = _make_pair()
    content_key = compute_content_key(URI, publisher_pub)

    async def main():
        async with env(tmp_path, strict_reviews=True) as (client, info, _, _):
            for _ in range(5):
                resp = await _attach(client, info, _make_pair(), content_key)
                assert resp["type"] == "error"
                assert "not assigned" in resp["msg"]
            listing = await client_get_attachments(client, info, content_key)
            assert listing["records"] == []

    _run(main)


def test_assigned_reviewer_can_attach(tmp_path):
    publisher = _make_pair()
    reviewer = _make_pair()
    content_key = compute_content_key(URI, publisher[1])

    async def main():
        async with env(tmp_path, strict_reviews=True) as (client, info, _, _):
            await _register_reviewer(client, info, *reviewer)
            resp = await _post_assignment(
                client, info, publisher, [reviewer[1]], int(time.time()) + 3600
            )
            assert resp["type"] == "ok", resp

            resp = await _attach(client, info, reviewer, content_key, "warn")
            assert resp["type"] == "ok", resp

            # A different, unassigned key is still refused for this content.
            resp = await _attach(client, info, _make_pair(), content_key)
            assert resp["type"] == "error"

    _run(main)


def test_review_after_deadline_is_rejected(tmp_path):
    publisher = _make_pair()
    reviewer = _make_pair()
    content_key = compute_content_key(URI, publisher[1])
    now = int(time.time())

    async def main():
        async with env(tmp_path, strict_reviews=True) as (client, info, _, _):
            await _register_reviewer(client, info, *reviewer)
            resp = await _post_assignment(
                client, info, publisher, [reviewer[1]], now + 60
            )
            assert resp["type"] == "ok", resp

            resp = await _attach(client, info, reviewer, content_key, ts=now + 120)
            assert resp["type"] == "error"
            assert "deadline" in resp["msg"]

    _run(main)


def test_assignment_to_unregistered_reviewer_is_rejected(tmp_path):
    """Publishers cannot spam arbitrary inboxes or hand-pick unknown keys."""
    publisher = _make_pair()

    async def main():
        async with env(tmp_path, strict_reviews=True) as (client, info, _, _):
            resp = await _post_assignment(
                client, info, publisher, [_make_pair()[1]], int(time.time()) + 3600
            )
            assert resp["type"] == "error"
            assert "not registered" in resp["msg"]

    _run(main)


def test_assignment_reviewer_count_is_capped(tmp_path):
    publisher = _make_pair()
    reviewers = [_make_pair() for _ in range(MAX_REVIEWERS_PER_ASSIGNMENT + 1)]

    async def main():
        async with env(tmp_path, strict_reviews=True) as (client, info, _, _):
            for reviewer in reviewers:
                await _register_reviewer(client, info, *reviewer)
            resp = await _post_assignment(
                client,
                info,
                publisher,
                [pub for _, pub in reviewers],
                int(time.time()) + 3600,
            )
            assert resp["type"] == "error"
            assert "too many reviewers" in resp["msg"]

    _run(main)


def test_list_is_paginated_and_client_merges_pages(tmp_path, monkeypatch):
    """Responses stay under MAX_MSG_SIZE however many names are registered."""
    monkeypatch.setattr(naming, "DEFAULT_PAGE_SIZE", 2)
    priv, pub = _make_pair()
    uris = [f"site.{i}" for i in range(5)]

    async def main():
        async with env(tmp_path) as (client, info, _, _):
            for uri in uris:
                record = build_name_record(uri, "alice", pub, "0" * 64)
                resp = await client_register(
                    client, info, record, sign_name_record(record, priv)
                )
                assert resp["type"] == "ok"

            first = await _rpc(client, info, {"type": "list"})
            assert len(first["records"]) == 2
            assert first["next_offset"] == 2

            merged = await client_list(client, info)
            assert sorted(r["uri"] for r in merged["records"]) == uris

    _run(main)


def test_oversized_request_is_rejected(tmp_path):
    """A request announcing a huge frame is refused without buffering it."""
    import struct

    from wire import recv_framed_json

    async def main():
        async with env(tmp_path) as (client, info, _, _):
            await client.connect(info)
            stream = await client.new_stream(info.peer_id, [NAMING_PROTOCOL])
            await stream.write(struct.pack("!I", 512 * 1024) + b"{")
            with trio.fail_after(5):
                resp = await recv_framed_json(stream, naming.MAX_MSG_SIZE)
            assert resp["type"] == "error"
            await stream.close()

    _run(main)


def test_idle_client_is_disconnected(tmp_path, monkeypatch):
    """A client that opens a stream and never writes cannot pin the server."""
    from wire import recv_framed_json

    monkeypatch.setattr(naming, "REQUEST_READ_TIMEOUT", 0.5)

    async def main():
        async with env(tmp_path) as (client, info, _, _):
            await client.connect(info)
            stream = await client.new_stream(info.peer_id, [NAMING_PROTOCOL])
            with trio.fail_after(5):
                resp = await recv_framed_json(stream, naming.MAX_MSG_SIZE)
            assert resp["type"] == "error"
            await stream.close()

    _run(main)
