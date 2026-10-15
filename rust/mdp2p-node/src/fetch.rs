//! `md://<uri>` retrieval: resolve, pin, discover, download, verify, store.
//!
//! Every trust decision is delegated to [`mdp2p_core`]; this module only
//! moves bytes and enforces the "first valid seeder wins" policy (SPEC §5).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, bail, Context, Result};
use futures::stream::{FuturesUnordered, StreamExt};
use libp2p::{Multiaddr, PeerId};
use mdp2p_core::{
    check_downloaded_bundle, check_resolved_record, content_key, validate_uri, PinStatus,
};
use serde_json::{json, Value};

use crate::node::{Node, Protocol};
use crate::pinstore::PinStore;
use crate::store::SiteStore;

/// Bundles in flight at once (each can be ~100 MiB in memory).
const MAX_PARALLEL_SEEDERS: usize = 4;

pub struct FetchOptions {
    /// Bound on each seeder attempt (dial + download).
    pub seeder_timeout: Duration,
    /// Announce ourselves as a provider afterwards ("every visitor seeds").
    pub announce: bool,
}

impl Default for FetchOptions {
    fn default() -> Self {
        Self {
            seeder_timeout: Duration::from_secs(120),
            announce: false,
        }
    }
}

#[derive(Debug)]
pub struct FetchOutcome {
    pub uri: String,
    pub site_dir: std::path::PathBuf,
    pub seeder: PeerId,
    pub file_count: usize,
    pub first_visit: bool,
}

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Fetch `uri` through `naming` (the naming server, which in MDP2P also
/// hosts the DHT entry point) and store it in `store`.
pub async fn fetch_site(
    node: &Node,
    naming: (PeerId, Multiaddr),
    uri: &str,
    store: &SiteStore,
    pins: &PinStore,
    options: &FetchOptions,
) -> Result<FetchOutcome> {
    if !validate_uri(uri) {
        bail!("invalid site name {uri:?}");
    }
    let (naming_peer, naming_addr) = naming;
    node.add_peer(naming_peer, naming_addr.clone()).await?;

    let response = node
        .request(
            Protocol::Naming,
            naming_peer,
            vec![naming_addr],
            json!({"type": "resolve", "uri": uri}),
        )
        .await
        .context("naming resolve failed")?;
    if response.get("type").and_then(Value::as_str) == Some("error") {
        bail!(
            "naming server: {}",
            response["msg"].as_str().unwrap_or("error")
        );
    }
    let resolved = check_resolved_record(uri, &response)
        .map_err(|e| anyhow!("naming record rejected: {e:?}"))?;
    let author = response["record"]["author"]
        .as_str()
        .unwrap_or("unknown")
        .to_owned();

    let first_visit = match pins.check(uri, &resolved.public_key, resolved.timestamp)? {
        PinStatus::Unknown => true,
        PinStatus::Match => false,
        PinStatus::Mismatch => {
            bail!("ALERT: author key changed for '{uri}', possible impersonation")
        }
        PinStatus::Rollback => {
            bail!("ALERT: older record than already seen for '{uri}', possible rollback")
        }
    };

    let key = content_key(uri, &resolved.public_key);
    let providers: Vec<_> = node
        .get_providers(naming_peer, key.clone().into_bytes())
        .await
        .context("provider lookup failed")?
        .into_iter()
        .filter(|p| p.peer_id != node.peer_id())
        .collect();
    if providers.is_empty() {
        bail!("no seeder found for '{uri}'");
    }
    tracing::info!("{} provider(s) for {uri}", providers.len());

    // First valid bundle wins; dropping the stream cancels the others.
    let request = json!({"type": "get_bundle", "uri": uri});
    let mut queue = providers.into_iter();
    let mut in_flight = FuturesUnordered::new();
    let launch = |p: crate::codec::Provider| {
        let node = node.clone();
        let request = request.clone();
        let timeout = options.seeder_timeout;
        async move {
            let result = tokio::time::timeout(
                timeout,
                node.request(Protocol::Bundle, p.peer_id, p.addrs, request),
            )
            .await;
            (p.peer_id, result)
        }
    };
    for p in queue.by_ref().take(MAX_PARALLEL_SEEDERS) {
        in_flight.push(launch(p));
    }
    let mut winner = None;
    while let Some((peer, result)) = in_flight.next().await {
        match result {
            Err(_) => tracing::warn!("seeder {peer} timed out"),
            Ok(Err(e)) => tracing::warn!("seeder {peer} failed: {e:#}"),
            Ok(Ok(bundle)) => {
                match check_downloaded_bundle(
                    &bundle,
                    uri,
                    &resolved.public_key,
                    &resolved.manifest_ref,
                    now(),
                ) {
                    Ok(accepted) => {
                        winner = Some((peer, bundle, accepted));
                        break;
                    }
                    Err(reject) => {
                        tracing::warn!("discarding bundle from {peer}: {}", reject.code())
                    }
                }
            }
        }
        if let Some(p) = queue.next() {
            in_flight.push(launch(p));
        }
    }
    drop(in_flight);
    let (seeder, bundle, accepted) =
        winner.ok_or_else(|| anyhow!("no seeder returned a valid bundle for '{uri}'"))?;

    let signature = bundle["signature"].as_str().unwrap_or_default();
    let site_dir = store.write_site(uri, &bundle["manifest"], signature, &accepted)?;
    pins.record_visit(uri, &resolved.public_key, &author, resolved.timestamp)?;

    if options.announce {
        if let Err(e) = node.provide(key.into_bytes()).await {
            tracing::warn!("could not announce {uri}: {e:#}");
        }
    }
    Ok(FetchOutcome {
        uri: uri.to_owned(),
        site_dir,
        seeder,
        file_count: accepted.files.len(),
        first_visit,
    })
}

/// Announce every site in `store` in the DHT. Returns how many succeeded.
pub async fn announce_all(node: &Node, store: &SiteStore) -> usize {
    let mut ok = 0;
    for uri in store.uris() {
        let Some(manifest) = store.manifest(&uri) else {
            continue;
        };
        let Some(pk) = manifest.get("public_key").and_then(Value::as_str) else {
            continue;
        };
        match node.provide(content_key(&uri, pk).into_bytes()).await {
            Ok(()) => ok += 1,
            Err(e) => tracing::warn!("could not announce {uri}: {e:#}"),
        }
    }
    ok
}
