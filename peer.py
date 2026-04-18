"""
MDP2P Peer — publishes, fetches, seeds Markdown bundles over libp2p.

Architecture:
  - Identity: libp2p PeerID (transport) distinct from the author's ed25519
    pubkey (content). Author identity travels in the signed manifest; the
    PeerID is stable per machine via a seed file.
  - Discovery: the naming service (see naming.py) resolves uri → (author_pubkey,
    manifest_ref); the list of seeders will come from the DHT in Phase 3.
    In Phase 2 callers supply seeder multiaddrs directly.
  - Transport: custom protocol /mdp2p/bundle/1.0.0 over libp2p streams.
    Messages are length-prefixed JSON. A bundle transfer is a single message
    whose size is capped by MAX_BUNDLE_MSG_SIZE.

Public API (Peer class):
  - publish(uri, author, site_dir, priv_key_path) → creates the bundle, signs,
    registers on the naming service, stores site locally for seeding.
  - fetch_site(uri, seeder_addrs) → resolve, verify author signature, TOFU,
    download, persist to data_dir.
  - check_for_update(uri, seeder_addr) → compare timestamps.

Lifecycle is delegated to the `run_peer` async context manager so the libp2p
host shutdown happens cleanly on exit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import multiaddr
import trio
from libp2p import new_host
from libp2p.abc import IHost
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.peerinfo import PeerInfo, info_from_p2p_addr

from bundle import (
    b64_to_public_key,
    build_name_record,
    bundle_to_dict,
    compute_manifest_ref,
    create_manifest,
    dict_to_bundle,
    is_manifest_expired,
    load_bundle,
    load_private_key,
    public_key_to_b64,
    save_bundle,
    sign_manifest,
    sign_name_record,
    validate_path,
    verify_files,
    verify_manifest,
    verify_name_record,
)
from naming import (
    client_register as naming_register,
    client_resolve as naming_resolve,
)
from naming import load_or_create_peer_seed
from pinstore import PinStatus, check_pin, pin_key, update_pin_last_seen
from protocol import validate_uri
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.peer")

BUNDLE_PROTOCOL = TProtocol("/mdp2p/bundle/1.0.0")
MAX_BUNDLE_MSG_SIZE = 100 * 1024 * 1024  # 100 MiB — bundle cap is 50 MiB in bundle.py
DEFAULT_DATA_DIR = "./peer_data"
DEFAULT_PINSTORE = str(Path.home() / ".mdp2p" / "known_keys.json")


class Peer:
    """Stateful wrapper around a libp2p host that serves and fetches bundles."""

    def __init__(
        self,
        host: IHost,
        data_dir: str = DEFAULT_DATA_DIR,
        naming_info: Optional[PeerInfo] = None,
        pinstore_path: str = DEFAULT_PINSTORE,
    ):
        self.host = host
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.naming_info = naming_info
        self.pinstore_path = pinstore_path
        self.sites: dict[str, str] = {}

    def attach(self) -> None:
        self.host.set_stream_handler(BUNDLE_PROTOCOL, self._handle_bundle_request)
        self._rediscover_local_sites()

    def _rediscover_local_sites(self) -> None:
        """Populate self.sites by scanning data_dir for previously-seeded bundles."""
        for site_dir in self.data_dir.iterdir() if self.data_dir.exists() else []:
            if not site_dir.is_dir():
                continue
            try:
                manifest, _ = load_bundle(str(site_dir))
                uri = manifest.get("uri")
                if uri:
                    self.sites[uri] = str(site_dir)
            except Exception:
                continue

    @property
    def addrs(self) -> list[str]:
        peer_id = self.host.get_id().to_string()
        return [f"{addr}/p2p/{peer_id}" for addr in self.host.get_addrs()]

    # ─── Publishing (author) ──────────────────────────────────────────

    async def publish(
        self,
        uri: str,
        author: str,
        site_dir: str,
        private_key_path: str,
    ) -> tuple[dict, str]:
        """Create a signed bundle, register on naming, seed locally.

        Returns (manifest, signature_b64).
        """
        validate_uri(uri)
        if self.naming_info is None:
            raise ValueError("cannot publish without a naming server configured")

        site_dir_resolved = str(Path(site_dir).resolve())
        private_key = load_private_key(private_key_path)
        pub_b64 = public_key_to_b64(private_key.public_key())

        version = 1
        manifest_file = Path(site_dir_resolved) / "manifest.json"
        if manifest_file.exists():
            try:
                old, _ = load_bundle(site_dir_resolved)
                version = int(old.get("version", 0)) + 1
            except Exception:
                pass

        manifest = create_manifest(
            site_dir_resolved, uri=uri, author=author, version=version
        )
        manifest, signature = sign_manifest(manifest, private_key)
        save_bundle(site_dir_resolved, manifest, signature)
        logger.info(
            "bundle signed: %d files, %d bytes, version %d",
            manifest["file_count"],
            manifest["total_size"],
            manifest["version"],
        )

        manifest_ref = compute_manifest_ref(manifest)
        record = build_name_record(uri, author, pub_b64, manifest_ref)
        name_sig = sign_name_record(record, private_key)
        resp = await naming_register(self.host, self.naming_info, record, name_sig)
        if resp.get("type") != "ok":
            raise RuntimeError(f"naming register failed: {resp.get('msg')}")
        logger.info("naming registered: %s → %s", uri, manifest_ref[:12])

        self.sites[uri] = site_dir_resolved
        return manifest, signature

    # ─── Downloading (client) ────────────────────────────────────────

    async def fetch_site(
        self,
        uri: str,
        seeder_addrs: list[str],
    ) -> bool:
        """Resolve via naming, download from a seeder, verify, and seed.

        `seeder_addrs` must be a list of full libp2p multiaddrs. In Phase 3
        callers can leave this empty and let DHT lookup fill it in.
        """
        validate_uri(uri)
        if self.naming_info is None:
            raise ValueError("cannot fetch without a naming server configured")
        if not seeder_addrs:
            raise ValueError("no seeders provided (DHT discovery arrives in Phase 3)")

        resp = await naming_resolve(self.host, self.naming_info, uri)
        if resp.get("type") != "record":
            logger.error("naming resolve failed for %s: %s", uri, resp.get("msg"))
            return False

        record = resp["record"]
        record_sig = resp["signature"]
        ok, err = verify_name_record(record, record_sig)
        if not ok:
            logger.error("naming record signature invalid for %s: %s", uri, err)
            return False

        author_pub_b64 = record["public_key"]
        expected_ref = record["manifest_ref"]
        record_author = record.get("author", "unknown")

        pinstore_data = None
        try:
            from pinstore import load_pinstore  # local import to avoid circular
            pinstore_data = load_pinstore(self.pinstore_path)
        except Exception:
            pinstore_data = {}
        pin_status = check_pin(pinstore_data, uri, author_pub_b64)
        if pin_status == PinStatus.MISMATCH:
            logger.error(
                "ALERT: public key changed for '%s' — possible MITM; aborting", uri
            )
            return False

        bundle_data = await self._try_download_from_seeders(uri, seeder_addrs)
        if bundle_data is None:
            logger.error("all seeders failed for %s", uri)
            return False

        manifest = bundle_data["manifest"]
        signature_b64 = bundle_data["signature"]
        trusted_key = b64_to_public_key(author_pub_b64)

        if not verify_manifest(manifest, signature_b64, trusted_key):
            logger.error("manifest signature invalid for %s", uri)
            return False

        actual_ref = compute_manifest_ref(manifest)
        if actual_ref != expected_ref:
            logger.error(
                "manifest ref mismatch for %s: naming says %s, got %s",
                uri,
                expected_ref,
                actual_ref,
            )
            return False

        if is_manifest_expired(manifest):
            logger.error("manifest expired for %s", uri)
            return False

        site_dir = str(self.data_dir / uri)
        dict_to_bundle(bundle_data, site_dir)

        errors = verify_files(manifest, site_dir)
        if errors:
            logger.error("file integrity errors for %s: %s", uri, errors)
            return False

        if pin_status == PinStatus.UNKNOWN:
            pin_key(uri, author_pub_b64, record_author, self.pinstore_path)
            logger.info("key pinned for '%s' (first visit)", uri)
        else:
            update_pin_last_seen(uri, self.pinstore_path)

        self.sites[uri] = site_dir
        logger.info(
            "fetched %s (%d files, %d bytes)",
            uri,
            manifest["file_count"],
            manifest["total_size"],
        )
        return True

    async def _try_download_from_seeders(
        self, uri: str, seeder_addrs: list[str]
    ) -> Optional[dict]:
        """Try each seeder in parallel; return the first successful bundle."""
        results: list[Optional[dict]] = [None] * len(seeder_addrs)

        async def try_one(idx: int, addr_str: str) -> None:
            try:
                info = info_from_p2p_addr(multiaddr.Multiaddr(addr_str))
                await self.host.connect(info)
                stream = await self.host.new_stream(info.peer_id, [BUNDLE_PROTOCOL])
                try:
                    await send_framed_json(
                        stream, {"type": "get_bundle", "uri": uri}, MAX_BUNDLE_MSG_SIZE
                    )
                    response = await recv_framed_json(stream, MAX_BUNDLE_MSG_SIZE)
                finally:
                    await stream.close()
                if response and response.get("type") == "bundle":
                    results[idx] = response
            except Exception as e:
                logger.warning("fetch from %s failed: %s", addr_str, e)

        async with trio.open_nursery() as nursery:
            for idx, addr in enumerate(seeder_addrs):
                nursery.start_soon(try_one, idx, addr)

        for r in results:
            if r is not None:
                return r
        return None

    # ─── Update check ────────────────────────────────────────────────

    async def check_for_update(self, uri: str, seeder_addr: str) -> bool:
        """Return True if a seeder has a newer manifest than our local copy."""
        if uri not in self.sites:
            return False
        local_manifest, _ = load_bundle(self.sites[uri])
        local_ts = int(local_manifest.get("timestamp", 0))

        try:
            info = info_from_p2p_addr(multiaddr.Multiaddr(seeder_addr))
            await self.host.connect(info)
            stream = await self.host.new_stream(info.peer_id, [BUNDLE_PROTOCOL])
            try:
                await send_framed_json(
                    stream, {"type": "get_manifest", "uri": uri}, MAX_BUNDLE_MSG_SIZE
                )
                response = await recv_framed_json(stream, MAX_BUNDLE_MSG_SIZE)
            finally:
                await stream.close()
            if response and response.get("type") == "manifest":
                remote_ts = int(response["manifest"].get("timestamp", 0))
                return remote_ts > local_ts
        except Exception as e:
            logger.warning("check_for_update failed: %s", e)
        return False

    # ─── Seeder handler ──────────────────────────────────────────────

    async def _handle_bundle_request(self, stream: INetStream) -> None:
        try:
            msg = await recv_framed_json(stream, MAX_BUNDLE_MSG_SIZE)
            if msg is None:
                return
            msg_type = msg.get("type", "")
            uri = msg.get("uri", "")

            if uri not in self.sites:
                await _send_error(stream, f"site '{uri}' not found")
                return

            if msg_type == "get_bundle":
                data = bundle_to_dict(self.sites[uri])
                data["type"] = "bundle"
                await send_framed_json(stream, data, MAX_BUNDLE_MSG_SIZE)
            elif msg_type == "get_manifest":
                manifest, signature = load_bundle(self.sites[uri])
                await send_framed_json(
                    stream,
                    {
                        "type": "manifest",
                        "manifest": manifest,
                        "signature": signature,
                    },
                    MAX_BUNDLE_MSG_SIZE,
                )
            else:
                await _send_error(stream, f"unknown type: {msg_type}")
        except Exception as e:
            logger.exception("bundle handler error")
            await _send_error(stream, f"internal error: {e}")
        finally:
            await stream.close()

    # ─── Rendering (unchanged from the previous version) ─────────────

    def render_site(self, uri: str) -> str:
        if uri not in self.sites:
            return f"Site '{uri}' not found locally."
        site_dir = Path(self.sites[uri])
        manifest, _ = load_bundle(str(site_dir))
        output = [
            f"\n{'━' * 60}",
            f"  md://{uri}",
            f"  {manifest['file_count']} pages — {manifest['total_size']} bytes",
            f"  version {manifest['version']}",
            f"{'━' * 60}\n",
        ]
        for entry in manifest["files"]:
            fpath = validate_path(site_dir, entry["path"])
            content = fpath.read_text(encoding="utf-8")
            output.append(
                f"┌─ {entry['path']} ─{'─' * max(0, 40 - len(entry['path']))}┐"
            )
            output.append(content.strip())
            output.append(f"└{'─' * 50}┘\n")
        return "\n".join(output)


async def _send_error(stream: INetStream, message: str) -> None:
    try:
        await send_framed_json(
            stream, {"type": "error", "msg": message}, MAX_BUNDLE_MSG_SIZE
        )
    except Exception:
        pass


# ─── Lifecycle helpers ────────────────────────────────────────────────

@asynccontextmanager
async def run_peer(
    data_dir: str = DEFAULT_DATA_DIR,
    port: int = 0,
    peer_key_path: Optional[str] = None,
    naming_info: Optional[PeerInfo] = None,
    pinstore_path: str = DEFAULT_PINSTORE,
) -> AsyncIterator[Peer]:
    """Run a Peer with a libp2p host, yielding the Peer to the caller.

    The host listens on `port` (0 = random), loads/creates a persistent peer
    key at `peer_key_path` (defaults to <data_dir>/peer.key), and attaches
    the bundle protocol handler.
    """
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    key_path = peer_key_path or str(data_path / "peer.key")

    seed = load_or_create_peer_seed(key_path)
    host = new_host(key_pair=create_new_key_pair(seed))
    listen = [multiaddr.Multiaddr(f"/ip4/0.0.0.0/tcp/{port}")]

    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        peer = Peer(
            host,
            data_dir=data_dir,
            naming_info=naming_info,
            pinstore_path=pinstore_path,
        )
        peer.attach()
        try:
            yield peer
        finally:
            nursery.cancel_scope.cancel()
