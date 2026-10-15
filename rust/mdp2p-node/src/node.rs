//! The libp2p node: one swarm driven by a background task, controlled
//! through a cloneable [`Node`] handle.
//!
//! Stack (same as py-libp2p's defaults, so both interoperate): TCP, Noise,
//! Yamux, Identify, Ping, Kademlia on `/ipfs/kad/1.0.0`, and request/response
//! protocols for naming, bundles and direct `GET_PROVIDERS` lookups.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use futures::StreamExt;
use libp2p::kad::{self, store::MemoryStore, Mode, QueryResult};
use libp2p::request_response::{self, OutboundRequestId, ProtocolSupport};
use libp2p::swarm::{NetworkBehaviour, SwarmEvent};
use libp2p::{identify, identity, noise, ping, tcp, yamux, Multiaddr, PeerId, Swarm};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};

use crate::codec::{
    JsonCodec, KadCodec, Provider, BUNDLE_PROTOCOL, KAD_PROTOCOL, MAX_BUNDLE_RESPONSE_SIZE,
    MAX_NAMING_RESPONSE_SIZE, NAMING_PROTOCOL,
};
use crate::store::SiteStore;

const NAMING_TIMEOUT: Duration = Duration::from_secs(20);
const BUNDLE_TIMEOUT: Duration = Duration::from_secs(120);
const IDLE_CONNECTION_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(NetworkBehaviour)]
struct Behaviour {
    kad: kad::Behaviour<MemoryStore>,
    identify: identify::Behaviour,
    ping: ping::Behaviour,
    naming: request_response::Behaviour<JsonCodec>,
    bundle: request_response::Behaviour<JsonCodec>,
    providers: request_response::Behaviour<KadCodec>,
}

/// Which JSON protocol a request goes to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Protocol {
    Naming,
    Bundle,
}

type Reply<T> = oneshot::Sender<Result<T>>;

enum Command {
    Listen(Multiaddr, Reply<()>),
    AddExternalAddress(Multiaddr),
    AddPeer(PeerId, Multiaddr),
    Request(Protocol, PeerId, Vec<Multiaddr>, Value, Reply<Value>),
    GetProviders(PeerId, Vec<u8>, Reply<Vec<Provider>>),
    Provide(Vec<u8>, Reply<()>),
    ListenAddrs(oneshot::Sender<Vec<Multiaddr>>),
}

/// Cheap, cloneable handle to the running node.
#[derive(Clone)]
pub struct Node {
    peer_id: PeerId,
    commands: mpsc::Sender<Command>,
}

/// Startup options.
pub struct NodeConfig {
    /// libp2p identity. Use [`identity::Keypair::ed25519_from_bytes`] with
    /// the Python peer's 32-byte seed to keep the same PeerID.
    pub keypair: identity::Keypair,
    /// Sites to serve on `/mdp2p/bundle/1.0.0`.
    pub store: Arc<SiteStore>,
    /// Answer DHT queries (server mode) instead of only asking (client).
    pub dht_server: bool,
    /// Advertise every concrete listen address as external (in identify
    /// and DHT provider records). rust-libp2p otherwise announces only
    /// confirmed external addresses, and a provider record without
    /// addresses is useless to readers. Turn off when passing explicit
    /// public addresses with [`Node::add_external_address`].
    pub advertise_listen_addrs: bool,
}

impl Node {
    /// Build the swarm and spawn its event loop on the current tokio runtime.
    pub fn spawn(config: NodeConfig) -> Result<Self> {
        let peer_id = config.keypair.public().to_peer_id();
        let mut swarm = libp2p::SwarmBuilder::with_existing_identity(config.keypair)
            .with_tokio()
            .with_tcp(
                tcp::Config::default().nodelay(true),
                noise::Config::new,
                yamux::Config::default,
            )?
            .with_behaviour(|key| {
                let local = key.public().to_peer_id();
                let kad_config = kad::Config::new(KAD_PROTOCOL);
                Behaviour {
                    kad: kad::Behaviour::with_config(local, MemoryStore::new(local), kad_config),
                    identify: identify::Behaviour::new(identify::Config::new(
                        "/mdp2p/1.0.0".into(),
                        key.public(),
                    )),
                    ping: ping::Behaviour::default(),
                    naming: request_response::Behaviour::with_codec(
                        JsonCodec::new(MAX_NAMING_RESPONSE_SIZE),
                        [(NAMING_PROTOCOL, ProtocolSupport::Outbound)],
                        request_response::Config::default().with_request_timeout(NAMING_TIMEOUT),
                    ),
                    bundle: request_response::Behaviour::with_codec(
                        JsonCodec::new(MAX_BUNDLE_RESPONSE_SIZE),
                        [(BUNDLE_PROTOCOL, ProtocolSupport::Full)],
                        request_response::Config::default().with_request_timeout(BUNDLE_TIMEOUT),
                    ),
                    providers: request_response::Behaviour::with_codec(
                        KadCodec,
                        [(KAD_PROTOCOL, ProtocolSupport::Outbound)],
                        request_response::Config::default().with_request_timeout(NAMING_TIMEOUT),
                    ),
                }
            })?
            .with_swarm_config(|c| c.with_idle_connection_timeout(IDLE_CONNECTION_TIMEOUT))
            .build();
        swarm
            .behaviour_mut()
            .kad
            .set_mode(Some(if config.dht_server {
                Mode::Server
            } else {
                Mode::Client
            }));

        let (tx, rx) = mpsc::channel(64);
        let mut event_loop = EventLoop::new(swarm, rx, config.store);
        event_loop.advertise_listen_addrs = config.advertise_listen_addrs;
        tokio::spawn(event_loop.run());
        Ok(Self {
            peer_id,
            commands: tx,
        })
    }

    pub fn peer_id(&self) -> PeerId {
        self.peer_id
    }

    async fn call<T>(&self, make: impl FnOnce(Reply<T>) -> Command) -> Result<T> {
        let (tx, rx) = oneshot::channel();
        self.commands
            .send(make(tx))
            .await
            .map_err(|_| anyhow!("node stopped"))?;
        rx.await.map_err(|_| anyhow!("node stopped"))?
    }

    pub async fn listen(&self, addr: Multiaddr) -> Result<()> {
        self.call(|r| Command::Listen(addr, r)).await
    }

    /// Advertise `addr` in identify and in DHT provider records.
    pub async fn add_external_address(&self, addr: Multiaddr) -> Result<()> {
        self.commands
            .send(Command::AddExternalAddress(addr))
            .await
            .map_err(|_| anyhow!("node stopped"))
    }

    /// Make `peer` known at `addr` (dialing and DHT routing table).
    pub async fn add_peer(&self, peer: PeerId, addr: Multiaddr) -> Result<()> {
        self.commands
            .send(Command::AddPeer(peer, addr))
            .await
            .map_err(|_| anyhow!("node stopped"))
    }

    /// Send one JSON request and wait for the response.
    pub async fn request(
        &self,
        protocol: Protocol,
        peer: PeerId,
        addrs: Vec<Multiaddr>,
        request: Value,
    ) -> Result<Value> {
        self.call(|r| Command::Request(protocol, peer, addrs, request, r))
            .await
    }

    /// Ask `peer` (normally the bootstrap / naming peer) who provides `key`.
    pub async fn get_providers(&self, peer: PeerId, key: Vec<u8>) -> Result<Vec<Provider>> {
        self.call(|r| Command::GetProviders(peer, key, r)).await
    }

    /// Announce this node as a provider of `key` in the DHT.
    pub async fn provide(&self, key: Vec<u8>) -> Result<()> {
        self.call(|r| Command::Provide(key, r)).await
    }

    /// Full multiaddrs (with `/p2p/<id>`) this node listens on.
    pub async fn listen_addrs(&self) -> Result<Vec<Multiaddr>> {
        let (tx, rx) = oneshot::channel();
        self.commands
            .send(Command::ListenAddrs(tx))
            .await
            .map_err(|_| anyhow!("node stopped"))?;
        let addrs = rx.await.map_err(|_| anyhow!("node stopped"))?;
        Ok(addrs
            .into_iter()
            .map(|a| a.with(libp2p::multiaddr::Protocol::P2p(self.peer_id)))
            .collect())
    }
}

/// Split `/…/p2p/<id>` into the peer id and the transport address.
pub fn split_peer_addr(addr: &Multiaddr) -> Result<(PeerId, Multiaddr)> {
    let mut transport = addr.clone();
    match transport.pop() {
        Some(libp2p::multiaddr::Protocol::P2p(peer)) => Ok((peer, transport)),
        _ => Err(anyhow!("{addr} has no /p2p/<peer id> suffix")),
    }
}

struct EventLoop {
    swarm: Swarm<Behaviour>,
    commands: mpsc::Receiver<Command>,
    store: Arc<SiteStore>,
    // Request ids are per behaviour (each counter starts at 1), so the
    // protocol is part of the key.
    pending_json: HashMap<(Protocol, OutboundRequestId), Reply<Value>>,
    pending_providers: HashMap<OutboundRequestId, Reply<Vec<Provider>>>,
    pending_provide: HashMap<kad::QueryId, Reply<()>>,
    pending_listen: Vec<Reply<()>>,
    responses: mpsc::Receiver<(request_response::ResponseChannel<Value>, Value)>,
    responses_tx: mpsc::Sender<(request_response::ResponseChannel<Value>, Value)>,
    advertise_listen_addrs: bool,
}

impl EventLoop {
    fn new(
        swarm: Swarm<Behaviour>,
        commands: mpsc::Receiver<Command>,
        store: Arc<SiteStore>,
    ) -> Self {
        let (responses_tx, responses) = mpsc::channel(16);
        Self {
            swarm,
            commands,
            store,
            pending_json: HashMap::new(),
            pending_providers: HashMap::new(),
            pending_provide: HashMap::new(),
            pending_listen: Vec::new(),
            responses,
            responses_tx,
            advertise_listen_addrs: false,
        }
    }

    async fn run(mut self) {
        loop {
            tokio::select! {
                event = self.swarm.select_next_some() => self.on_swarm_event(event),
                Some((channel, response)) = self.responses.recv() => {
                    if self.swarm.behaviour_mut().bundle.send_response(channel, response).is_err() {
                        tracing::debug!("bundle requester went away before the response");
                    }
                }
                command = self.commands.recv() => match command {
                    Some(c) => self.on_command(c),
                    None => return,
                },
            }
        }
    }

    fn on_command(&mut self, command: Command) {
        let behaviour = self.swarm.behaviour_mut();
        match command {
            Command::Listen(addr, reply) => match self.swarm.listen_on(addr) {
                Ok(_) => self.pending_listen.push(reply),
                Err(e) => {
                    let _ = reply.send(Err(e.into()));
                }
            },
            Command::AddExternalAddress(addr) => self.swarm.add_external_address(addr),
            Command::AddPeer(peer, addr) => {
                behaviour.kad.add_address(&peer, addr);
            }
            Command::Request(protocol, peer, addrs, request, reply) => {
                let rr = match protocol {
                    Protocol::Naming => &mut behaviour.naming,
                    Protocol::Bundle => &mut behaviour.bundle,
                };
                let id = rr.send_request_with_addresses(&peer, request, addrs);
                self.pending_json.insert((protocol, id), reply);
            }
            Command::GetProviders(peer, key, reply) => {
                let id = behaviour.providers.send_request(&peer, key);
                self.pending_providers.insert(id, reply);
            }
            Command::Provide(key, reply) => {
                match behaviour.kad.start_providing(kad::RecordKey::new(&key)) {
                    Ok(id) => {
                        self.pending_provide.insert(id, reply);
                    }
                    Err(e) => {
                        let _ = reply.send(Err(anyhow!("cannot provide: {e:?}")));
                    }
                }
            }
            Command::ListenAddrs(reply) => {
                let _ = reply.send(self.swarm.listeners().cloned().collect());
            }
        }
    }

    fn on_swarm_event(&mut self, event: SwarmEvent<BehaviourEvent>) {
        match event {
            SwarmEvent::NewListenAddr { address, .. } => {
                tracing::info!("listening on {address}");
                if self.advertise_listen_addrs {
                    self.swarm.add_external_address(address);
                }
                for reply in self.pending_listen.drain(..) {
                    let _ = reply.send(Ok(()));
                }
            }
            SwarmEvent::Behaviour(BehaviourEvent::Naming(e)) => {
                self.on_json_event(Protocol::Naming, e)
            }
            SwarmEvent::Behaviour(BehaviourEvent::Bundle(e)) => {
                self.on_json_event(Protocol::Bundle, e)
            }
            SwarmEvent::Behaviour(BehaviourEvent::Providers(e)) => self.on_providers_event(e),
            SwarmEvent::Behaviour(BehaviourEvent::Kad(kad::Event::OutboundQueryProgressed {
                id,
                result: QueryResult::StartProviding(result),
                ..
            })) => {
                if let Some(reply) = self.pending_provide.remove(&id) {
                    let _ = reply.send(
                        result
                            .map(|_| ())
                            .map_err(|e| anyhow!("provide failed: {e:?}")),
                    );
                }
            }
            SwarmEvent::OutgoingConnectionError { peer_id, error, .. } => {
                tracing::debug!("dial to {peer_id:?} failed: {error}");
            }
            _ => {}
        }
    }

    fn on_json_event(&mut self, protocol: Protocol, event: request_response::Event<Value, Value>) {
        match event {
            request_response::Event::Message { message, peer, .. } => match message {
                request_response::Message::Request {
                    request, channel, ..
                } if protocol == Protocol::Bundle => {
                    // Disk reads happen off the event loop.
                    let store = self.store.clone();
                    let tx = self.responses_tx.clone();
                    tracing::debug!("bundle request from {peer}");
                    tokio::task::spawn_blocking(move || {
                        let response = store.handle_request(&request);
                        let _ = tx.blocking_send((channel, response));
                    });
                }
                // Naming is outbound-only: no inbound request can arrive there.
                request_response::Message::Request { .. } => {}
                request_response::Message::Response {
                    request_id,
                    response,
                } => {
                    if let Some(reply) = self.pending_json.remove(&(protocol, request_id)) {
                        let _ = reply.send(Ok(response));
                    }
                }
            },
            request_response::Event::OutboundFailure {
                request_id,
                error,
                peer,
                ..
            } => {
                if let Some(reply) = self.pending_json.remove(&(protocol, request_id)) {
                    let _ = reply.send(Err(anyhow!("request to {peer} failed: {error}")));
                }
            }
            request_response::Event::InboundFailure { peer, error, .. } => {
                tracing::debug!("inbound request from {peer} failed: {error}");
            }
            request_response::Event::ResponseSent { .. } => {}
        }
    }

    fn on_providers_event(&mut self, event: request_response::Event<Vec<u8>, Vec<Provider>>) {
        match event {
            request_response::Event::Message {
                message:
                    request_response::Message::Response {
                        request_id,
                        response,
                    },
                ..
            } => {
                if let Some(reply) = self.pending_providers.remove(&request_id) {
                    let _ = reply.send(Ok(response));
                }
            }
            request_response::Event::OutboundFailure {
                request_id,
                error,
                peer,
                ..
            } => {
                if let Some(reply) = self.pending_providers.remove(&request_id) {
                    let _ = reply.send(Err(anyhow!("GET_PROVIDERS to {peer} failed: {error}")));
                }
            }
            _ => {}
        }
    }
}

/// Load a Python-compatible 32-byte peer seed, or create one (mode 0600).
pub fn load_or_create_keypair(path: &std::path::Path) -> Result<identity::Keypair> {
    let seed = if path.exists() {
        let raw = std::fs::read(path).with_context(|| format!("cannot read {}", path.display()))?;
        if raw.len() != 32 {
            anyhow::bail!("invalid peer key at {}: expected 32 bytes", path.display());
        }
        raw
    } else {
        use std::io::Write;
        let seed: [u8; 32] = rand_seed();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        options.open(path)?.write_all(&seed)?;
        seed.to_vec()
    };
    Ok(identity::Keypair::ed25519_from_bytes(seed)?)
}

fn rand_seed() -> [u8; 32] {
    // A fresh ed25519 keypair is 32 random bytes; reuse libp2p's RNG.
    let kp = identity::Keypair::generate_ed25519();
    let ed = kp.try_into_ed25519().expect("ed25519 keypair");
    let mut seed = [0u8; 32];
    seed.copy_from_slice(&ed.secret().as_ref()[..32]);
    seed
}
