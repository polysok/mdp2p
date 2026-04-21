"""
MDP2P Naming — minimal libp2p service that maps md:// URIs to author-signed records.

Each record binds:
  uri → {author, public_key, manifest_ref, timestamp}

The record is signed with the author's ed25519 private key. The naming server
never fabricates records — it only stores and serves records it could verify,
so it is non-trusted: clients re-verify signatures upon resolve.

Wire protocol: /mdp2p/naming/1.0.0
  register          : {"type": "register", "record": {...}, "signature": "<b64>"}
  resolve           : {"type": "resolve",  "uri": "<uri>"}
  list              : {"type": "list"}
  register_reviewer : {"type": "register_reviewer", "record": {...}, "signature": "<b64>"}
  list_reviewers    : {"type": "list_reviewers"}
  post_assignment   : {"type": "post_assignment", "record": {...}, "signature": "<b64>"}
  list_assignments  : {"type": "list_assignments", "reviewer_public_key": "<b64>"}
  attach_review     : {"type": "attach_review", "record": {...}, "signature": "<b64>"}
  get_attachments   : {"type": "get_attachments", "content_key": "<str>"}

Every message on a stream is length-prefixed: [4 bytes big-endian length][JSON].
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Optional

import multiaddr
import trio
from libp2p import new_host
from libp2p.abc import IHost
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.peerinfo import PeerInfo

from bundle import validate_uri, verify_name_record
from review import (
    verify_review_assignment,
    verify_review_record,
    verify_reviewer_opt_in,
)
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.naming")

NAMING_PROTOCOL = TProtocol("/mdp2p/naming/1.0.0")
MAX_MSG_SIZE = 1 * 1024 * 1024  # 1 MiB is ample for any name record
DEFAULT_PORT = 1707
DEFAULT_STORE_PATH = "./naming_data/records.json"
DEFAULT_REVIEWERS_PATH = "./naming_data/reviewers.json"
DEFAULT_ASSIGNMENTS_PATH = "./naming_data/assignments.json"
DEFAULT_ATTACHMENTS_PATH = "./naming_data/attachments.json"
DEFAULT_KEY_PATH = "./naming_data/peer.key"


async def send_json(stream: INetStream, obj: dict) -> None:
    await send_framed_json(stream, obj, MAX_MSG_SIZE)


async def recv_json(stream: INetStream) -> Optional[dict]:
    return await recv_framed_json(stream, MAX_MSG_SIZE)


# ─── Record store ─────────────────────────────────────────────────────

class NameStore:
    """In-memory records, flushed to disk on every accepted register."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._records: dict[str, tuple[dict, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._records = {
                uri: (entry["record"], entry["signature"])
                for uri, entry in raw.items()
            }
            logger.info("Loaded %d name records from %s", len(self._records), self.path)
        except Exception as e:
            logger.error("Failed to load name records: %s", e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            uri: {"record": record, "signature": signature}
            for uri, (record, signature) in self._records.items()
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def get(self, uri: str) -> Optional[tuple[dict, str]]:
        return self._records.get(uri)

    def list_records(self) -> list[dict]:
        return [record for record, _ in self._records.values()]

    def set(self, uri: str, record: dict, signature: str) -> tuple[bool, str]:
        existing = self._records.get(uri)
        if existing is not None:
            existing_record, _ = existing
            if existing_record.get("public_key") != record.get("public_key"):
                return False, f"'{uri}' already registered to a different public key"
            existing_ts = int(existing_record.get("timestamp", 0))
            new_ts = int(record.get("timestamp", 0))
            if new_ts <= existing_ts:
                return False, (
                    f"timestamp must be strictly greater than existing "
                    f"({new_ts} <= {existing_ts})"
                )
        self._records[uri] = (record, signature)
        self._save()
        return True, ""


# ─── Reviewer store ───────────────────────────────────────────────────

class ReviewerStore:
    """Opt-in reviewer registrations, keyed by the reviewer's public key.

    Structurally identical to NameStore but holds reviewer opt-in records
    signed by each reviewer. Persisted to a separate JSON file so deploys
    that enable/disable reviewers can be reasoned about independently.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._records: dict[str, tuple[dict, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._records = {
                pubkey: (entry["record"], entry["signature"])
                for pubkey, entry in raw.items()
            }
            logger.info("Loaded %d reviewer records from %s", len(self._records), self.path)
        except Exception as e:
            logger.error("Failed to load reviewer records: %s", e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            pubkey: {"record": record, "signature": signature}
            for pubkey, (record, signature) in self._records.items()
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def get(self, pubkey: str) -> Optional[tuple[dict, str]]:
        return self._records.get(pubkey)

    def list_records(self) -> list[tuple[dict, str]]:
        return list(self._records.values())

    def set(self, record: dict, signature: str) -> tuple[bool, str]:
        pubkey = record.get("public_key", "")
        if not pubkey:
            return False, "record missing public_key"
        existing = self._records.get(pubkey)
        if existing is not None:
            existing_record, _ = existing
            existing_ts = int(existing_record.get("timestamp", 0))
            new_ts = int(record.get("timestamp", 0))
            if new_ts <= existing_ts:
                return False, (
                    f"timestamp must be strictly greater than existing "
                    f"({new_ts} <= {existing_ts})"
                )
        self._records[pubkey] = (record, signature)
        self._save()
        return True, ""


# ─── Assignment inbox ─────────────────────────────────────────────────

class AssignmentStore:
    """Per-reviewer inbox of review assignments.

    Each entry is keyed by (reviewer_public_key, content_key) so a second
    assignment by the same publisher for the same content replaces the
    earlier one. Reviewers poll their inbox on their own schedule; the
    store itself does not enforce deadlines — readers and reviewers filter
    on the record's `deadline` field.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        # inbox[reviewer_pubkey][content_key] = (record, signature)
        self._inbox: dict[str, dict[str, tuple[dict, str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for reviewer_pub, by_content in raw.items():
                self._inbox[reviewer_pub] = {
                    content_key: (entry["record"], entry["signature"])
                    for content_key, entry in by_content.items()
                }
            logger.info(
                "Loaded %d reviewer inboxes from %s",
                len(self._inbox),
                self.path,
            )
        except Exception as e:
            logger.error("Failed to load assignments: %s", e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            reviewer_pub: {
                content_key: {"record": record, "signature": signature}
                for content_key, (record, signature) in by_content.items()
            }
            for reviewer_pub, by_content in self._inbox.items()
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def add(self, record: dict, signature: str) -> tuple[bool, str]:
        """Store an assignment in each selected reviewer's inbox."""
        content_key = record.get("content_key", "")
        reviewer_pubkeys = record.get("reviewer_public_keys") or []
        if not content_key or not reviewer_pubkeys:
            return False, "assignment missing content_key or reviewer_public_keys"

        new_ts = int(record.get("timestamp", 0))
        for reviewer_pub in reviewer_pubkeys:
            slot = self._inbox.setdefault(reviewer_pub, {})
            existing = slot.get(content_key)
            if existing is not None and int(existing[0].get("timestamp", 0)) >= new_ts:
                continue  # keep the newer (or equally-fresh) one
            slot[content_key] = (record, signature)
        self._save()
        return True, ""

    def list_for(self, reviewer_pubkey: str) -> list[tuple[dict, str]]:
        return list(self._inbox.get(reviewer_pubkey, {}).values())


# ─── Attachment store ────────────────────────────────────────────────

class AttachmentStore:
    """Review records attached to a content_key after publication.

    Dedup rule: one record per (content_key, reviewer_public_key). A newer
    record replaces the older one (reviewers can amend their verdict).
    """

    def __init__(self, path: str):
        self.path = Path(path)
        # attachments[content_key][reviewer_pubkey] = (record, signature)
        self._attachments: dict[str, dict[str, tuple[dict, str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for content_key, by_reviewer in raw.items():
                self._attachments[content_key] = {
                    reviewer_pub: (entry["record"], entry["signature"])
                    for reviewer_pub, entry in by_reviewer.items()
                }
            logger.info(
                "Loaded attachments for %d content keys from %s",
                len(self._attachments),
                self.path,
            )
        except Exception as e:
            logger.error("Failed to load attachments: %s", e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            content_key: {
                reviewer_pub: {"record": record, "signature": signature}
                for reviewer_pub, (record, signature) in by_reviewer.items()
            }
            for content_key, by_reviewer in self._attachments.items()
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def attach(self, record: dict, signature: str) -> tuple[bool, str]:
        content_key = record.get("content_key", "")
        reviewer_pub = record.get("reviewer_public_key", "")
        if not content_key or not reviewer_pub:
            return False, "review record missing content_key or reviewer_public_key"
        slot = self._attachments.setdefault(content_key, {})
        existing = slot.get(reviewer_pub)
        new_ts = int(record.get("timestamp", 0))
        if existing is not None and int(existing[0].get("timestamp", 0)) >= new_ts:
            return False, "existing attachment is at least as recent"
        slot[reviewer_pub] = (record, signature)
        self._save()
        return True, ""

    def get_for(self, content_key: str) -> list[tuple[dict, str]]:
        return list(self._attachments.get(content_key, {}).values())


# ─── Server ───────────────────────────────────────────────────────────

class NamingServer:
    def __init__(
        self,
        host: IHost,
        store: NameStore,
        reviewer_store: Optional[ReviewerStore] = None,
        assignment_store: Optional[AssignmentStore] = None,
        attachment_store: Optional[AttachmentStore] = None,
    ):
        self.host = host
        self.store = store
        self.reviewer_store = reviewer_store
        self.assignment_store = assignment_store
        self.attachment_store = attachment_store

    def attach(self) -> None:
        self.host.set_stream_handler(NAMING_PROTOCOL, self._handle_stream)

    async def _handle_stream(self, stream: INetStream) -> None:
        try:
            msg = await recv_json(stream)
            if msg is None:
                await _send_error(stream, "empty or malformed request")
                return

            msg_type = msg.get("type")
            if msg_type == "register":
                response = self._handle_register(msg)
            elif msg_type == "resolve":
                response = self._handle_resolve(msg)
            elif msg_type == "list":
                response = self._handle_list()
            elif msg_type == "register_reviewer":
                response = self._handle_register_reviewer(msg)
            elif msg_type == "list_reviewers":
                response = self._handle_list_reviewers()
            elif msg_type == "post_assignment":
                response = self._handle_post_assignment(msg)
            elif msg_type == "list_assignments":
                response = self._handle_list_assignments(msg)
            elif msg_type == "attach_review":
                response = self._handle_attach_review(msg)
            elif msg_type == "get_attachments":
                response = self._handle_get_attachments(msg)
            else:
                response = {"type": "error", "msg": f"unknown type: {msg_type}"}

            await send_json(stream, response)
        except Exception as e:
            logger.exception("stream handler error")
            await _send_error(stream, f"internal error: {e}")
        finally:
            await stream.close()

    def _handle_register(self, msg: dict) -> dict:
        record = msg.get("record") or {}
        signature = msg.get("signature", "")
        if not record or not signature:
            return {"type": "error", "msg": "record and signature required"}

        uri = record.get("uri", "")
        try:
            validate_uri(uri)
        except ValueError as e:
            return {"type": "error", "msg": str(e)}

        is_valid, err = verify_name_record(record, signature)
        if not is_valid:
            return {"type": "error", "msg": f"invalid signature: {err}"}

        ok, err = self.store.set(uri, record, signature)
        if not ok:
            return {"type": "error", "msg": err}

        logger.info(
            "REGISTER %s author=%s ref=%s ts=%s",
            uri,
            record.get("author"),
            record.get("manifest_ref", "")[:12],
            record.get("timestamp"),
        )
        return {"type": "ok", "uri": uri}

    def _handle_resolve(self, msg: dict) -> dict:
        uri = msg.get("uri", "")
        try:
            validate_uri(uri)
        except ValueError as e:
            return {"type": "error", "msg": str(e)}

        entry = self.store.get(uri)
        if entry is None:
            return {"type": "error", "msg": f"unknown uri: {uri}"}
        record, signature = entry
        logger.info("RESOLVE %s", uri)
        return {"type": "record", "record": record, "signature": signature}

    def _handle_list(self) -> dict:
        return {"type": "names", "records": self.store.list_records()}

    def _handle_register_reviewer(self, msg: dict) -> dict:
        if self.reviewer_store is None:
            return {"type": "error", "msg": "reviewer registry disabled on this server"}

        record = msg.get("record") or {}
        signature = msg.get("signature", "")
        if not record or not signature:
            return {"type": "error", "msg": "record and signature required"}

        is_valid, err = verify_reviewer_opt_in(record, signature)
        if not is_valid:
            return {"type": "error", "msg": f"invalid signature: {err}"}

        ok, err = self.reviewer_store.set(record, signature)
        if not ok:
            return {"type": "error", "msg": err}

        logger.info(
            "REGISTER_REVIEWER pubkey=%s cats=%s ts=%s",
            record.get("public_key", "")[:12],
            record.get("categories"),
            record.get("timestamp"),
        )
        return {"type": "ok", "public_key": record.get("public_key")}

    def _handle_list_reviewers(self) -> dict:
        if self.reviewer_store is None:
            return {"type": "reviewers", "records": []}
        # Return record + signature pairs so clients can re-verify.
        entries = [
            {"record": record, "signature": signature}
            for record, signature in self.reviewer_store.list_records()
        ]
        return {"type": "reviewers", "records": entries}

    def _handle_post_assignment(self, msg: dict) -> dict:
        if self.assignment_store is None:
            return {"type": "error", "msg": "assignment inbox disabled on this server"}

        record = msg.get("record") or {}
        signature = msg.get("signature", "")
        if not record or not signature:
            return {"type": "error", "msg": "record and signature required"}

        is_valid, err = verify_review_assignment(record, signature)
        if not is_valid:
            return {"type": "error", "msg": f"invalid signature: {err}"}

        ok, err = self.assignment_store.add(record, signature)
        if not ok:
            return {"type": "error", "msg": err}

        logger.info(
            "POST_ASSIGNMENT content=%s reviewers=%d deadline=%s",
            record.get("content_key", "")[:24],
            len(record.get("reviewer_public_keys") or []),
            record.get("deadline"),
        )
        return {"type": "ok", "content_key": record.get("content_key")}

    def _handle_list_assignments(self, msg: dict) -> dict:
        if self.assignment_store is None:
            return {"type": "assignments", "records": []}
        reviewer_pub = msg.get("reviewer_public_key", "")
        if not reviewer_pub:
            return {"type": "error", "msg": "reviewer_public_key required"}
        entries = [
            {"record": record, "signature": signature}
            for record, signature in self.assignment_store.list_for(reviewer_pub)
        ]
        return {"type": "assignments", "records": entries}

    def _handle_attach_review(self, msg: dict) -> dict:
        if self.attachment_store is None:
            return {"type": "error", "msg": "attachment store disabled on this server"}

        record = msg.get("record") or {}
        signature = msg.get("signature", "")
        if not record or not signature:
            return {"type": "error", "msg": "record and signature required"}

        is_valid, err = verify_review_record(record, signature)
        if not is_valid:
            return {"type": "error", "msg": f"invalid signature: {err}"}

        ok, err = self.attachment_store.attach(record, signature)
        if not ok:
            return {"type": "error", "msg": err}

        logger.info(
            "ATTACH_REVIEW content=%s reviewer=%s verdict=%s",
            record.get("content_key", "")[:24],
            record.get("reviewer_public_key", "")[:12],
            record.get("verdict"),
        )
        return {"type": "ok", "content_key": record.get("content_key")}

    def _handle_get_attachments(self, msg: dict) -> dict:
        if self.attachment_store is None:
            return {"type": "attachments", "records": []}
        content_key = msg.get("content_key", "")
        if not content_key:
            return {"type": "error", "msg": "content_key required"}
        entries = [
            {"record": record, "signature": signature}
            for record, signature in self.attachment_store.get_for(content_key)
        ]
        return {"type": "attachments", "records": entries}


async def _send_error(stream: INetStream, message: str) -> None:
    try:
        await send_json(stream, {"type": "error", "msg": message})
    except Exception:
        pass


# ─── Client helpers ───────────────────────────────────────────────────

async def _rpc(host: IHost, server_info: PeerInfo, request: dict) -> dict:
    await host.connect(server_info)
    stream = await host.new_stream(server_info.peer_id, [NAMING_PROTOCOL])
    try:
        await send_json(stream, request)
        response = await recv_json(stream)
    finally:
        await stream.close()
    if response is None:
        raise RuntimeError("no response from naming server")
    return response


async def client_register(
    host: IHost, server_info: PeerInfo, record: dict, signature: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "register", "record": record, "signature": signature},
    )


async def client_resolve(host: IHost, server_info: PeerInfo, uri: str) -> dict:
    return await _rpc(host, server_info, {"type": "resolve", "uri": uri})


async def client_list(host: IHost, server_info: PeerInfo) -> dict:
    return await _rpc(host, server_info, {"type": "list"})


async def client_register_reviewer(
    host: IHost, server_info: PeerInfo, record: dict, signature: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "register_reviewer", "record": record, "signature": signature},
    )


async def client_list_reviewers(host: IHost, server_info: PeerInfo) -> dict:
    return await _rpc(host, server_info, {"type": "list_reviewers"})


async def client_post_assignment(
    host: IHost, server_info: PeerInfo, record: dict, signature: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "post_assignment", "record": record, "signature": signature},
    )


async def client_list_assignments(
    host: IHost, server_info: PeerInfo, reviewer_public_key: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "list_assignments", "reviewer_public_key": reviewer_public_key},
    )


async def client_attach_review(
    host: IHost, server_info: PeerInfo, record: dict, signature: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "attach_review", "record": record, "signature": signature},
    )


async def client_get_attachments(
    host: IHost, server_info: PeerInfo, content_key: str
) -> dict:
    return await _rpc(
        host,
        server_info,
        {"type": "get_attachments", "content_key": content_key},
    )


# ─── Peer key persistence ─────────────────────────────────────────────

def load_or_create_peer_seed(path: str) -> bytes:
    """Load the 32-byte libp2p key seed, or generate and persist a fresh one."""
    p = Path(path)
    if p.exists():
        data = p.read_bytes()
        if len(data) != 32:
            raise ValueError(f"invalid peer key at {p}: expected 32 bytes, got {len(data)}")
        return data

    p.parent.mkdir(parents=True, exist_ok=True)
    seed = os.urandom(32)
    p.write_bytes(seed)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Generated fresh peer key at %s", p)
    return seed


# ─── CLI ──────────────────────────────────────────────────────────────

async def serve(
    port: int,
    store_path: str,
    reviewers_path: str,
    assignments_path: str,
    attachments_path: str,
    key_path: str,
) -> None:
    seed = load_or_create_peer_seed(key_path)
    host = new_host(key_pair=create_new_key_pair(seed))

    listen = multiaddr.Multiaddr(f"/ip4/0.0.0.0/tcp/{port}")
    store = NameStore(store_path)
    reviewer_store = ReviewerStore(reviewers_path)
    assignment_store = AssignmentStore(assignments_path)
    attachment_store = AttachmentStore(attachments_path)
    server = NamingServer(
        host, store, reviewer_store, assignment_store, attachment_store
    )

    async with host.run(listen_addrs=[listen]), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        server.attach()

        peer_id = host.get_id().to_string()
        print("━" * 60)
        print("  MDP2P Naming — /mdp2p/naming/1.0.0")
        print(f"  Port       : {port}")
        print(f"  PeerID     : {peer_id}")
        print(f"  Store      : {store_path} ({len(store.list_records())} records)")
        print(
            f"  Reviewers  : {reviewers_path} "
            f"({len(reviewer_store.list_records())} registered)"
        )
        for addr in host.get_addrs():
            print(f"  Listen     : {addr}")
        print(f"  Bootstrap  : /ip4/<host>/tcp/{port}/p2p/{peer_id}")
        print("━" * 60)

        await trio.sleep_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="MDP2P naming server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store", default=DEFAULT_STORE_PATH)
    parser.add_argument("--reviewers", default=DEFAULT_REVIEWERS_PATH)
    parser.add_argument("--assignments", default=DEFAULT_ASSIGNMENTS_PATH)
    parser.add_argument("--attachments", default=DEFAULT_ATTACHMENTS_PATH)
    parser.add_argument("--key", default=DEFAULT_KEY_PATH)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [NAMING] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        trio.run(
            serve,
            args.port,
            args.store,
            args.reviewers,
            args.assignments,
            args.attachments,
            args.key,
        )
    except KeyboardInterrupt:
        print("\n[NAMING] stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
