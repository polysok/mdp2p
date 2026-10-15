//! Trust-on-first-use key pinning with rollback protection.
//!
//! A pin remembers, per site name, the author key seen on first visit and
//! the newest naming-record timestamp seen since. Storage is up to the
//! client; this module only decides.

/// What the client remembers about a site.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pin {
    pub public_key: String,
    /// Newest naming-record timestamp accepted so far. `None` for pins
    /// written before rollback protection existed.
    pub record_timestamp: Option<i64>,
}

/// Outcome of checking a resolved name against the pin.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PinStatus {
    /// First visit: accept and pin.
    Unknown,
    /// Same key, record at least as recent: accept and raise the mark.
    Match,
    /// The author key changed: refuse (possible impersonation).
    Mismatch,
    /// Same key but an older record than already seen: refuse (replay of
    /// a superseded version).
    Rollback,
}

impl PinStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Match => "match",
            Self::Mismatch => "mismatch",
            Self::Rollback => "rollback",
        }
    }
}

/// Check `public_key_b64` / `record_timestamp` against an optional pin.
pub fn check_pin(pin: Option<&Pin>, public_key_b64: &str, record_timestamp: i64) -> PinStatus {
    match pin {
        None => PinStatus::Unknown,
        Some(p) if p.public_key != public_key_b64 => PinStatus::Mismatch,
        Some(Pin {
            record_timestamp: Some(seen),
            ..
        }) if record_timestamp < *seen => PinStatus::Rollback,
        Some(_) => PinStatus::Match,
    }
}
