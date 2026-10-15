//! Signed MDP2P structures: manifests, naming records and review records.
//!
//! Every structure is a JSON object signed with Ed25519 over its canonical
//! JSON (see [`crate::canonical`]). Keys travel as raw 32-byte public keys
//! in standard, padded base64; signatures likewise (64 bytes).

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::canonical::canonical_json;

/// Default tolerance between a record's timestamp and the verifier's clock
/// when a record is first accepted (naming server, review inbox).
pub const MAX_TIMESTAMP_DRIFT_SECONDS: i64 = 300;

/// Verdicts a reviewer may sign.
pub const VALID_VERDICTS: [&str; 3] = ["ok", "warn", "reject"];

/// Why a signed record was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordError {
    /// The value is not a JSON object.
    NotAnObject,
    /// A required field is absent.
    MissingField(&'static str),
    /// A field is present but has the wrong type or an invalid value.
    BadField(&'static str),
    /// `|now - timestamp|` exceeds the allowed drift.
    TimestampDrift,
    /// `content_key` does not derive from `(uri, publisher_public_key)`.
    ContentKeyMismatch,
    /// The key or signature cannot be decoded, or the signature is wrong.
    BadSignature,
}

/// Decode a raw base64 Ed25519 public key.
pub fn decode_public_key(b64: &str) -> Option<VerifyingKey> {
    let raw: [u8; 32] = B64.decode(b64).ok()?.try_into().ok()?;
    VerifyingKey::from_bytes(&raw).ok()
}

/// Check `signature_b64` over the canonical JSON of `value` with `public_key_b64`.
///
/// Uses strict verification (rejects non-canonical and small-order
/// encodings), which never accepts a signature the reference rejects.
pub fn verify_signature(public_key_b64: &str, signature_b64: &str, value: &Value) -> bool {
    let Some(key) = decode_public_key(public_key_b64) else {
        return false;
    };
    let Ok(raw) = B64.decode(signature_b64) else {
        return false;
    };
    let Ok(signature) = Signature::from_slice(&raw) else {
        return false;
    };
    let Ok(message) = canonical_json(value) else {
        return false;
    };
    key.verify_strict(&message, &signature).is_ok()
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// DHT key shared by every version of `uri` published by `public_key_b64`.
///
/// `"/mdp2p/" + hex(SHA-256(uri + ":" + public_key_b64))`.
pub fn content_key(uri: &str, public_key_b64: &str) -> String {
    format!(
        "/mdp2p/{}",
        sha256_hex(format!("{uri}:{public_key_b64}").as_bytes())
    )
}

/// `hex(SHA-256(canonical_json(manifest)))`, the value a naming record
/// commits to. `None` if the manifest has no canonical form.
pub fn manifest_ref(manifest: &Value) -> Option<String> {
    canonical_json(manifest)
        .ok()
        .map(|bytes| sha256_hex(&bytes))
}

/// Verify a manifest against a public key obtained independently of it
/// (from the naming record). The embedded `public_key` must equal it.
pub fn verify_manifest(
    manifest: &Value,
    signature_b64: &str,
    trusted_public_key_b64: &str,
) -> bool {
    let embedded = manifest.get("public_key").and_then(Value::as_str);
    embedded == Some(trusted_public_key_b64)
        && verify_signature(trusted_public_key_b64, signature_b64, manifest)
}

/// Whether `expires_at` is in the past. A missing field never expires; a
/// non-integer one is treated as expired (the reference fails on it).
pub fn is_manifest_expired(manifest: &Value, now: i64) -> bool {
    match manifest.get("expires_at") {
        None | Some(Value::Null) => false,
        Some(v) => v.as_i64().map_or(true, |expires_at| now > expires_at),
    }
}

fn object(record: &Value) -> Result<&Map<String, Value>, RecordError> {
    record.as_object().ok_or(RecordError::NotAnObject)
}

fn require(map: &Map<String, Value>, fields: &[&'static str]) -> Result<(), RecordError> {
    match fields.iter().find(|f| !map.contains_key(**f)) {
        Some(f) => Err(RecordError::MissingField(f)),
        None => Ok(()),
    }
}

fn str_field<'a>(map: &'a Map<String, Value>, field: &'static str) -> Result<&'a str, RecordError> {
    map.get(field)
        .and_then(Value::as_str)
        .ok_or(RecordError::BadField(field))
}

fn check_drift(
    map: &Map<String, Value>,
    max_drift: Option<i64>,
    now: i64,
) -> Result<(), RecordError> {
    let ts = map
        .get("timestamp")
        .and_then(Value::as_i64)
        .ok_or(RecordError::BadField("timestamp"))?;
    match max_drift {
        Some(drift) if (now - ts).abs() > drift => Err(RecordError::TimestampDrift),
        _ => Ok(()),
    }
}

fn check_signature(
    record: &Value,
    key_field: &'static str,
    signature_b64: &str,
) -> Result<(), RecordError> {
    let key = str_field(object(record)?, key_field)?;
    if verify_signature(key, signature_b64, record) {
        Ok(())
    } else {
        Err(RecordError::BadSignature)
    }
}

/// Verify a naming record `{uri, author, public_key, manifest_ref, timestamp}`
/// against its embedded key. `max_drift = None` skips the clock check (used
/// when re-reading records a server accepted earlier).
///
/// This does NOT check that `uri` is the name that was asked for; callers
/// resolving a name must compare it themselves (see [`crate::fetch`]).
pub fn verify_name_record(
    record: &Value,
    signature_b64: &str,
    max_drift: Option<i64>,
    now: i64,
) -> Result<(), RecordError> {
    let map = object(record)?;
    require(
        map,
        &["uri", "author", "public_key", "manifest_ref", "timestamp"],
    )?;
    check_drift(map, max_drift, now)?;
    check_signature(record, "public_key", signature_b64)
}

/// Verify a reviewer's verdict `{content_key, reviewer_public_key, verdict,
/// comment, timestamp}`.
pub fn verify_review_record(
    record: &Value,
    signature_b64: &str,
    max_drift: Option<i64>,
    now: i64,
) -> Result<(), RecordError> {
    let map = object(record)?;
    require(
        map,
        &[
            "content_key",
            "reviewer_public_key",
            "verdict",
            "comment",
            "timestamp",
        ],
    )?;
    let verdict = str_field(map, "verdict")?;
    if !VALID_VERDICTS.contains(&verdict) {
        return Err(RecordError::BadField("verdict"));
    }
    check_drift(map, max_drift, now)?;
    check_signature(record, "reviewer_public_key", signature_b64)
}

/// Verify a reviewer opt-in `{public_key, peer_id, addrs, categories, timestamp}`.
pub fn verify_reviewer_opt_in(
    record: &Value,
    signature_b64: &str,
    max_drift: Option<i64>,
    now: i64,
) -> Result<(), RecordError> {
    let map = object(record)?;
    require(
        map,
        &["public_key", "peer_id", "addrs", "categories", "timestamp"],
    )?;
    if !map["categories"].is_array() {
        return Err(RecordError::BadField("categories"));
    }
    let addrs_ok = map["addrs"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_string));
    if !addrs_ok {
        return Err(RecordError::BadField("addrs"));
    }
    if map["peer_id"].as_str().map_or(true, str::is_empty) {
        return Err(RecordError::BadField("peer_id"));
    }
    check_drift(map, max_drift, now)?;
    check_signature(record, "public_key", signature_b64)
}

/// Verify a publisher's assignment `{uri, content_key, publisher_public_key,
/// reviewer_public_keys, deadline, timestamp}`. `content_key` must derive
/// from `(uri, publisher_public_key)` so nobody can solicit reviews for
/// someone else's content.
pub fn verify_review_assignment(
    record: &Value,
    signature_b64: &str,
    max_drift: Option<i64>,
    now: i64,
) -> Result<(), RecordError> {
    let map = object(record)?;
    require(
        map,
        &[
            "uri",
            "content_key",
            "publisher_public_key",
            "reviewer_public_keys",
            "deadline",
            "timestamp",
        ],
    )?;
    let reviewers_ok = map["reviewer_public_keys"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_string));
    if !reviewers_ok {
        return Err(RecordError::BadField("reviewer_public_keys"));
    }
    if map["deadline"].as_i64().map_or(true, |d| d <= 0) {
        return Err(RecordError::BadField("deadline"));
    }
    let expected = content_key(
        map["uri"].as_str().unwrap_or(""),
        map["publisher_public_key"].as_str().unwrap_or(""),
    );
    if map["content_key"].as_str() != Some(expected.as_str()) {
        return Err(RecordError::ContentKeyMismatch);
    }
    check_drift(map, max_drift, now)?;
    check_signature(record, "publisher_public_key", signature_b64)
}
