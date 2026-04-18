"""
MDP2P Naming — minimal libp2p service that maps md:// URIs to author-signed records.

Each record binds:
  uri → {author, public_key, manifest_ref, timestamp}

The record is signed with the author's ed25519 private key. The naming server
never fabricates records — it only stores and serves records it could verify,
so it is non-trusted: clients re-verify signatures upon resolve.

Wire protocol: /mdp2p/naming/1.0.0
  register : {"type": "register", "record": {...}, "signature": "<b64>"}
  resolve  : {"type": "resolve",  "uri": "<uri>"}
  list     : {"type": "list"}

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
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.naming")

NAMING_PROTOCOL = TProtocol("/mdp2p/naming/1.0.0")
MAX_MSG_SIZE = 1 * 1024 * 1024  # 1 MiB is ample for any name record
DEFAULT_PORT = 1707
DEFAULT_STORE_PATH = "./naming_data/records.json"
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


# ─── Server ───────────────────────────────────────────────────────────

class NamingServer:
    def __init__(self, host: IHost, store: NameStore):
        self.host = host
        self.store = store

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

async def serve(port: int, store_path: str, key_path: str) -> None:
    seed = load_or_create_peer_seed(key_path)
    host = new_host(key_pair=create_new_key_pair(seed))

    listen = multiaddr.Multiaddr(f"/ip4/0.0.0.0/tcp/{port}")
    store = NameStore(store_path)
    server = NamingServer(host, store)

    async with host.run(listen_addrs=[listen]), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        server.attach()

        peer_id = host.get_id().to_string()
        print("━" * 60)
        print("  MDP2P Naming — /mdp2p/naming/1.0.0")
        print(f"  Port       : {port}")
        print(f"  PeerID     : {peer_id}")
        print(f"  Store      : {store_path} ({len(store.list_records())} records)")
        for addr in host.get_addrs():
            print(f"  Listen     : {addr}")
        print(f"  Bootstrap  : /ip4/<host>/tcp/{port}/p2p/{peer_id}")
        print("━" * 60)

        await trio.sleep_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="MDP2P naming server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--store", default=DEFAULT_STORE_PATH)
    parser.add_argument("--key", default=DEFAULT_KEY_PATH)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [NAMING] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        trio.run(serve, args.port, args.store, args.key)
    except KeyboardInterrupt:
        print("\n[NAMING] stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
