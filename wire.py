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

# The Noise transport rejects any single write above 65535 bytes, so large
# frames are written in slices that leave room for its framing overhead.
WRITE_CHUNK_SIZE = 32 * 1024


async def read_exact(stream: INetStream, n: int) -> Optional[bytes]:
    # bytearray keeps reassembly linear: `bytes += chunk` copies the whole
    # buffer on every chunk, which is quadratic on a 100 MiB bundle.
    buf = bytearray()
    while len(buf) < n:
        chunk = await stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


async def send_framed_json(
    stream: INetStream, obj: dict, max_size: int
) -> None:
    payload = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > max_size:
        raise ValueError(f"message too large: {len(payload)} > {max_size}")
    frame = memoryview(struct.pack("!I", len(payload)) + payload)
    for start in range(0, len(frame), WRITE_CHUNK_SIZE):
        await stream.write(bytes(frame[start : start + WRITE_CHUNK_SIZE]))


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
