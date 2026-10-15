//! TOFU pin store, file-compatible with the Python client's
//! `~/.mdp2p/known_keys.json`:
//! `{uri: {public_key, author, first_seen, last_seen, record_timestamp?}}`.

use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use mdp2p_core::{check_pin, Pin, PinStatus};
use serde_json::{json, Map, Value};

pub struct PinStore {
    path: PathBuf,
}

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

impl PinStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// Read the whole store. A missing file is empty; an unreadable or
    /// malformed one is an error, so callers fail closed instead of
    /// silently re-trusting every site.
    fn load(&self) -> Result<Map<String, Value>> {
        if !self.path.exists() {
            return Ok(Map::new());
        }
        let raw = fs::read(&self.path)
            .with_context(|| format!("cannot read pinstore {}", self.path.display()))?;
        match serde_json::from_slice(&raw) {
            Ok(Value::Object(map)) => Ok(map),
            Ok(_) => anyhow::bail!("pinstore {} is not a JSON object", self.path.display()),
            Err(e) => Err(e).with_context(|| format!("corrupted pinstore {}", self.path.display())),
        }
    }

    fn save(&self, store: &Map<String, Value>) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp = self
            .path
            .with_extension(format!("json.tmp-{}", std::process::id()));
        fs::write(&tmp, serde_json::to_vec_pretty(store)?)?;
        fs::rename(&tmp, &self.path)?;
        Ok(())
    }

    /// Check a resolved author key and record timestamp for `uri`.
    pub fn check(&self, uri: &str, public_key: &str, record_timestamp: i64) -> Result<PinStatus> {
        let store = self.load()?;
        let pin = store.get(uri).and_then(|entry| {
            Some(Pin {
                public_key: entry.get("public_key")?.as_str()?.to_owned(),
                record_timestamp: entry.get("record_timestamp").and_then(Value::as_i64),
            })
        });
        Ok(check_pin(pin.as_ref(), public_key, record_timestamp))
    }

    /// Record a successful visit: pin on first sight, otherwise refresh
    /// `last_seen` and raise the record-timestamp high-water mark.
    pub fn record_visit(
        &self,
        uri: &str,
        public_key: &str,
        author: &str,
        record_timestamp: i64,
    ) -> Result<()> {
        let mut store = self.load()?;
        let t = now();
        let entry = match store.get(uri) {
            Some(existing) => {
                let mut e = existing.clone();
                let seen = e
                    .get("record_timestamp")
                    .and_then(Value::as_i64)
                    .unwrap_or(0);
                e["last_seen"] = json!(t);
                e["record_timestamp"] = json!(seen.max(record_timestamp));
                e
            }
            None => json!({
                "public_key": public_key,
                "author": author,
                "first_seen": t,
                "last_seen": t,
                "record_timestamp": record_timestamp,
            }),
        };
        store.insert(uri.to_owned(), entry);
        self.save(&store)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pins_on_first_visit_then_detects_key_change_and_rollback() {
        let dir = tempfile::tempdir().unwrap();
        let pins = PinStore::new(dir.path().join("known_keys.json"));
        assert_eq!(pins.check("blog", "K", 100).unwrap(), PinStatus::Unknown);
        pins.record_visit("blog", "K", "alice", 100).unwrap();
        assert_eq!(pins.check("blog", "K", 100).unwrap(), PinStatus::Match);
        assert_eq!(pins.check("blog", "K", 99).unwrap(), PinStatus::Rollback);
        assert_eq!(
            pins.check("blog", "OTHER", 200).unwrap(),
            PinStatus::Mismatch
        );

        // The high-water mark only goes up.
        pins.record_visit("blog", "K", "alice", 50).unwrap();
        assert_eq!(pins.check("blog", "K", 99).unwrap(), PinStatus::Rollback);
    }

    #[test]
    fn corrupted_store_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("known_keys.json");
        fs::write(&path, b"{not json").unwrap();
        assert!(PinStore::new(&path).check("blog", "K", 1).is_err());
    }
}
