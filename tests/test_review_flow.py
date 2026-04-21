"""Integration tests for mdp2p_client.review_flow: inbox + attach helpers."""

from contextlib import asynccontextmanager
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
from libp2p.utils.address_validation import find_free_port

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import public_key_to_b64
from mdp2p_client.config import ClientConfig
from mdp2p_client.review_flow import do_attach_review, do_list_inbox
from naming import (
    AssignmentStore,
    AttachmentStore,
    NameStore,
    NamingServer,
    ReviewerStore,
    client_get_attachments,
    client_post_assignment,
)
from peer.reviewer_daemon import ensure_reviewer_identity
from review import build_review_assignment, sign_review_assignment


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


@asynccontextmanager
async def naming_server_env(tmp_path: Path):
    """Run a naming server on localhost; yield its multiaddr."""
    server_host = _fresh_host()
    port = find_free_port()

    async with (
        server_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")]
        ),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(server_host.get_peerstore().start_cleanup_task, 60)

        store = NameStore(str(tmp_path / "names.json"))
        reviewer_store = ReviewerStore(str(tmp_path / "reviewers.json"))
        assignment_store = AssignmentStore(str(tmp_path / "assignments.json"))
        attachment_store = AttachmentStore(str(tmp_path / "attachments.json"))
        server = NamingServer(
            server_host, store, reviewer_store, assignment_store, attachment_store
        )
        server.attach()

        maddr = (
            f"/ip4/127.0.0.1/tcp/{port}/p2p/{server_host.get_id().to_string()}"
        )
        try:
            yield maddr, server_host
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


def _config(tmp_path: Path, naming_maddr: str) -> ClientConfig:
    return ClientConfig(
        author="reviewer-test",
        naming_multiaddr=naming_maddr,
        data_dir=str(tmp_path / "sites"),
        keys_dir=str(tmp_path / "keys"),
        reviewer_dir=str(tmp_path / "rev"),
    )


def _post_assignment(client_host, naming_info, publisher_priv, publisher_pub, reviewer_pub):
    deadline = int(time.time()) + 3 * 86400
    record = build_review_assignment(
        uri="inbox-test",
        publisher_pubkey_b64=publisher_pub,
        reviewer_pubkeys_b64=[reviewer_pub],
        deadline=deadline,
    )
    signature = sign_review_assignment(record, publisher_priv)
    return record, signature


# ─── Tests ─────────────────────────────────────────────────────────────


def test_list_inbox_returns_empty_when_no_assignments(tmp_path):
    async def main():
        async with naming_server_env(tmp_path) as (maddr, _):
            cfg = _config(tmp_path, maddr)
            pending = await do_list_inbox(cfg)
            assert pending == []

    _run(main)


def test_list_inbox_returns_pending_assignment(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())

    async def main():
        async with naming_server_env(tmp_path) as (maddr, _):
            cfg = _config(tmp_path, maddr)
            _, reviewer_pub = ensure_reviewer_identity(cfg.reviewer_dir)

            from libp2p.peer.peerinfo import info_from_p2p_addr
            naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
            record, signature = _post_assignment(
                None, naming_info, publisher_priv, publisher_pub, reviewer_pub
            )

            client_host = _fresh_host()
            async with client_host.run(
                listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/0")]
            ):
                resp = await client_post_assignment(
                    client_host, naming_info, record, signature
                )
                assert resp["type"] == "ok"

            pending = await do_list_inbox(cfg)
            assert len(pending) == 1
            assert pending[0]["record"]["uri"] == "inbox-test"

    _run(main)


def test_list_inbox_drops_expired_assignments(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())

    async def main():
        async with naming_server_env(tmp_path) as (maddr, _):
            cfg = _config(tmp_path, maddr)
            _, reviewer_pub = ensure_reviewer_identity(cfg.reviewer_dir)

            # Build an assignment with a deadline in the past.
            now = int(time.time())
            record = build_review_assignment(
                uri="expired-test",
                publisher_pubkey_b64=publisher_pub,
                reviewer_pubkeys_b64=[reviewer_pub],
                deadline=1,  # unambiguously past
                timestamp=now,
            )
            signature = sign_review_assignment(record, publisher_priv)

            from libp2p.peer.peerinfo import info_from_p2p_addr
            naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))

            client_host = _fresh_host()
            async with client_host.run(
                listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/0")]
            ):
                await client_post_assignment(
                    client_host, naming_info, record, signature
                )

            pending = await do_list_inbox(cfg)
            assert pending == []

    _run(main)


def test_attach_review_posts_signed_record(tmp_path):
    async def main():
        async with naming_server_env(tmp_path) as (maddr, _):
            cfg = _config(tmp_path, maddr)
            _, reviewer_pub = ensure_reviewer_identity(cfg.reviewer_dir)

            content_key = "/mdp2p/some-content"
            await do_attach_review(cfg, content_key, "warn", "needs sources")

            # Verify it landed on the server.
            from libp2p.peer.peerinfo import info_from_p2p_addr
            naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
            client_host = _fresh_host()
            async with client_host.run(
                listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/0")]
            ):
                resp = await client_get_attachments(
                    client_host, naming_info, content_key
                )
            assert len(resp["records"]) == 1
            r = resp["records"][0]["record"]
            assert r["reviewer_public_key"] == reviewer_pub
            assert r["verdict"] == "warn"
            assert r["comment"] == "needs sources"

    _run(main)


def test_attach_review_invalid_verdict_raises(tmp_path):
    async def main():
        async with naming_server_env(tmp_path) as (maddr, _):
            cfg = _config(tmp_path, maddr)
            with pytest.raises(ValueError, match="verdict"):
                await do_attach_review(cfg, "/mdp2p/x", "excellent", "")

    _run(main)
