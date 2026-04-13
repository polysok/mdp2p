"""
MDP2P Peer — P2P node that downloads, verifies, seeds and renders Markdown sites.

A peer has two roles:
  - Client: resolves a URI via the tracker, downloads the bundle from another peer
  - Seeder: listens on a port and serves the bundles it owns

Once a client downloads a site, it automatically becomes a seeder.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from bundle import (
    bundle_to_dict,
    dict_to_bundle,
    verify_manifest,
    verify_files,
    load_bundle,
    save_bundle,
    create_manifest,
    sign_manifest,
    load_private_key,
    public_key_to_b64,
    b64_to_public_key,
    create_register_proof,
    is_manifest_expired,
    validate_path,
)
from pinstore import PinStatus, load_pinstore, check_pin, pin_key, update_pin_last_seen
from protocol import CONNECT_TIMEOUT, send_msg, recv_msg, validate_uri

logger = logging.getLogger("mdp2p.peer")

DOWNLOAD_TIMEOUT = 30  # seconds per peer download


class Peer:
    def __init__(
        self,
        data_dir: str = "./peer_data",
        host: str = "127.0.0.1",
        port: int = 5000,
        tracker_host: str = "127.0.0.1",
        tracker_port: int = 1707,
        pinstore_path: Optional[str] = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self.tracker_host = tracker_host
        self.tracker_port = tracker_port
        self.pinstore_path = pinstore_path or str(
            Path.home() / ".mdp2p" / "known_keys.json"
        )
        self.sites: dict = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._stopping = False

    # ─── Tracker communication ───────────────────────────────────────

    async def _tracker_request(self, msg: dict) -> dict:
        """Send a message to the tracker and return the response."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.tracker_host, self.tracker_port),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            await send_msg(writer, msg)
            response = await recv_msg(reader)
            return response or {"type": "error", "msg": "No response from tracker"}
        finally:
            writer.close()
            await writer.wait_closed()

    async def register_site(
        self, uri: str, author: str, public_key_b64: str, proof: str, timestamp: int
    ) -> dict:
        """Register a URI alias on the tracker with proof of key ownership."""
        return await self._tracker_request(
            {
                "type": "register",
                "uri": uri,
                "author": author,
                "public_key": public_key_b64,
                "proof": proof,
                "timestamp": timestamp,
            }
        )

    async def announce(self, uri: str) -> dict:
        """Announce self as a seeder for a site."""
        return await self._tracker_request(
            {"type": "announce", "uri": uri, "host": self.host, "port": self.port}
        )

    async def unannounce(self, uri: str) -> dict:
        """Remove self from a site's swarm."""
        return await self._tracker_request(
            {"type": "unannounce", "uri": uri, "host": self.host, "port": self.port}
        )

    async def resolve(self, uri: str) -> dict:
        """Resolve a URI to a public key and list of peers."""
        return await self._tracker_request({"type": "resolve", "uri": uri})

    async def list_sites(self) -> dict:
        """List all sites registered on the tracker."""
        return await self._tracker_request({"type": "list"})

    # ─── Publishing (author) ─────────────────────────────────────────

    async def publish(
        self, uri: str, author: str, site_dir: str, private_key_path: str
    ):
        """Publish a site: create the bundle, register on the tracker, start seeding."""
        validate_uri(uri)
        site_dir = str(Path(site_dir).resolve())
        private_key = load_private_key(private_key_path)
        pub_b64 = public_key_to_b64(private_key.public_key())

        version = 1
        existing_manifest = Path(site_dir) / "manifest.json"
        if existing_manifest.exists():
            try:
                old, _ = load_bundle(site_dir)
                version = old.get("version", 0) + 1
            except Exception:
                pass

        manifest = create_manifest(site_dir, uri=uri, author=author, version=version)
        manifest, signature = sign_manifest(manifest, private_key)
        save_bundle(site_dir, manifest, signature)

        logger.info(
            f"Bundle created: {manifest['file_count']} files, {manifest['total_size']} bytes"
        )

        proof, timestamp = create_register_proof(uri, author, private_key)
        result = await self.register_site(uri, author, pub_b64, proof, timestamp)
        logger.info(f"Register: {result.get('msg', result)}")

        self.sites[uri] = site_dir

        result = await self.announce(uri)
        logger.info(f"Announce: swarm = {result.get('peers', '?')} peers")

    # ─── Downloading (client) ────────────────────────────────────────

    async def _try_download(
        self, peer_host: str, peer_port: int, uri: str
    ) -> Optional[dict]:
        """Try to download from one peer with a timeout."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(peer_host, peer_port),
                timeout=DOWNLOAD_TIMEOUT,
            )
            try:
                await send_msg(writer, {"type": "get_bundle", "uri": uri})
                response = await asyncio.wait_for(
                    recv_msg(reader), timeout=DOWNLOAD_TIMEOUT
                )
                return response
            finally:
                writer.close()
                await writer.wait_closed()
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"Connection failed to {peer_host}:{peer_port} — {e}")
            return None

    async def fetch_site(self, uri: str) -> bool:
        """Resolve a URI, download the site, verify it, and start seeding."""
        validate_uri(uri)
        resolution = await self.resolve(uri)
        if resolution.get("type") == "error":
            logger.error(f"Resolution failed: {resolution.get('msg')}")
            return False

        author = resolution.get("author", "unknown")
        public_key_b64 = resolution["public_key"]
        peers = resolution["peers"]
        logger.info(f"Resolved {uri} by {author} → {len(peers)} available peers")

        # TOFU key pinning check
        pinstore = load_pinstore(self.pinstore_path)
        pin_status = check_pin(pinstore, uri, public_key_b64)

        if pin_status == PinStatus.MISMATCH:
            logger.error(
                f"ALERT: Public key changed for '{uri}'! "
                f"Possible MITM attack. Aborting."
            )
            return False

        if not peers:
            logger.error("No peer available!")
            return False

        other_peers = [
            p for p in peers
            if not (p["host"] == self.host and p["port"] == self.port)
        ]
        if not other_peers:
            logger.error("No remote peer available!")
            return False

        pending = [
            asyncio.create_task(self._try_download(p["host"], p["port"], uri))
            for p in other_peers
        ]
        bundle_data = None
        try:
            for future in asyncio.as_completed(pending):
                result = await future
                if result and result.get("type") == "bundle":
                    bundle_data = result
                    break
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()

        if not bundle_data:
            logger.error("No peer could provide the bundle")
            return False

        manifest = bundle_data["manifest"]
        signature = bundle_data["signature"]
        trusted_key = b64_to_public_key(public_key_b64)

        if not verify_manifest(manifest, signature, trusted_key):
            logger.error("ALERT: Invalid signature or key mismatch!")
            return False
        logger.info("Signature verified")

        if is_manifest_expired(manifest):
            logger.error("ALERT: Bundle has expired!")
            return False

        site_dir = str(self.data_dir / uri)
        dict_to_bundle(bundle_data, site_dir)

        errors = verify_files(manifest, site_dir)
        if errors:
            logger.error(f"Corrupted files: {errors}")
            return False
        logger.info("File integrity verified")

        # Pin the key after successful verification
        if pin_status == PinStatus.UNKNOWN:
            pin_key(uri, public_key_b64, author, self.pinstore_path)
            logger.info(f"Key pinned for '{uri}' (first visit)")
        else:
            update_pin_last_seen(uri, self.pinstore_path)

        self.sites[uri] = site_dir
        result = await self.announce(uri)
        logger.info(f"Now seeding! Swarm = {result.get('peers', '?')} peers")
        return True

    # ─── Update check ──────────────────────────────────────────────

    async def check_for_update(self, uri: str) -> bool:
        """Check if a newer version of a site exists on the network.

        Returns True if a peer has a newer timestamp than our local copy.
        """
        if uri not in self.sites:
            return False

        local_manifest, _ = load_bundle(self.sites[uri])
        local_ts = local_manifest.get("timestamp", 0)

        resolution = await self.resolve(uri)
        if resolution.get("type") == "error":
            return False

        for peer in resolution.get("peers", []):
            if peer["host"] == self.host and peer["port"] == self.port:
                continue
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(peer["host"], peer["port"]),
                    timeout=DOWNLOAD_TIMEOUT,
                )
                try:
                    await send_msg(writer, {"type": "get_manifest", "uri": uri})
                    response = await asyncio.wait_for(
                        recv_msg(reader), timeout=DOWNLOAD_TIMEOUT
                    )
                    if response and response.get("type") == "manifest":
                        remote_ts = response["manifest"].get("timestamp", 0)
                        return remote_ts > local_ts
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                continue

        return False

    # ─── Seeder server ───────────────────────────────────────────────

    async def _handle_peer_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming requests from other peers."""
        addr = writer.get_extra_info("peername")
        try:
            while not self._stopping:
                msg = await recv_msg(reader)
                if msg is None:
                    break
                msg_type = msg.get("type", "")

                if msg_type == "get_bundle":
                    uri = msg.get("uri", "")
                    if uri in self.sites:
                        logger.info(f"Sending bundle '{uri}' to {addr}")
                        data = bundle_to_dict(self.sites[uri])
                        data["type"] = "bundle"
                        await send_msg(writer, data)
                    else:
                        await send_msg(
                            writer,
                            {"type": "error", "msg": f"Site '{uri}' not found"},
                        )

                elif msg_type == "get_manifest":
                    uri = msg.get("uri", "")
                    if uri in self.sites:
                        manifest, signature = load_bundle(self.sites[uri])
                        await send_msg(
                            writer,
                            {
                                "type": "manifest",
                                "manifest": manifest,
                                "signature": signature,
                            },
                        )
                    else:
                        await send_msg(
                            writer,
                            {"type": "error", "msg": f"Site '{uri}' not found"},
                        )

                else:
                    await send_msg(
                        writer, {"type": "error", "msg": f"Unknown: {msg_type}"}
                    )
        except Exception as e:
            logger.error(f"Peer error {addr}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def start_seeding(self) -> asyncio.AbstractServer:
        """Start the seeder server."""
        self._server = await asyncio.start_server(
            self._handle_peer_request, self.host, self.port
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info(f"Seeder listening on {addrs}")
        if self.port == 0:
            actual_port = self._server.sockets[0].getsockname()[1]
            self.port = actual_port
            logger.info(f"Auto-assigned port: {actual_port}")
        return self._server

    async def close(self) -> None:
        """Gracefully close the seeder server."""
        self._stopping = True
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def stop(self) -> None:
        """Synchronous stop. Prefer close() in async code."""
        self._stopping = True
        if self._server:
            self._server.close()

    # ─── Markdown rendering (terminal) ─────────────────────────────────

    def render_site(self, uri: str) -> str:
        """Minimal text rendering of a site for the terminal."""
        if uri not in self.sites:
            return f"Site '{uri}' not found locally."

        site_dir = Path(self.sites[uri])
        manifest, _ = load_bundle(str(site_dir))
        output = []
        output.append(f"\n{'━' * 60}")
        output.append(f"  md://{uri}")
        output.append(
            f"  {manifest['file_count']} pages — {manifest['total_size']} bytes"
        )
        output.append(f"  version {manifest['version']}")
        output.append(f"{'━' * 60}\n")

        for entry in manifest["files"]:
            fpath = validate_path(site_dir, entry["path"])
            content = fpath.read_text(encoding="utf-8")
            output.append(
                f"┌─ {entry['path']} ─{'─' * max(0, 40 - len(entry['path']))}┐"
            )
            output.append(content.strip())
            output.append(f"└{'─' * 50}┘\n")

        return "\n".join(output)