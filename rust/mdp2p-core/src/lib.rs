//! # mdp2p-core
//!
//! The verification core of the MDP2P (`md://`) protocol, shared by every
//! client: browser extension (WASM), mobile apps (UniFFI) and the desktop
//! daemon. It decides what to trust; it does no I/O.
//!
//! The rules are specified in `SPEC.md` at the repository root and pinned
//! by the conformance vectors in `spec/vectors/`, which are generated from
//! the Python reference implementation. `tests/vectors.rs` replays them.

pub mod canonical;
pub mod fetch;
pub mod paths;
pub mod pin;
pub mod records;
pub mod selection;

pub use canonical::{canonical_json, CanonicalError};
pub use fetch::{
    check_downloaded_bundle, check_resolved_record, AcceptedBundle, BundleReject, ResolveError,
    ResolvedName,
};
pub use paths::{validate_path, validate_uri};
pub use pin::{check_pin, Pin, PinStatus};
pub use records::{
    content_key, is_manifest_expired, manifest_ref, verify_manifest, verify_name_record,
    verify_review_assignment, verify_review_record, verify_reviewer_opt_in, RecordError,
};
pub use selection::select_reviewers;
