//! Deterministic reviewer selection (hash sortition).
//!
//! Anyone can recompute which reviewers a publisher had to contact for a
//! given content: rank the deduplicated pool by
//! `SHA-256(content_key || 0x00 || pubkey)` ascending and take the first `n`.

use sha2::{Digest, Sha256};

/// Return up to `n` reviewer keys from `pool`, in rank order.
pub fn select_reviewers(content_key: &str, pool: &[String], n: usize) -> Vec<String> {
    let mut unique: Vec<&String> = pool.iter().collect();
    unique.sort();
    unique.dedup();
    let mut ranked: Vec<([u8; 32], &String)> = unique
        .into_iter()
        .map(|pk| {
            let mut h = Sha256::new();
            h.update(content_key.as_bytes());
            h.update([0u8]);
            h.update(pk.as_bytes());
            (h.finalize().into(), pk)
        })
        .collect();
    ranked.sort();
    ranked
        .into_iter()
        .take(n)
        .map(|(_, pk)| pk.clone())
        .collect()
}
