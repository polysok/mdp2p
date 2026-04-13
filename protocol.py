"""
MDP2P Protocol — Length-prefixed JSON messages over async TCP.

Every message is: [4 bytes big-endian length][JSON payload]
"""

import asyncio
import json
import logging
import re
import struct
import time
from collections import defaultdict
from typing import Dict, Optional

logger = logging.getLogger("mdp2p.protocol")

HEADER_SIZE = 4
MAX_MSG_SIZE = 10 * 1024 * 1024  # 10 MB

DEFAULT_RECV_TIMEOUT = 30  # seconds
CONNECT_TIMEOUT = 10  # seconds

MAX_URI_LENGTH = 255
_VALID_URI_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_uri(uri: str) -> str:
    """Validate and return a safe URI string.

    Accepted characters: alphanumeric, dots, hyphens, underscores.
    Rejects empty strings, path traversal, path separators, and excessive length.
    """
    if not uri or not isinstance(uri, str):
        raise ValueError("URI must be a non-empty string")
    if len(uri) > MAX_URI_LENGTH:
        raise ValueError(f"URI too long ({len(uri)} chars, max {MAX_URI_LENGTH})")
    if "/" in uri or "\\" in uri or "\x00" in uri:
        raise ValueError(f"URI contains forbidden characters: {uri!r}")
    if uri in (".", "..") or ".." in uri:
        raise ValueError(f"URI contains path traversal: {uri!r}")
    if not _VALID_URI_RE.match(uri):
        raise ValueError(
            f"URI contains invalid characters (allowed: a-z, 0-9, '.', '-', '_'): {uri!r}"
        )
    return uri


class RateLimiter:
    """Per-IP rate limiter using a sliding window."""

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: float = 60.0,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_id: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        timestamps = [t for t in self._requests.get(client_id, []) if t > cutoff]
        if len(timestamps) >= self.max_requests:
            self._requests[client_id] = timestamps
            return False
        timestamps.append(now)
        self._requests[client_id] = timestamps
        return True

    def cleanup(self) -> None:
        """Remove entries with no recent requests."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        stale = [
            cid for cid, ts in self._requests.items()
            if not any(t > cutoff for t in ts)
        ]
        for cid in stale:
            del self._requests[cid]


async def send_msg(writer: asyncio.StreamWriter, data: dict) -> None:
    """Send a JSON message with a length prefix."""
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


async def recv_msg(
    reader: asyncio.StreamReader,
    timeout: float = DEFAULT_RECV_TIMEOUT,
) -> Optional[dict]:
    """Receive a length-prefixed JSON message. Returns None if the connection is closed or timed out."""
    try:
        header = await asyncio.wait_for(
            reader.readexactly(HEADER_SIZE), timeout=timeout
        )
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    except asyncio.TimeoutError:
        logger.warning("Timeout waiting for message header")
        return None

    length = struct.unpack("!I", header)[0]
    if length > MAX_MSG_SIZE:
        logger.warning("Message too large: %d bytes", length)
        return None

    try:
        payload = await asyncio.wait_for(
            reader.readexactly(length), timeout=timeout
        )
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    except asyncio.TimeoutError:
        logger.warning("Timeout reading message payload")
        return None

    return json.loads(payload.decode("utf-8"))