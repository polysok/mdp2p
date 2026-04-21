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
    client_get_attachments as naming_get_attachments,
    client_list_reviewers as naming_list_reviewers,
    client_post_assignment as naming_post_assignment,
    client_register as naming_register,
    client_resolve as naming_resolve,
)
from pinstore import PinStatus, check_pin, pin_key, update_pin_last_seen
from review import (
    build_review_assignment,
    select_reviewers,
    sign_review_assignment,
    verify_review_record,
    verify_reviewer_opt_in,
)
from trust import (
    Policy,
    ScoreResult,
    Signal,
    load_policy,
    load_store,
    score_content as _score_content_fn,
)
from wire import recv_framed_json, send_framed_json

from .bundle_protocol import (
    BUNDLE_PROTOCOL,
    MAX_BUNDLE_MSG_SIZE,
    make_bundle_handler,
    try_download_from_seeders,
)

logger = logging.getLogger("mdp2p.peer")

DEFAULT_DATA_DIR = "./peer_data"
DEFAULT_PINSTORE = str(Path.home() / ".mdp2p" / "known_keys.json")
DEFAULT_TRUST_STORE = str(Path.home() / ".mdp2p" / "trust.json")
DEFAULT_POLICY_PATH = str(Path.home() / ".mdp2p" / "policy.json")

DEFAULT_REVIEW_COUNT = 3
DEFAULT_REVIEW_DEADLINE_DAYS = 3


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
        review_count: int = DEFAULT_REVIEW_COUNT,
        review_deadline_days: int = DEFAULT_REVIEW_DEADLINE_DAYS,
        reviewer_freshness_seconds: Optional[int] = None,
    ) -> tuple[dict, str]:
        """Create a signed bundle, register on naming, seed locally.

        After registration, the publisher also attempts to solicit reviews
        by selecting `review_count` reviewers from the naming server's
        registry and posting a signed assignment to their inbox. Reviewers
        then have up to `review_deadline_days` to attach a verdict. A
        missing or empty reviewer pool does not block publication — the
        content is still registered and announced.

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

        await self._solicit_reviews(
            uri=uri,
            publisher_pub_b64=pub_b64,
            publisher_private_key=private_key,
            review_count=review_count,
            review_deadline_days=review_deadline_days,
            freshness_seconds=reviewer_freshness_seconds,
        )

        self.sites[uri] = site_dir_resolved
        await self.announce(uri)
        return manifest, signature

    # ─── Review solicitation ─────────────────────────────────────────

    async def _solicit_reviews(
        self,
        uri: str,
        publisher_pub_b64: str,
        publisher_private_key,
        review_count: int,
        review_deadline_days: int,
        freshness_seconds: Optional[int],
    ) -> None:
        """Fetch reviewer pool, select, and post a signed assignment.

        Graceful failure: an empty pool, an unreachable naming server, or
        a rejected assignment logs a warning but never raises — publication
        must not be held hostage by the review subsystem.
        """
        try:
            listing = await naming_list_reviewers(self.host, self.naming_info)
        except Exception as e:
            logger.warning("list_reviewers failed, skipping assignment: %s", e)
            return

        entries = listing.get("records") or []
        pool = _extract_fresh_reviewer_pool(entries, freshness_seconds)
        if not pool:
            logger.info("no fresh reviewers available, skipping assignment")
            return

        content_key = compute_content_key(uri, publisher_pub_b64)
        selected = select_reviewers(content_key, pool, review_count)
        if not selected:
            logger.info("selection yielded no reviewer, skipping assignment")
            return

        import time as _time
        deadline = int(_time.time()) + review_deadline_days * 86400
        assignment = build_review_assignment(
            uri=uri,
            publisher_pubkey_b64=publisher_pub_b64,
            reviewer_pubkeys_b64=selected,
            deadline=deadline,
        )
        signature = sign_review_assignment(assignment, publisher_private_key)

        try:
            resp = await naming_post_assignment(
                self.host, self.naming_info, assignment, signature
            )
        except Exception as e:
            logger.warning("post_assignment failed: %s", e)
            return

        if resp.get("type") != "ok":
            logger.warning("post_assignment rejected: %s", resp.get("msg"))
        else:
            logger.info(
                "review assignment posted: %s → %d reviewers, deadline in %dd",
                content_key[:24],
                len(selected),
                review_deadline_days,
            )

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

        # Pull any review attachments alongside the bundle so the reader
        # can compute a score later without another network round-trip.
        # Best-effort: a failure here never fails the fetch itself.
        await self._cache_attachments(uri, author_pub_b64, site_dir)

        # Join the swarm: announce ourselves as a provider (unless the
        # caller opted out — useful for one-shot visitors that would
        # otherwise leave stale records in the DHT on exit).
        if announce_after:
            await self.announce(uri)
        return True

    async def _cache_attachments(
        self, uri: str, author_pub_b64: str, site_dir: str
    ) -> None:
        """Fetch review attachments for this content and persist them locally.

        Stored at ``{site_dir}/attachments.json`` in the same ``{record,
        signature}`` shape the naming server returns. Scoring tools read
        this file and re-verify signatures, so a corrupted or forged file
        produces zero signal rather than a wrong one.
        """
        if self.naming_info is None:
            return
        content_key = compute_content_key(uri, author_pub_b64)
        try:
            resp = await naming_get_attachments(
                self.host, self.naming_info, content_key
            )
        except Exception as e:
            logger.debug("attachment fetch for %s failed: %s", uri, e)
            return

        entries = resp.get("records") or []
        path = Path(site_dir) / "attachments.json"
        try:
            import json as _json
            path.write_text(
                _json.dumps(
                    {"content_key": content_key, "records": entries},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            logger.info("cached %d review attachment(s) for %s", len(entries), uri)
        except Exception as e:
            logger.warning("could not write attachments.json for %s: %s", uri, e)

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

    # ─── Reader-side scoring ─────────────────────────────────────────

    async def compute_score(
        self,
        uri: str,
        policy_path: Optional[str] = None,
        trust_store_path: Optional[str] = None,
        now: Optional[int] = None,
    ) -> ScoreResult:
        """Fetch attached reviews for a content and run the scorer locally.

        Uses the reader's local trust store and moderation policy from
        ``~/.mdp2p/`` by default. The scoring is local: what this peer
        decides to show, warn on, or hide is purely its own business.
        """
        if self.naming_info is None:
            raise ValueError("cannot score without a naming server configured")
        validate_uri(uri)

        resp = await naming_resolve(self.host, self.naming_info, uri)
        if resp.get("type") != "record":
            raise RuntimeError(
                f"naming resolve failed for {uri}: {resp.get('msg')}"
            )
        author_pub_b64 = resp["record"]["public_key"]
        content_key = compute_content_key(uri, author_pub_b64)

        try:
            att_resp = await naming_get_attachments(
                self.host, self.naming_info, content_key
            )
        except Exception as e:
            logger.warning("get_attachments failed for %s: %s", uri, e)
            att_resp = {"type": "attachments", "records": []}

        signals = _attachments_to_signals(att_resp.get("records") or [], content_key)

        policy = load_policy(policy_path or DEFAULT_POLICY_PATH)
        store = load_store(trust_store_path or DEFAULT_TRUST_STORE)
        return _score_content_fn(signals, store, policy, now=now)

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


def _attachments_to_signals(
    entries: list[dict],
    expected_content_key: str,
) -> list[Signal]:
    """Verify and convert naming-server attachment entries into scorer Signals.

    Drops entries with invalid signatures or content_key mismatches — these
    are either corruption or a malicious server trying to inject reviews
    under the wrong key, and should not influence the score either way.
    """
    signals: list[Signal] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = entry.get("record")
        signature = entry.get("signature", "")
        if not isinstance(record, dict) or not signature:
            continue
        ok, _ = verify_review_record(record, signature, max_drift=None)
        if not ok:
            continue
        if record.get("content_key") != expected_content_key:
            continue
        signals.append(
            Signal(
                kind="review",
                content_key=expected_content_key,
                source_pubkey=record.get("reviewer_public_key", ""),
                verdict=record.get("verdict", "ok"),
                reason=record.get("comment", ""),
                timestamp=int(record.get("timestamp", 0)),
            )
        )
    return signals


def _extract_fresh_reviewer_pool(
    entries: list[dict],
    freshness_seconds: Optional[int],
) -> list[str]:
    """Extract verified reviewer public keys from a list_reviewers listing.

    Drops entries with invalid signatures; optionally drops those whose
    last-known timestamp is older than `freshness_seconds`. Returning an
    empty list is valid (caller handles it gracefully).
    """
    import time as _time

    now = int(_time.time())
    pool: list[str] = []
    for entry in entries:
        record = entry.get("record") if isinstance(entry, dict) else None
        signature = entry.get("signature", "") if isinstance(entry, dict) else ""
        if not isinstance(record, dict) or not signature:
            continue
        # max_drift=None: we accept records stored long ago on the server,
        # since the naming server enforces freshness at register time. We
        # verify the signature only — the subsequent freshness filter uses
        # the record's own timestamp.
        ok, _ = verify_reviewer_opt_in(record, signature, max_drift=None)
        if not ok:
            continue
        if freshness_seconds is not None:
            if now - int(record.get("timestamp", 0)) > freshness_seconds:
                continue
        pubkey = record.get("public_key", "")
        if pubkey:
            pool.append(pubkey)
    return pool
