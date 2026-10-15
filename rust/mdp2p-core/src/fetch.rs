//! Client-side acceptance rules for a resolved name and a downloaded bundle.
//!
//! Everything here is pure: it decides whether to trust bytes that came
//! from an untrusted naming server or seeder, and never touches the disk
//! or the network. A client calls [`check_resolved_record`] on the naming
//! answer, then [`check_downloaded_bundle`] on each seeder's response, and
//! keeps the first bundle accepted.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::paths::validate_path;
use crate::records::{
    is_manifest_expired, manifest_ref, verify_manifest, verify_name_record, RecordError,
};

/// Most files a bundle may hold.
pub const MAX_BUNDLE_FILES: usize = 1000;
/// Largest total size of a bundle's files, in bytes of UTF-8.
pub const MAX_BUNDLE_TOTAL_SIZE: usize = 50 * 1024 * 1024;

/// Why a naming answer was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolveError {
    /// The response is not `{"type": "record", "record": {...}, "signature": "..."}`.
    Malformed,
    /// The record is not validly signed by its embedded key.
    Record(RecordError),
    /// The record is signed, but for another name than the one requested.
    UriMismatch,
}

/// What a valid naming answer commits the client to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedName {
    /// Author key every manifest for this name must be signed with.
    pub public_key: String,
    /// `manifest_ref` the downloaded manifest must hash to.
    pub manifest_ref: String,
    /// Record timestamp, for rollback protection (see [`crate::pin`]).
    pub timestamp: i64,
}

/// Validate a naming server's answer to `resolve(uri)`.
///
/// The clock check is skipped: the server enforced freshness when it
/// accepted the record, and a stored record legitimately ages.
pub fn check_resolved_record(uri: &str, response: &Value) -> Result<ResolvedName, ResolveError> {
    if response.get("type").and_then(Value::as_str) != Some("record") {
        return Err(ResolveError::Malformed);
    }
    let (Some(record), Some(signature)) = (
        response.get("record"),
        response.get("signature").and_then(Value::as_str),
    ) else {
        return Err(ResolveError::Malformed);
    };
    verify_name_record(record, signature, None, 0).map_err(ResolveError::Record)?;
    if record.get("uri").and_then(Value::as_str) != Some(uri) {
        return Err(ResolveError::UriMismatch);
    }
    let field = |name| record.get(name).and_then(Value::as_str).map(str::to_owned);
    match (
        field("public_key"),
        field("manifest_ref"),
        record["timestamp"].as_i64(),
    ) {
        (Some(public_key), Some(manifest_ref), Some(timestamp)) => Ok(ResolvedName {
            public_key,
            manifest_ref,
            timestamp,
        }),
        _ => Err(ResolveError::Malformed),
    }
}

/// Why a seeder's bundle was refused. The `code()` strings are the stable
/// reason codes used by the conformance vectors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BundleReject {
    Malformed,
    BadSignature,
    UriMismatch,
    RefMismatch,
    Expired,
    TooManyFiles,
    BadPath(String),
    TooLarge,
    FilesMismatch,
    NotMarkdown(String),
    Corrupted(String),
}

impl BundleReject {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Malformed => "malformed",
            Self::BadSignature => "bad_signature",
            Self::UriMismatch => "uri_mismatch",
            Self::RefMismatch => "ref_mismatch",
            Self::Expired => "expired",
            Self::TooManyFiles => "too_many_files",
            Self::BadPath(_) => "bad_path",
            Self::TooLarge => "too_large",
            Self::FilesMismatch => "files_mismatch",
            Self::NotMarkdown(_) => "not_markdown",
            Self::Corrupted(_) => "corrupted",
        }
    }
}

/// A bundle that passed every check: the files to write, as raw bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedBundle {
    pub files: Vec<(String, Vec<u8>)>,
}

/// Decide whether a seeder's `{"manifest", "signature", "files"}` response
/// is the content the naming record committed to.
///
/// Checks, in this order: shape, manifest signature by the trusted key,
/// `manifest.uri == uri`, `manifest_ref`, expiry, file count, each path,
/// total size, file set equal to the manifest's, `.md` extension, SHA-256
/// of each file. Only an accepted bundle may be written to disk, and it
/// should be written to a staging directory and swapped in atomically.
pub fn check_downloaded_bundle(
    bundle: &Value,
    uri: &str,
    trusted_public_key_b64: &str,
    expected_ref: &str,
    now: i64,
) -> Result<AcceptedBundle, BundleReject> {
    let (Some(manifest), Some(signature)) = (
        bundle.get("manifest").filter(|m| m.is_object()),
        bundle.get("signature").and_then(Value::as_str),
    ) else {
        return Err(BundleReject::Malformed);
    };
    let Some(files) = bundle.get("files").and_then(Value::as_object) else {
        return Err(BundleReject::Malformed);
    };
    if !verify_manifest(manifest, signature, trusted_public_key_b64) {
        return Err(BundleReject::BadSignature);
    }
    if manifest.get("uri").and_then(Value::as_str) != Some(uri) {
        return Err(BundleReject::UriMismatch);
    }
    if manifest_ref(manifest).as_deref() != Some(expected_ref) {
        return Err(BundleReject::RefMismatch);
    }
    if is_manifest_expired(manifest, now) {
        return Err(BundleReject::Expired);
    }

    let declared_count = match manifest.get("file_count") {
        None => files.len() as u64,
        Some(v) => v.as_u64().ok_or(BundleReject::Malformed)?,
    };
    if declared_count > MAX_BUNDLE_FILES as u64 || files.len() > MAX_BUNDLE_FILES {
        return Err(BundleReject::TooManyFiles);
    }

    let mut accepted = Vec::with_capacity(files.len());
    let mut total = 0usize;
    for (path, content) in files {
        let Some(content) = content.as_str() else {
            return Err(BundleReject::Malformed);
        };
        if !validate_path(path) {
            return Err(BundleReject::BadPath(path.clone()));
        }
        total += content.len();
        if total > MAX_BUNDLE_TOTAL_SIZE {
            return Err(BundleReject::TooLarge);
        }
        accepted.push((path.clone(), content.as_bytes().to_vec()));
    }

    let Some(entries) = manifest.get("files").and_then(Value::as_array) else {
        return Err(BundleReject::Malformed);
    };
    let mut expected = std::collections::BTreeMap::new();
    for entry in entries {
        match (
            entry.get("path").and_then(Value::as_str),
            entry.get("hash").and_then(Value::as_str),
        ) {
            (Some(p), Some(h)) => {
                expected.insert(p, h);
            }
            _ => return Err(BundleReject::Malformed),
        }
    }
    if expected.len() != files.len() || !files.keys().all(|p| expected.contains_key(p.as_str())) {
        return Err(BundleReject::FilesMismatch);
    }
    for (path, raw) in &accepted {
        if !path.ends_with(".md") {
            return Err(BundleReject::NotMarkdown(path.clone()));
        }
        if hex::encode(Sha256::digest(raw)) != expected[path.as_str()] {
            return Err(BundleReject::Corrupted(path.clone()));
        }
    }
    Ok(AcceptedBundle { files: accepted })
}
