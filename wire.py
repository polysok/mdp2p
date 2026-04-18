"""Length-prefixed JSON framing over libp2p streams.

Every message is: [4 bytes big-endian length][UTF-8 JSON payload].
`max_size` lets callers cap large transfers per protocol (small for
control messages, bigger for bundle transport).
"""

from __future__ import annotations

import json
import struct
from typing import Optional

from libp2p.network.stream.net_stream import INetStream


async def read_exact(stream: INetStream, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = await stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


async def send_framed_json(
    stream: INetStream, obj: dict, max_size: int
) -> None:
    payload = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > max_size:
        raise ValueError(f"message too large: {len(payload)} > {max_size}")
    await stream.write(struct.pack("!I", len(payload)) + payload)


async def recv_framed_json(
    stream: INetStream, max_size: int
) -> Optional[dict]:
    header = await read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("!I", header)
    if length > max_size:
        return None
    payload = await read_exact(stream, length)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))
