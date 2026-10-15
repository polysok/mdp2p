//! Stream codecs for the protocols this node speaks.
//!
//! - [`JsonCodec`]: MDP2P framing, `[u32 big-endian length][UTF-8 JSON]`,
//!   one request and one response per stream (`/mdp2p/naming/1.0.0`,
//!   `/mdp2p/bundle/1.0.0`). Frames are read by length, never to EOF, so a
//!   peer that keeps its write side open (as py-libp2p does) still works.
//! - [`KadCodec`]: one Kademlia `GET_PROVIDERS` exchange on `/ipfs/kad/1.0.0`
//!   (unsigned-varint length + protobuf). rust-libp2p's Kademlia forgets a
//!   provider's addresses once its query ends, so asking the bootstrap peer
//!   directly (as the Python client does) gives dialable results.

use std::io;

use futures::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use libp2p::{request_response, Multiaddr, PeerId, StreamProtocol};
use serde_json::Value;

pub const NAMING_PROTOCOL: StreamProtocol = StreamProtocol::new("/mdp2p/naming/1.0.0");
pub const BUNDLE_PROTOCOL: StreamProtocol = StreamProtocol::new("/mdp2p/bundle/1.0.0");
pub const KAD_PROTOCOL: StreamProtocol = StreamProtocol::new("/ipfs/kad/1.0.0");

/// Requests on every MDP2P protocol fit in this (one signed record at most).
pub const MAX_REQUEST_SIZE: usize = 64 * 1024;
/// Naming-server responses (one page of a listing).
pub const MAX_NAMING_RESPONSE_SIZE: usize = 1024 * 1024;
/// Bundle responses (50 MiB of content plus JSON overhead).
pub const MAX_BUNDLE_RESPONSE_SIZE: usize = 100 * 1024 * 1024;
/// Kademlia messages (a page of provider records).
pub const MAX_KAD_MESSAGE_SIZE: usize = 1024 * 1024;

/// Length-prefixed JSON, with separate caps for requests and responses.
#[derive(Debug, Clone)]
pub struct JsonCodec {
    max_response: usize,
}

impl JsonCodec {
    pub fn new(max_response: usize) -> Self {
        Self { max_response }
    }
}

async fn read_frame<T: AsyncRead + Unpin + Send>(io: &mut T, max: usize) -> io::Result<Vec<u8>> {
    let mut header = [0u8; 4];
    io.read_exact(&mut header).await?;
    let len = u32::from_be_bytes(header) as usize;
    if len > max {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("frame of {len} bytes exceeds the {max} byte limit"),
        ));
    }
    let mut payload = vec![0u8; len];
    io.read_exact(&mut payload).await?;
    Ok(payload)
}

async fn write_frame<T: AsyncWrite + Unpin + Send>(io: &mut T, payload: &[u8]) -> io::Result<()> {
    let len = u32::try_from(payload.len())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "frame too large"))?;
    io.write_all(&len.to_be_bytes()).await?;
    // The muxer splits large writes; Noise frames stay under 64 KiB.
    io.write_all(payload).await?;
    io.flush().await
}

fn parse_json(bytes: &[u8]) -> io::Result<Value> {
    serde_json::from_slice(bytes).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

fn encode_json(value: &Value, max: usize) -> io::Result<Vec<u8>> {
    // Python's reference sends sorted, compact JSON; any valid JSON is
    // accepted on read, so plain compact serialization is enough here.
    let bytes = serde_json::to_vec(value).map_err(io::Error::other)?;
    if bytes.len() > max {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "message too large",
        ));
    }
    Ok(bytes)
}

impl request_response::Codec for JsonCodec {
    type Protocol = StreamProtocol;
    type Request = Value;
    type Response = Value;

    async fn read_request<T>(&mut self, _: &Self::Protocol, io: &mut T) -> io::Result<Value>
    where
        T: AsyncRead + Unpin + Send,
    {
        parse_json(&read_frame(io, MAX_REQUEST_SIZE).await?)
    }

    async fn read_response<T>(&mut self, _: &Self::Protocol, io: &mut T) -> io::Result<Value>
    where
        T: AsyncRead + Unpin + Send,
    {
        parse_json(&read_frame(io, self.max_response).await?)
    }

    async fn write_request<T>(
        &mut self,
        _: &Self::Protocol,
        io: &mut T,
        req: Value,
    ) -> io::Result<()>
    where
        T: AsyncWrite + Unpin + Send,
    {
        write_frame(io, &encode_json(&req, MAX_REQUEST_SIZE)?).await
    }

    async fn write_response<T>(
        &mut self,
        _: &Self::Protocol,
        io: &mut T,
        res: Value,
    ) -> io::Result<()>
    where
        T: AsyncWrite + Unpin + Send,
    {
        write_frame(io, &encode_json(&res, self.max_response)?).await
    }
}

// ─── Kademlia GET_PROVIDERS (protobuf, hand-encoded) ──────────────────

/// A provider as reported by a DHT peer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Provider {
    pub peer_id: PeerId,
    pub addrs: Vec<Multiaddr>,
}

/// `GET_PROVIDERS` request for `key` / list of providers in the response.
#[derive(Debug, Clone, Default)]
pub struct KadCodec;

const KAD_GET_PROVIDERS: u64 = 3;

fn put_varint(out: &mut Vec<u8>, mut v: u64) {
    loop {
        let byte = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(byte);
            return;
        }
        out.push(byte | 0x80);
    }
}

fn get_varint(buf: &[u8], pos: &mut usize) -> io::Result<u64> {
    let mut value = 0u64;
    for shift in (0..64).step_by(7) {
        let byte = *buf.get(*pos).ok_or_else(|| invalid("truncated varint"))?;
        *pos += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
    }
    Err(invalid("varint too long"))
}

fn invalid(msg: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.to_owned())
}

/// Iterate `(field number, wire type, payload)` over a protobuf message.
/// Varint fields yield an empty payload (their value is not needed here).
fn fields(buf: &[u8]) -> io::Result<Vec<(u64, &[u8])>> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < buf.len() {
        let tag = get_varint(buf, &mut pos)?;
        match tag & 7 {
            0 => {
                get_varint(buf, &mut pos)?;
                out.push((tag >> 3, &buf[0..0]));
            }
            2 => {
                let len = get_varint(buf, &mut pos)? as usize;
                let end = pos.checked_add(len).filter(|e| *e <= buf.len());
                let end = end.ok_or_else(|| invalid("truncated field"))?;
                out.push((tag >> 3, &buf[pos..end]));
                pos = end;
            }
            1 => pos += 8,
            5 => pos += 4,
            _ => return Err(invalid("unsupported wire type")),
        }
    }
    Ok(out)
}

/// Encode a `GET_PROVIDERS` message for `key` (the raw DHT key bytes).
pub fn encode_get_providers(key: &[u8]) -> Vec<u8> {
    let mut msg = Vec::with_capacity(key.len() + 8);
    put_varint(&mut msg, 1 << 3); // field 1 (type), varint
    put_varint(&mut msg, KAD_GET_PROVIDERS);
    put_varint(&mut msg, (2 << 3) | 2); // field 2 (key), bytes
    put_varint(&mut msg, key.len() as u64);
    msg.extend_from_slice(key);
    msg
}

/// Decode the `providerPeers` (field 9) of a Kademlia response. Peers or
/// addresses that do not parse are skipped rather than failing the lookup.
pub fn decode_providers(msg: &[u8]) -> io::Result<Vec<Provider>> {
    let mut providers = Vec::new();
    for (field, peer) in fields(msg)? {
        if field != 9 {
            continue;
        }
        let mut peer_id = None;
        let mut addrs = Vec::new();
        for (sub, value) in fields(peer)? {
            match sub {
                1 => peer_id = PeerId::from_bytes(value).ok(),
                2 => addrs.extend(Multiaddr::try_from(value.to_vec()).ok()),
                _ => {}
            }
        }
        if let Some(peer_id) = peer_id {
            providers.push(Provider { peer_id, addrs });
        }
    }
    Ok(providers)
}

async fn read_varint_frame<T: AsyncRead + Unpin + Send>(io: &mut T) -> io::Result<Vec<u8>> {
    let mut len = 0u64;
    for shift in (0..64).step_by(7) {
        let mut byte = [0u8; 1];
        io.read_exact(&mut byte).await?;
        len |= u64::from(byte[0] & 0x7f) << shift;
        if byte[0] & 0x80 == 0 {
            break;
        }
    }
    let len = len as usize;
    if len > MAX_KAD_MESSAGE_SIZE {
        return Err(invalid("kad message too large"));
    }
    let mut payload = vec![0u8; len];
    io.read_exact(&mut payload).await?;
    Ok(payload)
}

impl request_response::Codec for KadCodec {
    type Protocol = StreamProtocol;
    /// The raw DHT key.
    type Request = Vec<u8>;
    type Response = Vec<Provider>;

    async fn read_request<T>(&mut self, _: &Self::Protocol, _io: &mut T) -> io::Result<Vec<u8>>
    where
        T: AsyncRead + Unpin + Send,
    {
        // Outbound only: inbound /ipfs/kad/1.0.0 belongs to the Kademlia behaviour.
        Err(io::Error::new(io::ErrorKind::Unsupported, "outbound only"))
    }

    async fn read_response<T>(
        &mut self,
        _: &Self::Protocol,
        io: &mut T,
    ) -> io::Result<Vec<Provider>>
    where
        T: AsyncRead + Unpin + Send,
    {
        decode_providers(&read_varint_frame(io).await?)
    }

    async fn write_request<T>(
        &mut self,
        _: &Self::Protocol,
        io: &mut T,
        key: Vec<u8>,
    ) -> io::Result<()>
    where
        T: AsyncWrite + Unpin + Send,
    {
        let msg = encode_get_providers(&key);
        let mut frame = Vec::with_capacity(msg.len() + 4);
        put_varint(&mut frame, msg.len() as u64);
        frame.extend_from_slice(&msg);
        io.write_all(&frame).await?;
        io.flush().await
    }

    async fn write_response<T>(
        &mut self,
        _: &Self::Protocol,
        _io: &mut T,
        _res: Vec<Provider>,
    ) -> io::Result<()>
    where
        T: AsyncWrite + Unpin + Send,
    {
        Err(io::Error::new(io::ErrorKind::Unsupported, "outbound only"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn get_providers_round_trips_through_the_decoder_shape() {
        let msg = encode_get_providers(b"/mdp2p/abc");
        // type=3, key="/mdp2p/abc"
        assert_eq!(&msg[..4], &[0x08, 0x03, 0x12, 10]);
        assert_eq!(&msg[4..], b"/mdp2p/abc");
    }

    #[test]
    fn decodes_provider_peers_and_skips_garbage_addrs() {
        let peer = PeerId::random();
        let addr: Multiaddr = "/ip4/127.0.0.1/tcp/4001".parse().unwrap();
        let mut peer_msg = Vec::new();
        let id = peer.to_bytes();
        put_varint(&mut peer_msg, (1 << 3) | 2);
        put_varint(&mut peer_msg, id.len() as u64);
        peer_msg.extend_from_slice(&id);
        for raw in [addr.to_vec(), vec![0xff, 0xff]] {
            put_varint(&mut peer_msg, (2 << 3) | 2);
            put_varint(&mut peer_msg, raw.len() as u64);
            peer_msg.extend_from_slice(&raw);
        }
        let mut msg = vec![0x08, 0x03];
        put_varint(&mut msg, (9 << 3) | 2);
        put_varint(&mut msg, peer_msg.len() as u64);
        msg.extend_from_slice(&peer_msg);

        let providers = decode_providers(&msg).unwrap();
        assert_eq!(
            providers,
            vec![Provider {
                peer_id: peer,
                addrs: vec![addr]
            }]
        );
    }
}
