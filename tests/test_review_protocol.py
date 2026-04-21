"""Tests for /mdp2p/review/1.0.0 wire protocol."""

from contextlib import AsyncExitStack, asynccontextmanager
import secrets
import sys
from pathlib import Path

import multiaddr
import pytest
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import (
    compute_content_key,
    create_manifest,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_to_b64,
    sign_manifest,
)
from peer.review_protocol import (
    REVIEW_PROTOCOL,
    ReviewRequest,
    ReviewVerdict,
    auto_decline,
    make_review_handler,
    request_reviews,
)
from review import verify_review_record


# ─── Fixtures ───────────────────────────────────────────────────────────


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


def _signed_manifest(tmp_path: Path, uri: str = "blog") -> tuple[dict, str, str]:
    """Build a real signed manifest; return (manifest, signature_b64, content_key)."""
    site = tmp_path / f"site_{uri}"
    site.mkdir()
    (site / "index.md").write_text("# Hello\n", encoding="utf-8")

    keys = tmp_path / f"keys_{uri}"
    priv_path, pub_path = generate_keypair(str(keys), "author")
    private_key = load_private_key(priv_path)
    pub_b64 = public_key_to_b64(load_public_key(pub_path))

    manifest = create_manifest(str(site), uri=uri, author="alice", version=1)
    manifest, signature = sign_manifest(manifest, private_key)
    content_key = compute_content_key(uri, pub_b64)
    return manifest, signature, content_key


def _reviewer_keypair(tmp_path: Path, name: str = "reviewer"):
    """Return (private_key, public_key_b64) for a reviewer."""
    priv_path, pub_path = generate_keypair(str(tmp_path / f"keys_{name}"), name)
    return load_private_key(priv_path), public_key_to_b64(load_public_key(pub_path))


@asynccontextmanager
async def review_env(
    reviewer_handlers: list[tuple[object, str, object]],
):
    """Spawn one publisher host plus one host per reviewer_handler entry.

    Each reviewer_handler entry is (private_key, public_key_b64, callback).
    Yields (publisher_host, [reviewer_addr_str, ...]).
    """
    publisher = _fresh_host()
    pub_port = find_free_port()

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            publisher.run(
                listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{pub_port}")]
            )
        )

        reviewer_addrs: list[str] = []
        reviewer_hosts = []
        for priv, pub_b64, cb in reviewer_handlers:
            host = _fresh_host()
            port = find_free_port()
            await stack.enter_async_context(
                host.run(
                    listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")]
                )
            )
            host.set_stream_handler(
                REVIEW_PROTOCOL,
                make_review_handler(priv, pub_b64, cb),
            )
            reviewer_addrs.append(
                f"/ip4/127.0.0.1/tcp/{port}/p2p/{host.get_id().to_string()}"
            )
            reviewer_hosts.append(host)

        nursery = await stack.enter_async_context(trio.open_nursery())
        nursery.start_soon(publisher.get_peerstore().start_cleanup_task, 60)
        for host in reviewer_hosts:
            nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        try:
            yield publisher, reviewer_addrs
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


# ─── Happy path ─────────────────────────────────────────────────────────


def test_single_reviewer_returns_signed_verdict(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv, pub_b64 = _reviewer_keypair(tmp_path)

    async def cb(req: ReviewRequest):
        assert req.content_key == content_key
        return ReviewVerdict(verdict="ok", comment="reads fine")

    async def main():
        async with review_env([(priv, pub_b64, cb)]) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert len(reviews) == 1
            record = reviews[0].record
            assert record["verdict"] == "ok"
            assert record["comment"] == "reads fine"
            assert record["reviewer_public_key"] == pub_b64
            # Each review is independently verifiable.
            ok, err = verify_review_record(record, reviews[0].signature)
            assert ok, err

    _run(main)


def test_multiple_reviewers_all_collected(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    reviewers = []
    for i, verdict in enumerate(("ok", "warn", "reject")):
        priv, pub_b64 = _reviewer_keypair(tmp_path, name=f"rev{i}")
        v = ReviewVerdict(verdict=verdict, comment=f"rev{i}")

        async def cb(_req, v=v):
            return v

        reviewers.append((priv, pub_b64, cb))

    async def main():
        async with review_env(reviewers) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert len(reviews) == 3
            verdicts = {r.record["verdict"] for r in reviews}
            assert verdicts == {"ok", "warn", "reject"}

    _run(main)


# ─── Decline / abstention ──────────────────────────────────────────────


def test_auto_decline_default_yields_no_review(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv, pub_b64 = _reviewer_keypair(tmp_path)

    async def main():
        async with review_env([(priv, pub_b64, auto_decline)]) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert reviews == []

    _run(main)


def test_mixed_decline_and_verdict(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv1, pub1 = _reviewer_keypair(tmp_path, name="rev1")
    priv2, pub2 = _reviewer_keypair(tmp_path, name="rev2")

    async def decline(_req):
        return None

    async def approve(_req):
        return ReviewVerdict(verdict="ok")

    async def main():
        async with review_env(
            [(priv1, pub1, decline), (priv2, pub2, approve)]
        ) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert len(reviews) == 1
            assert reviews[0].record["reviewer_public_key"] == pub2

    _run(main)


# ─── Validation / tampering ────────────────────────────────────────────


def test_tampered_manifest_rejected_by_reviewer(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    # Flip a file hash post-sign — the reviewer should refuse to issue a verdict.
    manifest["files"][0]["hash"] = "0" * 64
    priv, pub_b64 = _reviewer_keypair(tmp_path)

    called = [False]

    async def cb(_req):
        called[0] = True
        return ReviewVerdict(verdict="ok")

    async def main():
        async with review_env([(priv, pub_b64, cb)]) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert reviews == []
            assert called[0] is False, "callback must not run on invalid manifest"

    _run(main)


def test_mismatched_content_key_rejected(tmp_path):
    manifest, sig, _ = _signed_manifest(tmp_path)
    priv, pub_b64 = _reviewer_keypair(tmp_path)
    wrong_key = "/mdp2p/deadbeef"

    called = [False]

    async def cb(_req):
        called[0] = True
        return ReviewVerdict(verdict="ok")

    async def main():
        async with review_env([(priv, pub_b64, cb)]) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, wrong_key, manifest, sig, addrs,
                timeout_seconds=10,
            )
            assert reviews == []
            assert called[0] is False

    _run(main)


# ─── Timeout ───────────────────────────────────────────────────────────


def test_slow_reviewer_is_dropped_by_timeout(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv, pub_b64 = _reviewer_keypair(tmp_path)

    async def slow_cb(_req):
        await trio.sleep(5)  # much longer than the test timeout
        return ReviewVerdict(verdict="ok")

    async def main():
        async with review_env([(priv, pub_b64, slow_cb)]) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=0.5,
            )
            assert reviews == []

    _run(main)


def test_fast_reviewer_kept_when_another_is_slow(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv_fast, pub_fast = _reviewer_keypair(tmp_path, name="fast")
    priv_slow, pub_slow = _reviewer_keypair(tmp_path, name="slow")

    async def fast_cb(_req):
        return ReviewVerdict(verdict="ok")

    async def slow_cb(_req):
        await trio.sleep(5)
        return ReviewVerdict(verdict="reject")

    async def main():
        async with review_env(
            [(priv_fast, pub_fast, fast_cb), (priv_slow, pub_slow, slow_cb)]
        ) as (publisher, addrs):
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, addrs,
                timeout_seconds=1.0,
            )
            assert len(reviews) == 1
            assert reviews[0].record["reviewer_public_key"] == pub_fast

    _run(main)


# ─── Unreachable reviewer ──────────────────────────────────────────────


def test_unreachable_reviewer_does_not_block_others(tmp_path):
    manifest, sig, content_key = _signed_manifest(tmp_path)
    priv, pub_b64 = _reviewer_keypair(tmp_path)

    async def cb(_req):
        return ReviewVerdict(verdict="ok")

    async def main():
        async with review_env([(priv, pub_b64, cb)]) as (publisher, addrs):
            # Add a bogus reviewer address that can't be dialed.
            bogus = (
                "/ip4/127.0.0.1/tcp/1"
                "/p2p/12D3KooWQYhTNQdmPWHBHCoDczcaN6Q2nGm7P5sFMFq9sKXoxxxx"
            )
            reviews = await request_reviews(
                publisher, None, content_key, manifest, sig, [bogus, *addrs],
                timeout_seconds=5.0,
            )
            assert len(reviews) == 1
            assert reviews[0].record["reviewer_public_key"] == pub_b64

    _run(main)
