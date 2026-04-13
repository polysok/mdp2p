"""
MDP2P Tracker — URI → Peers resolution server.

The tracker maintains two tables:
  - sites   : URI → SiteRecord with public_key, proof, timestamp, origin
  - swarms  : URI → set of (host, port) active peers

Supported messages:
  REGISTER    : The author registers a URI alias for their public key
  ANNOUNCE    : A peer signals that it is seeding a site
  UNANNOUNCE  : A peer signals that it stopped seeding
  RESOLVE     : A client requests the peers for a URI
  LIST        : Lists all registered sites
  FED_SYNC    : Federation sync (exchange registrations between trackers)
  FED_GOSSIP  : Broadcast a new registration to known trackers
"""

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from bundle import verify_register_proof, MAX_TIMESTAMP_DRIFT_SECONDS
from protocol import CONNECT_TIMEOUT, RateLimiter, send_msg, recv_msg, validate_uri

try:
    import redis.asyncio as redis

    _REDIS_AVAILABLE = True
except ImportError:
    redis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

logger = logging.getLogger("mdp2p.tracker")

SITES_KEY = "mdp2p:sites"
SWARM_KEY_PREFIX = "mdp2p:swarm:"
PEER_TTL_SECONDS = 600  # 10 minutes — peers must re-announce before expiry
CLEANUP_INTERVAL_SECONDS = 60


@dataclass
class SiteRecord:
    """Immutable registration record signed by the author."""

    author: str
    public_key: str
    proof: str
    timestamp: int
    origin_tracker: Optional[str] = None
    registered_at: float = field(default_factory=time.time)


class Tracker:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 1707,
        peer_trackers: Optional[List[Tuple[str, int]]] = None,
        name: Optional[str] = None,
        redis_url: str = "redis://localhost:6379",
        redis_enabled: bool = True,
        rate_limit: int = 100,
        rate_window: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.name = name or f"{host}:{port}"
        self.peer_trackers = peer_trackers or []
        self.redis_url = redis_url
        self.redis_enabled = redis_enabled and _REDIS_AVAILABLE
        if redis_enabled and not _REDIS_AVAILABLE:
            logger.warning("redis package not installed — Redis persistence disabled")
        self._redis: Optional[object] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._gossip_task: Optional[asyncio.Task] = None
        self._rate_limiter = RateLimiter(
            max_requests=rate_limit, window_seconds=rate_window
        )
        self._lock = asyncio.Lock()
        self._sites: Dict[str, SiteRecord] = {}
        self._swarms: Dict[str, Set[Tuple[str, int]]] = {}
        self._peer_last_seen: Dict[Tuple[str, str, int], float] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._stopping = False

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def _init_redis(self) -> None:
        """Initialize Redis connection and load existing data."""
        if not self.redis_enabled:
            logger.info("Redis persistence disabled")
            return

        try:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info(f"Redis connected: {self.redis_url}")
            await self._load_from_redis()
        except Exception as e:
            logger.warning(f"Redis unavailable: {e}. Running without persistence.")
            self._redis = None

    async def _load_from_redis(self) -> None:
        """Load sites and swarms from Redis on startup."""
        if not self._redis:
            return

        try:
            sites_data = await self._redis.hgetall(SITES_KEY)
            for uri, data_str in sites_data.items():
                data = json.loads(data_str)
                self._sites[uri] = SiteRecord(
                    author=data["author"],
                    public_key=data["public_key"],
                    proof=data["proof"],
                    timestamp=data["timestamp"],
                    origin_tracker=data.get("origin_tracker"),
                    registered_at=data.get("registered_at", time.time()),
                )

            keys = await self._redis.keys(f"{SWARM_KEY_PREFIX}*")
            for key in keys:
                uri = key[len(SWARM_KEY_PREFIX):]
                members = await self._redis.smembers(key)
                self._swarms[uri] = set()
                for member in members:
                    host, port_str = member.rsplit(":", 1)
                    self._swarms[uri].add((host, int(port_str)))

            count = len(self._sites)
            swarm_count = len(self._swarms)
            logger.info(f"Loaded {count} sites and {swarm_count} swarms from Redis")
        except Exception as e:
            logger.error(f"Failed to load from Redis: {e}")

    async def _save_site_to_redis(self, uri: str, record: SiteRecord) -> None:
        if not self._redis:
            return
        try:
            data = {
                "author": record.author,
                "public_key": record.public_key,
                "proof": record.proof,
                "timestamp": record.timestamp,
                "origin_tracker": record.origin_tracker,
                "registered_at": record.registered_at,
            }
            await self._redis.hset(SITES_KEY, uri, json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to save site to Redis: {e}")

    async def _remove_site_from_redis(self, uri: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hdel(SITES_KEY, uri)
            await self._redis.delete(f"{SWARM_KEY_PREFIX}{uri}")
        except Exception as e:
            logger.error(f"Failed to remove site from Redis: {e}")

    async def _add_peer_to_swarm(self, uri: str, host: str, port: int) -> None:
        if not self._redis:
            return
        try:
            await self._redis.sadd(f"{SWARM_KEY_PREFIX}{uri}", f"{host}:{port}")
        except Exception as e:
            logger.error(f"Failed to add peer to swarm in Redis: {e}")

    async def _remove_peer_from_swarm(self, uri: str, host: str, port: int) -> None:
        if not self._redis:
            return
        try:
            await self._redis.srem(f"{SWARM_KEY_PREFIX}{uri}", f"{host}:{port}")
        except Exception as e:
            logger.error(f"Failed to remove peer from swarm in Redis: {e}")

    async def _get_swarm_from_redis(self, uri: str) -> Set[Tuple[str, int]]:
        if not self._redis:
            return set()
        try:
            members = await self._redis.smembers(f"{SWARM_KEY_PREFIX}{uri}")
            return {
                (m.rsplit(":", 1)[0], int(m.rsplit(":", 1)[1])) for m in members
            }
        except Exception as e:
            logger.error(f"Failed to get swarm from Redis: {e}")
            return set()

    def _get_swarm(self, uri: str) -> Set[Tuple[str, int]]:
        """Return in-memory swarm (may be empty for unknown URIs)."""
        return self._swarms.get(uri, set())

    # ─── Request handlers ────────────────────────────────────────────────

    async def _handle_register(self, msg: dict) -> dict:
        uri = msg.get("uri", "")
        author = msg.get("author", "")
        public_key = msg.get("public_key", "")
        proof = msg.get("proof", "")
        timestamp = msg.get("timestamp", 0)

        if not uri or not public_key or not author:
            return {"type": "error", "msg": "uri, author, and public_key required"}
        try:
            validate_uri(uri)
        except ValueError as e:
            return {"type": "error", "msg": str(e)}
        if not proof:
            return {"type": "error", "msg": "proof of key ownership required"}
        if not timestamp:
            return {"type": "error", "msg": "timestamp required"}

        is_valid, error_msg = verify_register_proof(
            uri, author, public_key, proof, timestamp
        )
        if not is_valid:
            return {"type": "error", "msg": f"invalid proof — {error_msg}"}

        async with self._lock:
            existing = self._sites.get(uri)
            if existing:
                if existing.public_key != public_key:
                    return {
                        "type": "error",
                        "msg": f"URI '{uri}' already registered with different key",
                    }
                if existing.timestamp > timestamp:
                    return {
                        "type": "error",
                        "msg": f"URI '{uri}' registered at a later timestamp",
                    }

            record = SiteRecord(
                author=author,
                public_key=public_key,
                proof=proof,
                timestamp=timestamp,
                origin_tracker=self.name,
            )
            self._sites[uri] = record
            self._swarms.setdefault(uri, set())

        logger.info(
            f"REGISTER  {author}@{uri} → {public_key[:16]}... (ts: {timestamp})"
        )
        asyncio.create_task(self._save_site_to_redis(uri, record))
        return {"type": "ok", "msg": f"Site '{uri}' registered by '{author}'"}

    async def _handle_announce(self, msg: dict, client_host: str) -> dict:
        uri = msg.get("uri", "")
        port = msg.get("port", 0)
        if not uri or not port:
            return {"type": "error", "msg": "uri and port required"}
        if not isinstance(port, int) or port < 1 or port > 65535:
            return {"type": "error", "msg": "port must be between 1 and 65535"}
        try:
            validate_uri(uri)
        except ValueError as e:
            return {"type": "error", "msg": str(e)}
        if uri not in self._sites:
            return {"type": "error", "msg": f"Unknown URI '{uri}'. Register first."}

        # Always use the actual connection IP to prevent swarm poisoning
        host = client_host

        async with self._lock:
            self._swarms[uri].add((host, port))
            self._peer_last_seen[(uri, host, port)] = time.monotonic()
            peer_count = len(self._swarms[uri])

        logger.info(f"ANNOUNCE  {uri} ← {host}:{port}  (swarm: {peer_count} peers)")
        asyncio.create_task(self._add_peer_to_swarm(uri, host, port))
        return {"type": "ok", "peers": peer_count}

    async def _handle_unannounce(self, msg: dict) -> dict:
        uri = msg.get("uri", "")
        host = msg.get("host", "")
        port = msg.get("port", 0)

        async with self._lock:
            self._swarms.get(uri, set()).discard((host, port))
            self._peer_last_seen.pop((uri, host, port), None)

        logger.info(f"UNANNOUNCE {uri} ← {host}:{port}")
        asyncio.create_task(self._remove_peer_from_swarm(uri, host, port))
        return {"type": "ok"}

    async def _handle_resolve(self, msg: dict) -> dict:
        uri = msg.get("uri", "")
        try:
            validate_uri(uri)
        except ValueError as e:
            return {"type": "error", "msg": str(e)}
        if uri not in self._sites:
            return {"type": "error", "msg": f"Unknown URI '{uri}'"}

        site = self._sites[uri]
        swarm = set(self._get_swarm(uri))

        if self._redis and self.redis_enabled:
            redis_swarm = await self._get_swarm_from_redis(uri)
            swarm.update(redis_swarm)

        peers = [{"host": h, "port": p} for h, p in swarm]
        logger.info(f"RESOLVE   {uri} → {len(peers)} peers")
        return {
            "type": "peers",
            "uri": uri,
            "author": site.author,
            "public_key": site.public_key,
            "peers": peers,
        }

    async def _handle_list(self, msg: dict) -> dict:
        sites = []
        for uri, record in self._sites.items():
            sites.append(
                {
                    "uri": uri,
                    "author": record.author,
                    "public_key": record.public_key[:16] + "...",
                    "peers": len(self._get_swarm(uri)),
                    "timestamp": record.timestamp,
                }
            )
        return {"type": "site_list", "sites": sites}

    async def _handle_fed_sync(self, msg: dict) -> dict:
        """Handle federation sync request. Return all registrations."""
        incoming = msg.get("registrations", [])
        accepted = []
        rejected = []

        for reg in incoming:
            uri = reg.get("uri", "")
            reason = await self._try_import_registration(
                uri,
                reg.get("author", ""),
                reg.get("public_key", ""),
                reg.get("proof", ""),
                reg.get("timestamp", 0),
                reg.get("origin_tracker", self.name),
            )
            if reason is None:
                accepted.append(uri)
            else:
                rejected.append({"uri": uri, "reason": reason})

        return {
            "type": "fed_sync_ack",
            "accepted": accepted,
            "rejected": rejected,
            "registrations": self._export_registrations(),
        }

    async def _handle_fed_gossip(self, msg: dict) -> dict:
        """Handle gossip broadcast."""
        reason = await self._try_import_registration(
            msg.get("uri", ""),
            msg.get("author", ""),
            msg.get("public_key", ""),
            msg.get("proof", ""),
            msg.get("timestamp", 0),
            msg.get("origin_tracker", self.name),
        )
        if reason is None:
            return {"type": "ok"}
        if reason == "already have newer registration":
            return {"type": "ok", "msg": "Already have newer registration"}
        return {"type": "error", "msg": f"Invalid gossip: {reason}"}

    async def _try_import_registration(
        self,
        uri: str,
        author: str,
        public_key: str,
        proof: str,
        timestamp: int,
        origin: str,
    ) -> Optional[str]:
        """Try to import a federated registration.

        Returns None on success, or an error/skip reason string.
        """
        # Federation imports: verify signature but skip timestamp drift,
        # since the proof was already validated by the origin tracker.
        is_valid, error_msg = verify_register_proof(
            uri, author, public_key, proof, timestamp, max_drift=None
        )
        if not is_valid:
            return error_msg

        async with self._lock:
            existing = self._sites.get(uri)
            if existing and existing.timestamp >= timestamp:
                return "already have newer registration"

            record = SiteRecord(
                author=author,
                public_key=public_key,
                proof=proof,
                timestamp=timestamp,
                origin_tracker=origin,
            )
            self._sites[uri] = record
            self._swarms.setdefault(uri, set())

        logger.info(f"FED_IMPORT {author}@{uri} from {origin} (ts: {timestamp})")
        asyncio.create_task(self._save_site_to_redis(uri, record))
        return None

    def _export_registrations(self) -> List[dict]:
        """Export all registrations for federation sync."""
        return [
            {
                "uri": uri,
                "author": record.author,
                "public_key": record.public_key,
                "proof": record.proof,
                "timestamp": record.timestamp,
                "origin_tracker": record.origin_tracker,
            }
            for uri, record in self._sites.items()
        ]

    # ─── Peer TTL cleanup ────────────────────────────────────────────────

    async def _cleanup_stale_peers(self) -> None:
        """Remove peers that haven't re-announced within PEER_TTL."""
        now = time.monotonic()
        async with self._lock:
            expired = [
                key for key, ts in self._peer_last_seen.items()
                if now - ts > PEER_TTL_SECONDS
            ]
            for uri, host, port in expired:
                self._swarms.get(uri, set()).discard((host, port))
                del self._peer_last_seen[(uri, host, port)]

        for uri, host, port in expired:
            asyncio.create_task(self._remove_peer_from_swarm(uri, host, port))

        if expired:
            logger.info(f"Cleaned up {len(expired)} stale peer(s)")

        self._rate_limiter.cleanup()

    async def _cleanup_loop(self) -> None:
        """Periodically remove stale peers."""
        while not self._stopping:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            if self._stopping:
                break
            await self._cleanup_stale_peers()

    # ─── Federation ───────────────────────────────────────────────────────

    async def _sync_with_tracker(self, host: str, port: int) -> bool:
        """Sync registrations with a peer tracker."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
            )
            try:
                await send_msg(writer, {"type": "fed_sync", "registrations": []})
                response = await recv_msg(reader)

                if response and response.get("type") == "fed_sync_ack":
                    incoming = response.get("registrations", [])
                    for reg in incoming:
                        await self._try_import_registration(
                            reg.get("uri", ""),
                            reg.get("author", ""),
                            reg.get("public_key", ""),
                            reg.get("proof", ""),
                            reg.get("timestamp", 0),
                            reg.get("origin_tracker", f"{host}:{port}"),
                        )

                    logger.info(
                        f"FED_SYNC  completed with {host}:{port} ({len(incoming)} registrations)"
                    )
                    return True
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as e:
            logger.warning(f"FED_SYNC  failed with {host}:{port}: {e}")
        return False

    async def _gossip_to_one(
        self, host: str, port: int, msg: dict
    ) -> None:
        """Send a gossip message to a single tracker."""
        if f"{host}:{port}" == self.name:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
            )
            try:
                await send_msg(writer, msg)
                response = await recv_msg(reader)
                if response and response.get("type") == "ok":
                    logger.info(f"FED_GOSSIP sent {msg['uri']} to {host}:{port}")
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as e:
            logger.warning(f"FED_GOSSIP failed to {host}:{port}: {e}")

    async def _gossip_registration(
        self, uri: str, author: str, public_key: str, proof: str, timestamp: int
    ) -> None:
        """Broadcast a new registration to all peer trackers in parallel."""
        msg = {
            "type": "fed_gossip",
            "uri": uri,
            "author": author,
            "public_key": public_key,
            "proof": proof,
            "timestamp": timestamp,
            "origin_tracker": self.name,
        }
        tasks = [
            self._gossip_to_one(h, p, msg) for h, p in self.peer_trackers
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _federation_loop(self) -> None:
        """Periodically sync with peer trackers."""
        while not self._stopping:
            await asyncio.sleep(60)
            if self._stopping:
                break
            for host, port in self.peer_trackers:
                await self._sync_with_tracker(host, port)

    # ─── Connection handler ───────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        client_host = addr[0] if addr else "unknown"
        logger.debug(f"Connection from {addr}")

        try:
            while not self._stopping:
                msg = await recv_msg(reader)
                if msg is None:
                    break

                if not self._rate_limiter.is_allowed(client_host):
                    await send_msg(
                        writer, {"type": "error", "msg": "Rate limit exceeded"}
                    )
                    continue

                msg_type = msg.get("type", "")
                handler = {
                    "register": self._handle_register,
                    "announce": self._handle_announce,
                    "unannounce": self._handle_unannounce,
                    "resolve": self._handle_resolve,
                    "list": self._handle_list,
                    "fed_sync": self._handle_fed_sync,
                    "fed_gossip": self._handle_fed_gossip,
                }.get(msg_type)

                if handler:
                    if msg_type == "announce":
                        response = await handler(msg, client_host)
                    else:
                        response = await handler(msg)

                    if (
                        msg_type == "register"
                        and response.get("type") == "ok"
                        and self.peer_trackers
                    ):
                        asyncio.create_task(
                            self._gossip_registration(
                                msg.get("uri", ""),
                                msg.get("author", ""),
                                msg.get("public_key", ""),
                                msg.get("proof", ""),
                                msg.get("timestamp", 0),
                            )
                        )
                else:
                    response = {
                        "type": "error",
                        "msg": f"Unknown message type: {msg_type}",
                    }

                await send_msg(writer, response)
        except Exception as e:
            logger.error(f"Error with {addr}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    # ─── Server lifecycle ───────────────────────────────────────────────

    async def start(self) -> asyncio.AbstractServer:
        await self._init_redis()

        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info(f"Tracker listening on {self.host}:{self.port}")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self.peer_trackers:
            logger.info(f"Peer trackers: {self.peer_trackers}")
            self._gossip_task = asyncio.create_task(self._federation_loop())
        return self._server

    async def serve_forever(self) -> None:
        server = await self.start()
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Received shutdown signal")
            self._stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        async with server:
            await server.serve_forever()

    async def close(self) -> None:
        """Gracefully close all resources."""
        self._stopping = True

        for task in (self._gossip_task, self._cleanup_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self._redis:
            await self._redis.close()

    def stop(self) -> None:
        """Synchronous stop — for simple scripts. Prefer close() in async code."""
        self._stopping = True
        for task in (self._gossip_task, self._cleanup_task):
            if task:
                task.cancel()
        if self._server:
            self._server.close()


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [TRACKER] %(message)s",
        datefmt="%H:%M:%S",
    )

    port = 1707
    peer_trackers: List[Tuple[str, int]] = []
    redis_url = "redis://localhost:6379"
    redis_enabled = True

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--peers" and i + 1 < len(args):
            peer_strs = args[i + 1].split(",")
            for p in peer_strs:
                if ":" in p:
                    h, p_port = p.split(":")
                    peer_trackers.append((h, int(p_port)))
            i += 2
        elif args[i] == "--redis-url" and i + 1 < len(args):
            redis_url = args[i + 1]
            i += 2
        elif args[i] == "--no-redis":
            redis_enabled = False
            i += 1
        else:
            i += 1

    tracker = Tracker(
        port=port,
        peer_trackers=peer_trackers,
        name=f"main:{port}",
        redis_url=redis_url,
        redis_enabled=redis_enabled,
    )
    print("━" * 50)
    print("  MDP2P Tracker — md:// resolution server")
    print(f"  Port: {port}")
    if peer_trackers:
        print(f"  Peer trackers: {peer_trackers}")
    print(f"  Redis: {'enabled' if redis_enabled else 'disabled'}")
    print("━" * 50)
    asyncio.run(tracker.serve_forever())