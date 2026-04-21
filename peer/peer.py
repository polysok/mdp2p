"""The ``Peer`` class — stateful wrapper around a libp2p host.

Owns the mapping uri → local site directory, talks to the naming service
for publish/resolve, and drives the DHT for provider advertisement.
Wire-level concerns (streams, framing, parallel dials) live in
``bundle_protocol``; host/DHT/relay assembly lives in ``host_factory``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import multiaddr
from libp2p.abc import IHost
from libp2p.kad_dht.kad_dht import KadDHT
from libp2p.peer.peerinfo import PeerInfo, info_from_p2p_addr
from libp2p.relay.circuit_v2.transport import CircuitV2Transport

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
from naming import (
    client_register as naming_register,
    client_resolve as naming_resolve,
)
from pinstore import PinStatus, check_pin, pin_key, update_pin_last_seen
from wire import recv_framed_json, send_framed_json

from .bundle_protocol import (
    BUNDLE_PROTOCOL,
    MAX_BUNDLE_MSG_SIZE,
    make_bundle_handler,
    try_download_from_seeders,
)
from .review_protocol import (
    REVIEW_PROTOCOL,
    CollectedReview,
    ReviewerCallback,
    auto_decline,
    make_review_handler,
    request_reviews,
)

logger = logging.getLogger("mdp2p.peer")

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
        handler = make_bundle_handler(lambda uri: self.sites.get(uri))
        self.host.set_stream_handler(BUNDLE_PROTOCOL, handler)
        self._rediscover_local_sites()

    def attach_reviewer(
        self,
        reviewer_private_key,
        reviewer_public_key_b64: str,
        callback: ReviewerCallback = auto_decline,
    ) -> None:
        """Opt in to the reviewer role: register the REVIEW_PROTOCOL handler.

        Publishers that discover this peer via the naming server's reviewer
        registry will be able to send it review requests. The supplied
        callback decides whether to issue a verdict (TUI prompt, policy,
        or the default auto-decline).
        """
        handler = make_review_handler(
            reviewer_private_key, reviewer_public_key_b64, callback
        )
        self.host.set_stream_handler(REVIEW_PROTOCOL, handler)

    async def request_reviews(
        self,
        content_key: str,
        manifest: dict,
        manifest_signature: str,
        reviewer_addrs: list[str],
        timeout_seconds: float = 60.0,
    ) -> list[CollectedReview]:
        """Fan out review requests to the selected reviewers, collect results."""
        return await request_reviews(
            self.host,
            self.relay_transport,
            content_key,
            manifest,
            manifest_signature,
            reviewer_addrs,
            timeout_seconds,
        )

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
        """Return multiaddr strings of peers advertising `uri` in the DHT.

        Optimistic fast-path: if a naming server is configured, we ask it
        directly via a single DHT GetProviders request instead of paying
        for py-libp2p's full iterative Kademlia lookup (which can drag on
        for tens of seconds when the peer-zero's routing table contains
        dead peers from old test runs). In mdp2p the naming server always
        doubles as the DHT hub — any live provider record is either there
        or doesn't exist anywhere.
        """
        if self.dht is None:
            return []
        key_bytes = compute_content_key(uri, author_pub_b64).encode()

        providers = []
        if self.naming_info is not None:
            try:
                providers = await self.dht.provider_store._get_providers_from_peer(
                    self.naming_info.peer_id, key_bytes
                )
            except Exception as e:
                logger.warning(
                    "fast-path provider query failed for %s: %s — falling back",
                    uri, e,
                )

        if not providers:
            try:
                providers = await self.dht.find_providers(key_bytes.decode())
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
        # Skip the drift check at read time: the naming server already
        # enforces monotonic + fresh timestamps at register time, so a
        # legitimately-stored record stays valid after the drift window.
        ok, err = verify_name_record(record, record_sig, max_drift=None)
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

        bundle_data = await try_download_from_seeders(
            self.host, self.relay_transport, uri, seeder_addrs, logger
        )
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

    # ─── Rendering ──────────────────────────────────────────────────

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
