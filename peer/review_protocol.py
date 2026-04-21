"""The ``/mdp2p/review/1.0.0`` wire protocol.

Symmetric to ``bundle_protocol``: protocol constant, reviewer-side stream
handler factory, and a publisher-side parallel request helper. Kept
separate from the ``Peer`` class so the latter stays focused on policy.

Role asymmetry:
  - Reviewer: runs the stream handler, inspects incoming manifests,
    consults a local callback (which may prompt the user, apply policy,
    or auto-decline), and returns a signed ReviewRecord or a decline.
  - Publisher: selects reviewers, fans out review requests in parallel,
    collects signed responses until the deadline and returns whatever
    arrived in time — never blocks publication indefinitely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import multiaddr
import trio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.abc import IHost
from libp2p.custom_types import TProtocol
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.relay.circuit_v2.transport import CircuitV2Transport

from bundle import b64_to_public_key, compute_content_key, verify_manifest
from review import (
    build_review_record,
    sign_review_record,
    verify_review_record,
)
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.peer.review")

REVIEW_PROTOCOL = TProtocol("/mdp2p/review/1.0.0")
MAX_REVIEW_MSG_SIZE = 10 * 1024 * 1024  # 10 MiB — markdown payloads plus slack

DEFAULT_REVIEW_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ReviewRequest:
    """What the callback sees when deciding whether to issue a verdict."""

    content_key: str
    manifest: dict
    manifest_signature: str


@dataclass(frozen=True)
class ReviewVerdict:
    """What the callback returns to issue a signed verdict."""

    verdict: str  # "ok" | "warn" | "reject"
    comment: str = ""


# Async so callbacks can prompt the UI or call out to any other async code.
ReviewerCallback = Callable[[ReviewRequest], Awaitable[Optional[ReviewVerdict]]]


async def auto_decline(_request: ReviewRequest) -> Optional[ReviewVerdict]:
    """Default callback: never issue a verdict — always decline politely."""
    return None


@dataclass(frozen=True)
class CollectedReview:
    """A successfully-fetched and signature-verified review."""

    record: dict
    signature: str


# ─── Reviewer side ────────────────────────────────────────────────────


def make_review_handler(
    reviewer_private_key: Ed25519PrivateKey,
    reviewer_public_key_b64: str,
    callback: ReviewerCallback = auto_decline,
) -> Callable[[INetStream], Awaitable[None]]:
    """Build the stream handler for REVIEW_PROTOCOL.

    The handler validates the incoming manifest (correct author signature,
    matching content key) before handing it to the callback, so callbacks
    can trust what they see.
    """

    async def handler(stream: INetStream) -> None:
        try:
            msg = await recv_framed_json(stream, MAX_REVIEW_MSG_SIZE)
            if msg is None or msg.get("type") != "review_request":
                await _send_error(stream, "malformed review request")
                return

            content_key = msg.get("content_key", "")
            manifest = msg.get("manifest") or {}
            manifest_signature = msg.get("manifest_signature", "")
            if not content_key or not manifest or not manifest_signature:
                await _send_error(stream, "missing fields in review request")
                return

            author_pub_b64 = manifest.get("public_key", "")
            if not author_pub_b64:
                await _send_error(stream, "manifest missing public_key")
                return

            expected_key = compute_content_key(
                manifest.get("uri", ""), author_pub_b64
            )
            if expected_key != content_key:
                await _send_error(
                    stream, "content_key does not match manifest (uri, public_key)"
                )
                return

            if not verify_manifest(
                manifest, manifest_signature, b64_to_public_key(author_pub_b64)
            ):
                await _send_error(stream, "invalid manifest signature")
                return

            request = ReviewRequest(content_key, manifest, manifest_signature)
            verdict = await callback(request)
            if verdict is None:
                await send_framed_json(
                    stream,
                    {"type": "review_decline", "reason": "no verdict emitted"},
                    MAX_REVIEW_MSG_SIZE,
                )
                return

            record = build_review_record(
                content_key=content_key,
                reviewer_pubkey_b64=reviewer_public_key_b64,
                verdict=verdict.verdict,
                comment=verdict.comment,
            )
            signature = sign_review_record(record, reviewer_private_key)
            await send_framed_json(
                stream,
                {
                    "type": "review_response",
                    "record": record,
                    "signature": signature,
                },
                MAX_REVIEW_MSG_SIZE,
            )
        except Exception as e:
            logger.exception("review handler error")
            await _send_error(stream, f"internal error: {e}")
        finally:
            await stream.close()

    return handler


# ─── Publisher side ───────────────────────────────────────────────────


async def request_reviews(
    host: IHost,
    relay_transport: Optional[CircuitV2Transport],
    content_key: str,
    manifest: dict,
    manifest_signature: str,
    reviewer_addrs: list[str],
    timeout_seconds: float = DEFAULT_REVIEW_TIMEOUT_SECONDS,
) -> list[CollectedReview]:
    """Fan out review requests in parallel; collect verified responses.

    Returns whatever reviews arrived before ``timeout_seconds`` elapsed.
    Publication never blocks on missing or slow reviewers: the caller
    proceeds with whatever list is returned (including an empty list).
    """
    collected: list[CollectedReview] = []
    lock = trio.Lock()

    async def try_one(addr_str: str) -> None:
        try:
            maddr = multiaddr.Multiaddr(addr_str)
            info = info_from_p2p_addr(maddr)
            if "/p2p-circuit/" in addr_str and relay_transport is not None:
                # Relay dials need the destination peer in the peerstore.
                host.get_peerstore().add_addrs(info.peer_id, [maddr], 3600)
                await relay_transport.dial(maddr)
            else:
                await host.connect(info)
            stream = await host.new_stream(info.peer_id, [REVIEW_PROTOCOL])
            try:
                await send_framed_json(
                    stream,
                    {
                        "type": "review_request",
                        "content_key": content_key,
                        "manifest": manifest,
                        "manifest_signature": manifest_signature,
                    },
                    MAX_REVIEW_MSG_SIZE,
                )
                response = await recv_framed_json(stream, MAX_REVIEW_MSG_SIZE)
            finally:
                await stream.close()

            if not response or response.get("type") != "review_response":
                return

            record = response.get("record") or {}
            signature = response.get("signature", "")
            ok, err = verify_review_record(record, signature)
            if not ok:
                logger.warning("invalid review signature from %s: %s", addr_str, err)
                return
            # A reviewer that signs a review for a different content_key
            # than what was requested is either buggy or malicious.
            if record.get("content_key") != content_key:
                logger.warning(
                    "reviewer %s returned review for wrong content_key", addr_str
                )
                return

            async with lock:
                collected.append(
                    CollectedReview(record=record, signature=signature)
                )
        except Exception as e:
            logger.warning("review request to %s failed: %s", addr_str, e)

    with trio.move_on_after(timeout_seconds):
        async with trio.open_nursery() as nursery:
            for addr in reviewer_addrs:
                nursery.start_soon(try_one, addr)

    return collected


async def _send_error(stream: INetStream, message: str) -> None:
    try:
        await send_framed_json(
            stream, {"type": "error", "msg": message}, MAX_REVIEW_MSG_SIZE
        )
    except Exception:
        pass
