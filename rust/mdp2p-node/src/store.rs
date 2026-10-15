//! On-disk site storage, layout-compatible with the Python peer:
//! `<data_dir>/<uri>/manifest.json`, `manifest.sig` and the `.md` files.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::RwLock;

use anyhow::{bail, Context, Result};
use mdp2p_core::{validate_path, validate_uri, AcceptedBundle};
use serde_json::{json, Map, Value};

/// The sites this node holds and serves, keyed by uri.
#[derive(Debug)]
pub struct SiteStore {
    data_dir: PathBuf,
    sites: RwLock<BTreeMap<String, PathBuf>>,
}

impl SiteStore {
    /// Open `data_dir` and index the sites already in it.
    pub fn open(data_dir: impl Into<PathBuf>) -> Result<Self> {
        let data_dir = data_dir.into();
        fs::create_dir_all(&data_dir)
            .with_context(|| format!("cannot create {}", data_dir.display()))?;
        let store = Self {
            data_dir,
            sites: RwLock::new(BTreeMap::new()),
        };
        store.rescan()?;
        Ok(store)
    }

    /// Re-index `data_dir`. Dot-prefixed dirs are staging leftovers.
    pub fn rescan(&self) -> Result<()> {
        let mut found = BTreeMap::new();
        for entry in fs::read_dir(&self.data_dir)? {
            let path = entry?.path();
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !path.is_dir() || name.starts_with('.') {
                continue;
            }
            let Ok((manifest, _)) = load_manifest(&path) else {
                continue;
            };
            if let Some(uri) = manifest.get("uri").and_then(Value::as_str) {
                if validate_uri(uri) {
                    found.insert(uri.to_owned(), path);
                }
            }
        }
        *self.sites.write().expect("site index poisoned") = found;
        Ok(())
    }

    pub fn uris(&self) -> Vec<String> {
        self.sites
            .read()
            .expect("site index poisoned")
            .keys()
            .cloned()
            .collect()
    }

    pub fn site_dir(&self, uri: &str) -> Option<PathBuf> {
        self.sites
            .read()
            .expect("site index poisoned")
            .get(uri)
            .cloned()
    }

    /// The manifest of a site this node holds.
    pub fn manifest(&self, uri: &str) -> Option<Value> {
        load_manifest(&self.site_dir(uri)?).ok().map(|(m, _)| m)
    }

    /// Answer a `/mdp2p/bundle/1.0.0` request.
    pub fn handle_request(&self, request: &Value) -> Value {
        let uri = request.get("uri").and_then(Value::as_str).unwrap_or("");
        let Some(dir) = self.site_dir(uri) else {
            return error(&format!("site '{uri}' not found"));
        };
        let result = match request.get("type").and_then(Value::as_str) {
            Some("get_bundle") => bundle_response(&dir),
            Some("get_manifest") => load_manifest(&dir).map(|(manifest, signature)| {
                json!({"type": "manifest", "manifest": manifest, "signature": signature})
            }),
            other => return error(&format!("unknown type: {}", other.unwrap_or(""))),
        };
        result.unwrap_or_else(|e| {
            tracing::warn!("cannot serve {uri}: {e:#}");
            error("internal error")
        })
    }

    /// Write an accepted bundle as `<data_dir>/<uri>`, atomically: the files
    /// go to a staging dir that replaces the previous copy by rename.
    pub fn write_site(
        &self,
        uri: &str,
        manifest: &Value,
        signature: &str,
        bundle: &AcceptedBundle,
    ) -> Result<PathBuf> {
        if !validate_uri(uri) {
            bail!("invalid uri {uri:?}");
        }
        let target = self.data_dir.join(uri);
        let staging = self
            .data_dir
            .join(format!(".{uri}.staging-{}", std::process::id()));
        if staging.exists() {
            fs::remove_dir_all(&staging)?;
        }
        fs::create_dir_all(&staging)?;
        let written = (|| -> Result<()> {
            for (path, raw) in &bundle.files {
                // check_downloaded_bundle already enforced this; re-check so
                // this function stays safe on its own.
                if !validate_path(path) {
                    bail!("invalid path {path:?}");
                }
                let file = path.split('/').fold(staging.clone(), |p, seg| p.join(seg));
                if let Some(parent) = file.parent() {
                    fs::create_dir_all(parent)?;
                }
                fs::write(&file, raw)?;
            }
            fs::write(
                staging.join("manifest.json"),
                serde_json::to_vec_pretty(manifest)?,
            )?;
            fs::write(staging.join("manifest.sig"), signature)?;
            swap_into_place(&staging, &target)
        })();
        if let Err(e) = written {
            let _ = fs::remove_dir_all(&staging);
            return Err(e);
        }
        self.sites
            .write()
            .expect("site index poisoned")
            .insert(uri.to_owned(), target.clone());
        Ok(target)
    }
}

fn error(msg: &str) -> Value {
    json!({"type": "error", "msg": msg})
}

fn load_manifest(dir: &Path) -> Result<(Value, String)> {
    let manifest: Value = serde_json::from_slice(&fs::read(dir.join("manifest.json"))?)?;
    let signature = fs::read_to_string(dir.join("manifest.sig"))?
        .trim()
        .to_owned();
    Ok((manifest, signature))
}

fn bundle_response(dir: &Path) -> Result<Value> {
    let (manifest, signature) = load_manifest(dir)?;
    let mut files = Map::new();
    let entries = manifest
        .get("files")
        .and_then(Value::as_array)
        .context("manifest has no files")?;
    for entry in entries {
        let path = entry
            .get("path")
            .and_then(Value::as_str)
            .context("file entry has no path")?;
        if !validate_path(path) {
            bail!("refusing to serve invalid path {path:?}");
        }
        let file = path
            .split('/')
            .fold(dir.to_path_buf(), |p, seg| p.join(seg));
        // Exact bytes, no newline translation (hashes are over raw bytes).
        let content = String::from_utf8(fs::read(&file)?).context("file is not UTF-8")?;
        files.insert(path.to_owned(), Value::String(content));
    }
    Ok(json!({"type": "bundle", "manifest": manifest, "signature": signature, "files": files}))
}

fn swap_into_place(staging: &Path, target: &Path) -> Result<()> {
    if !target.exists() {
        fs::rename(staging, target)?;
        return Ok(());
    }
    let name = target
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("site");
    let backup = target.with_file_name(format!(".{name}.old-{}", std::process::id()));
    if backup.exists() {
        fs::remove_dir_all(&backup)?;
    }
    fs::rename(target, &backup)?;
    if let Err(e) = fs::rename(staging, target) {
        fs::rename(&backup, target)?;
        return Err(e.into());
    }
    let _ = fs::remove_dir_all(&backup);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn accepted(files: &[(&str, &str)]) -> AcceptedBundle {
        AcceptedBundle {
            files: files
                .iter()
                .map(|(p, c)| (p.to_string(), c.as_bytes().to_vec()))
                .collect(),
        }
    }

    fn manifest(uri: &str, paths: &[&str]) -> Value {
        let files: Vec<Value> = paths.iter().map(|p| json!({"path": p})).collect();
        json!({"uri": uri, "files": files, "public_key": "pk"})
    }

    #[test]
    fn written_site_is_served_byte_for_byte_and_reindexed() {
        let dir = tempfile::tempdir().unwrap();
        let store = SiteStore::open(dir.path()).unwrap();
        let files = [("index.md", "# Hi\r\nCRLF\r\n"), ("posts/a.md", "é")];
        store
            .write_site(
                "blog",
                &manifest("blog", &["index.md", "posts/a.md"]),
                "sig",
                &accepted(&files),
            )
            .unwrap();

        let response = store.handle_request(&json!({"type": "get_bundle", "uri": "blog"}));
        assert_eq!(response["type"], "bundle");
        assert_eq!(response["signature"], "sig");
        assert_eq!(response["files"]["index.md"], "# Hi\r\nCRLF\r\n");
        assert_eq!(response["files"]["posts/a.md"], "é");

        let reopened = SiteStore::open(dir.path()).unwrap();
        assert_eq!(reopened.uris(), vec!["blog".to_string()]);
    }

    #[test]
    fn update_replaces_previous_copy_without_leftovers() {
        let dir = tempfile::tempdir().unwrap();
        let store = SiteStore::open(dir.path()).unwrap();
        store
            .write_site(
                "blog",
                &manifest("blog", &["old.md"]),
                "s1",
                &accepted(&[("old.md", "v1")]),
            )
            .unwrap();
        store
            .write_site(
                "blog",
                &manifest("blog", &["new.md"]),
                "s2",
                &accepted(&[("new.md", "v2")]),
            )
            .unwrap();
        let site = dir.path().join("blog");
        assert!(!site.join("old.md").exists());
        assert_eq!(fs::read_to_string(site.join("new.md")).unwrap(), "v2");
        let hidden: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().starts_with('.'))
            .collect();
        assert!(hidden.is_empty(), "staging or backup dir left behind");
    }

    #[test]
    fn refuses_unknown_sites_and_invalid_names() {
        let dir = tempfile::tempdir().unwrap();
        let store = SiteStore::open(dir.path()).unwrap();
        let response = store.handle_request(&json!({"type": "get_bundle", "uri": "nope"}));
        assert_eq!(response["type"], "error");
        assert!(store
            .write_site("../evil", &manifest("x", &[]), "s", &accepted(&[]))
            .is_err());
    }
}
