//! `mdp2p-node` — fetch and seed md:// sites from the command line.
//!
//! ```text
//! mdp2p-node fetch --naming /ip4/…/tcp/1707/p2p/<id> blog.alice
//! mdp2p-node serve --naming /ip4/…/tcp/1707/p2p/<id> --listen /ip4/0.0.0.0/tcp/4001
//! ```
//!
//! Machine-readable lines on stdout (used by the interop tests):
//! `LISTEN <multiaddr>`, `FETCHED <uri> <dir> <files>`, `ANNOUNCED <n>`.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use libp2p::{identity, Multiaddr};
use mdp2p_node::{
    announce_all, fetch_site, load_or_create_keypair, split_peer_addr, FetchOptions, Node,
    NodeConfig, PinStore, SiteStore,
};

#[derive(Parser)]
#[command(version, about = "MDP2P peer (rust-libp2p)")]
struct Cli {
    /// Naming server / DHT bootstrap multiaddr, ending in /p2p/<peer id>.
    #[arg(long, global = true)]
    naming: Option<Multiaddr>,
    /// Where sites are stored and served from.
    #[arg(long, global = true, default_value = "./peer_data")]
    data_dir: PathBuf,
    /// TOFU pin store (Python-compatible).
    #[arg(long, global = true)]
    pinstore: Option<PathBuf>,
    /// 32-byte libp2p seed file (same format as the Python peer). Random if absent.
    #[arg(long, global = true)]
    key: Option<PathBuf>,
    /// Listen address(es).
    #[arg(long, global = true, default_value = "/ip4/0.0.0.0/tcp/0")]
    listen: Vec<Multiaddr>,
    /// Address(es) to advertise in DHT provider records. Defaults to the
    /// concrete listen addresses.
    #[arg(long, global = true)]
    external: Vec<Multiaddr>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Download and verify a site, then optionally keep seeding it.
    Fetch {
        uri: String,
        /// Announce and keep seeding after the download (Ctrl-C to stop).
        #[arg(long)]
        seed: bool,
    },
    /// Announce every site in --data-dir and serve them until stopped.
    Serve,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mdp2p_node=info".into()),
        )
        .with_writer(std::io::stderr)
        .init();
    let cli = Cli::parse();

    let keypair = match &cli.key {
        Some(path) => load_or_create_keypair(path)?,
        None => identity::Keypair::generate_ed25519(),
    };
    let store = Arc::new(SiteStore::open(&cli.data_dir)?);
    let seeding = matches!(
        cli.command,
        Command::Serve | Command::Fetch { seed: true, .. }
    );
    let node = Node::spawn(NodeConfig {
        keypair,
        store: store.clone(),
        dht_server: seeding,
        advertise_listen_addrs: cli.external.is_empty(),
    })?;
    for addr in &cli.listen {
        node.listen(addr.clone()).await?;
    }
    for addr in &cli.external {
        node.add_external_address(addr.clone()).await?;
    }
    for addr in node.listen_addrs().await? {
        println!("LISTEN {addr}");
    }

    let naming = cli
        .naming
        .as_ref()
        .context("--naming is required")
        .and_then(split_peer_addr)?;
    node.add_peer(naming.0, naming.1.clone()).await?;

    match cli.command {
        Command::Fetch { uri, seed } => {
            let pins = PinStore::new(cli.pinstore.unwrap_or_else(default_pinstore));
            let options = FetchOptions {
                announce: seed,
                ..FetchOptions::default()
            };
            let outcome = fetch_site(&node, naming, &uri, &store, &pins, &options).await?;
            println!(
                "FETCHED {} {} {}",
                outcome.uri,
                outcome.site_dir.display(),
                outcome.file_count
            );
            if seed {
                wait_forever().await;
            }
        }
        Command::Serve => {
            let n = announce_all(&node, &store).await;
            println!("ANNOUNCED {n}");
            // Provider records expire; re-announce periodically.
            let mut tick = tokio::time::interval(Duration::from_secs(3600));
            tick.tick().await;
            loop {
                tokio::select! {
                    _ = tick.tick() => { announce_all(&node, &store).await; }
                    _ = tokio::signal::ctrl_c() => break,
                }
            }
        }
    }
    Ok(())
}

fn default_pinstore() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
        .join(".mdp2p")
        .join("known_keys.json")
}

async fn wait_forever() {
    let _ = tokio::signal::ctrl_c().await;
}
