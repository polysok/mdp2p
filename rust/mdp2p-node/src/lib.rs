//! # mdp2p-node
//!
//! An MDP2P peer on rust-libp2p that interoperates with the Python
//! reference: it resolves names on the naming server, finds seeders in the
//! DHT, downloads and verifies bundles with [`mdp2p_core`], and seeds the
//! sites it holds.

pub mod codec;
pub mod fetch;
pub mod node;
pub mod pinstore;
pub mod store;

pub use fetch::{announce_all, fetch_site, FetchOptions, FetchOutcome};
pub use node::{load_or_create_keypair, split_peer_addr, Node, NodeConfig, Protocol};
pub use pinstore::PinStore;
pub use store::SiteStore;
