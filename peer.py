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
from contextlib import AsyncExitStack, asynccontextmanager
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
    compute_content_key,
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
    validate_uri,
    verify_files,
    verify_manifest,
    verify_name_record,
)
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.relay.circuit_v2.config import RelayConfig, RelayRole
from libp2p.relay.circuit_v2.dcutr import DCUtRProtocol
from libp2p.relay.circuit_v2.discovery import RelayDiscovery
from libp2p.relay.circuit_v2.protocol import (
    PROTOCOL_ID as HOP_PROTOCOL_ID,
    STOP_PROTOCOL_ID,
    CircuitV2Protocol,
)
from libp2p.relay.circuit_v2.resources import RelayLimits
from libp2p.relay.circuit_v2.transport import CircuitV2Transport
from libp2p.tools.async_service import background_trio_service
from naming import (
    client_register as naming_register,
    client_resolve as naming_resolve,
)
from naming import load_or_create_peer_seed
from pinstore import PinStatus, check_pin, pin_key, update_pin_last_seen
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.peer")

BUNDLE_PROTOCOL = TProtocol("/mdp2p/bundle/1.0.0")
MAX_BUNDLE_MSG_SIZE = 100 * 1024 * 1024  # 100 MiB — bundle cap is 50 MiB in bundle.py
DEFAULT_DATA_DIR = "./peer_data"
DEFAULT_PINSTORE = str(Path.home() / ".mdp2p" / "known_keys.json")


def detect_local_ip() -> str:
    """Best-effort detection of the primary LAN IPv4 of this machine.

    Uses the UDP-connect trick: asks the kernel which interface it would
    use to reach a public address, without actually sending any packet.
    Falls back to ``127.0.0.1`` when no routable interface is available
    (offline machine, container with no egress, etc.).
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _default_relay_limits() -> RelayLimits:
    """Conservative limits for a public peer-zero running as a relay.

    100 MiB/reservation and 10 concurrent circuits caps bandwidth so the
    relay cannot be trivially abused as free bandwidth.
    """
    return RelayLimits(
        duration=3600,
        data=100 * 1024 * 1024,
        max_circuit_conns=10,
        max_reservations=20,
    )


class Peer:
    """Stateful wrapper around a libp2p host that serves and fetches bundles."""

    def __init__(
        self,
        host: IHost,
        data_dir: str = DEFAULT_DATA_DIR,
        naming_info: Optional[PeerInfo] = None,
        pinstore_path: str = DEFAULT_PINSTORE,
        dht: Optional[KadDHT] = None,
        relay_transport: Optional[CircuitV2Transport] = None,
    ):
        self.host = host
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.naming_info = naming_info
        self.pinstore_path = pinstore_path
        self.dht = dht
        self.relay_transport = relay_transport
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
        peer_id_component = f"/p2p/{self.host.get_id().to_string()}"
        result: list[str] = []
        for addr in self.host.get_addrs():
            s = str(addr)
            if peer_id_component in s:
                result.append(s)
            else:
                result.append(s + peer_id_component)
        return result

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
        await self.announce(uri)
        return manifest, signature

    # ─── DHT announce ────────────────────────────────────────────────

    async def announce(self, uri: str) -> bool:
        """Advertise this peer as a provider for `uri` in the DHT.

        Requires `dht` to be set and at least one peer in the routing table.
        Returns True on successful advertisement.
        """
        if self.dht is None:
            logger.debug("announce skipped: no DHT configured")
            return False
        if uri not in self.sites:
            logger.warning("announce called for unknown uri %s", uri)
            return False
        manifest, _ = load_bundle(self.sites[uri])
        author_pub_b64 = manifest.get("public_key", "")
        if not author_pub_b64:
            logger.warning("manifest has no public_key for uri %s", uri)
            return False
        key = compute_content_key(uri, author_pub_b64)
        try:
            ok = await self.dht.provide(key)
            logger.info("dht.provide(%s): %s", uri, "ok" if ok else "failed")
            return ok
        except Exception as e:
            logger.warning("dht.provide for %s raised: %s", uri, e)
            return False

    async def find_providers(self, uri: str, author_pub_b64: str) -> list[str]:
        """Return multiaddr strings of peers advertising `uri` in the DHT."""
        if self.dht is None:
            return []
        key = compute_content_key(uri, author_pub_b64)
        try:
            providers = await self.dht.find_providers(key)
        except Exception as e:
            logger.warning("dht.find_providers for %s raised: %s", uri, e)
            return []
        self_id = self.host.get_id()
        addrs: list[str] = []
        for info in providers:
            if info.peer_id == self_id:
                continue
            for addr in info.addrs:
                addrs.append(f"{addr}/p2p/{info.peer_id.to_string()}")
        return addrs

    # ─── Downloading (client) ────────────────────────────────────────

    async def fetch_site(
        self,
        uri: str,
        seeder_addrs: Optional[list[str]] = None,
        announce_after: bool = True,
    ) -> bool:
        """Resolve via naming, download from a seeder, verify, and (optionally) seed.

        - `seeder_addrs` : if None or empty, the DHT is queried for providers.
          Passing an explicit list bypasses discovery.
        - `announce_after` : default True, preserves the "every visitor becomes
          a seeder" mdp2p principle. Pass False for short-lived visitors so
          they do not leave ghost provider records in the DHT on exit.
        """
        validate_uri(uri)
        if self.naming_info is None:
            raise ValueError("cannot fetch without a naming server configured")

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

        if not seeder_addrs:
            seeder_addrs = await self.find_providers(uri, author_pub_b64)
            if not seeder_addrs:
                logger.error(
                    "no providers found for %s (DHT returned empty)", uri
                )
                return False
            logger.info("DHT lookup for %s: %d provider(s)", uri, len(seeder_addrs))

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
        # Join the swarm: announce ourselves as a provider (unless the
        # caller opted out — useful for one-shot visitors that would
        # otherwise leave stale records in the DHT on exit).
        if announce_after:
            await self.announce(uri)
        return True

    async def _try_download_from_seeders(
        self, uri: str, seeder_addrs: list[str]
    ) -> Optional[dict]:
        """Try each seeder in parallel; return the first successful bundle."""
        results: list[Optional[dict]] = [None] * len(seeder_addrs)

        async def try_one(idx: int, addr_str: str) -> None:
            try:
                maddr = multiaddr.Multiaddr(addr_str)
                info = info_from_p2p_addr(maddr)
                if "/p2p-circuit/" in addr_str and self.relay_transport is not None:
                    # CircuitV2Transport needs the destination peer in the
                    # peerstore to complete the dial, even though the circuit
                    # address already carries the peer_id.
                    self.host.get_peerstore().add_addrs(
                        info.peer_id, [maddr], 3600
                    )
                    await self.relay_transport.dial(maddr)
                else:
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
    listen_host: Optional[str] = None,
    peer_key_path: Optional[str] = None,
    naming_info: Optional[PeerInfo] = None,
    pinstore_path: str = DEFAULT_PINSTORE,
    bootstrap_multiaddrs: Optional[list[str]] = None,
    enable_dht: bool = True,
    relay_mode: str = "none",
    relay_multiaddrs: Optional[list[str]] = None,
) -> AsyncIterator[Peer]:
    """Run a Peer with a libp2p host, DHT, and optional Circuit Relay v2 stack.

    - `bootstrap_multiaddrs` : peers dialed at startup; they seed the DHT
      routing table so provide/find_providers have propagation targets.
    - `enable_dht=False` disables the DHT entirely (useful for tests that
      wire peers manually without discovery).
    - `relay_mode` : one of
        - "none" (default): no Circuit Relay services loaded
        - "client": STOP + CLIENT roles. The peer can dial and accept
          connections through a relay, and attempts DCUtR upgrades.
        - "hop": full relay (HOP + STOP + CLIENT). For the peer-zero VPS.
    - `relay_multiaddrs` : list of relay multiaddrs to dial at startup
      (client mode only); RelayDiscovery will auto-reserve slots.
    - `listen_host` : bind address. Defaults to the auto-detected LAN IP
      (see detect_local_ip) to avoid polluting the DHT with ``0.0.0.0``
      peer records. Public hosts (the peer-zero VPS) should explicitly
      pass ``"0.0.0.0"`` to accept connections from anywhere.
    """
    if relay_mode not in ("none", "client", "hop"):
        raise ValueError(f"relay_mode must be 'none', 'client' or 'hop', got {relay_mode!r}")

    if listen_host is None:
        listen_host = detect_local_ip()

    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    key_path = peer_key_path or str(data_path / "peer.key")

    seed = load_or_create_peer_seed(key_path)
    host = new_host(key_pair=create_new_key_pair(seed))
    listen = [multiaddr.Multiaddr(f"/ip4/{listen_host}/tcp/{port}")]

    dht = KadDHT(host, DHTMode.SERVER) if enable_dht else None

    async def _bootstrap_dht() -> None:
        if dht is None:
            return
        for maddr in bootstrap_multiaddrs or []:
            try:
                info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
                host.get_peerstore().add_addrs(info.peer_id, info.addrs, 3600)
                await host.connect(info)
                await dht.routing_table.add_peer(info.peer_id)
                logger.info("bootstrapped DHT via %s", info.peer_id)
            except Exception as e:
                logger.warning("bootstrap to %s failed: %s", maddr, e)

    async def _connect_relays() -> None:
        for maddr in relay_multiaddrs or []:
            try:
                info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
                host.get_peerstore().add_addrs(info.peer_id, info.addrs, 3600)
                await host.connect(info)
                logger.info("connected to relay %s", info.peer_id)
            except Exception as e:
                logger.warning("relay connect to %s failed: %s", maddr, e)

    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        async with AsyncExitStack() as stack:
            if dht is not None:
                await stack.enter_async_context(background_trio_service(dht))

            circuit_protocol: Optional[CircuitV2Protocol] = None
            if relay_mode in ("client", "hop"):
                limits = _default_relay_limits()
                allow_hop = relay_mode == "hop"
                circuit_protocol = CircuitV2Protocol(
                    host, limits=limits, allow_hop=allow_hop
                )
                roles = RelayRole.STOP | RelayRole.CLIENT
                if allow_hop:
                    roles |= RelayRole.HOP
                relay_config = RelayConfig(roles=roles, limits=limits)

                host.set_stream_handler(
                    HOP_PROTOCOL_ID, circuit_protocol._handle_hop_stream
                )
                host.set_stream_handler(
                    STOP_PROTOCOL_ID, circuit_protocol._handle_stop_stream
                )
                await stack.enter_async_context(
                    background_trio_service(circuit_protocol)
                )

                relay_transport = CircuitV2Transport(
                    host, circuit_protocol, relay_config
                )
                if relay_mode == "client":
                    discovery = RelayDiscovery(host, auto_reserve=True)
                    relay_transport.discovery = discovery
                    await stack.enter_async_context(background_trio_service(discovery))
                    dcutr = DCUtRProtocol(host)
                    await stack.enter_async_context(background_trio_service(dcutr))
            else:
                relay_transport = None

            peer = Peer(
                host,
                data_dir=data_dir,
                naming_info=naming_info,
                pinstore_path=pinstore_path,
                dht=dht,
                relay_transport=relay_transport,
            )
            peer.attach()
            await _bootstrap_dht()
            await _connect_relays()

            try:
                yield peer
            finally:
                nursery.cancel_scope.cancel()


async def link_peers_dht(*peers: Peer) -> None:
    """Full-mesh the DHT routing tables of the given peers.

    py-libp2p 0.6.0 doesn't auto-populate the routing table from inbound
    connections, so peers that dial each other still need an explicit
    add_peer call. This helper is mostly for tests and small deployments.
    """
    for a in peers:
        for b in peers:
            if a is b or a.dht is None or b.dht is None:
                continue
            b_id = b.host.get_id()
            for addr in b.host.get_addrs():
                a.host.get_peerstore().add_addrs(b_id, [addr], 3600)
            try:
                info = PeerInfo(b_id, list(b.host.get_addrs()))
                await a.host.connect(info)
            except Exception:
                pass
            await a.dht.routing_table.add_peer(b_id)
