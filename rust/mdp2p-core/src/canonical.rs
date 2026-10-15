//! Canonical JSON: the exact bytes every MDP2P signature is computed over.
//!
//! Byte-identical to Python's
//! `json.dumps(obj, separators=(",", ":"), sort_keys=True)` (with its
//! default `ensure_ascii=True`):
//!
//! - object keys sorted by Unicode code point, no whitespace;
//! - strings escape `"` `\` `\b` `\f` `\n` `\r` `\t`; every other
//!   character outside printable ASCII (`' '..='~'`, so control characters,
//!   DEL and all non-ASCII) as `\uXXXX`, using UTF-16 surrogate pairs above
//!   U+FFFF, lowercase hex; `/` is emitted as is;
//! - integers in plain decimal.
//!
//! Floats are refused: no signed MDP2P structure carries one, and their
//! textual form differs between implementations.

use serde_json::Value;

/// Why a value has no canonical form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalError {
    /// A non-integer number was found.
    Float,
}

/// Serialize `value` to its canonical JSON bytes.
pub fn canonical_json(value: &Value) -> Result<Vec<u8>, CanonicalError> {
    let mut out = Vec::with_capacity(256);
    write_value(value, &mut out)?;
    Ok(out)
}

fn write_value(value: &Value, out: &mut Vec<u8>) -> Result<(), CanonicalError> {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.extend_from_slice(i.to_string().as_bytes());
            } else if let Some(u) = n.as_u64() {
                out.extend_from_slice(u.to_string().as_bytes());
            } else {
                return Err(CanonicalError::Float);
            }
        }
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_value(item, out)?;
            }
            out.push(b']');
        }
        Value::Object(map) => {
            // serde_json's default Map is a BTreeMap<String, _>: iteration
            // is in byte order of the UTF-8 keys, which equals code point
            // order (what Python's sort_keys uses). Sort explicitly anyway
            // so the `preserve_order` feature cannot silently break this.
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push(b'{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_string(key, out);
                out.push(b':');
                write_value(&map[key], out)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

fn write_string(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\u{0c}' => out.extend_from_slice(b"\\f"),
            c if !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.extend_from_slice(format!("\\u{:04x}", unit).as_bytes());
                }
            }
            c => out.push(c as u8),
        }
    }
    out.push(b'"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sorts_keys_and_strips_whitespace() {
        let v = json!({"b": 1, "a": [true, null]});
        assert_eq!(canonical_json(&v).unwrap(), br#"{"a":[true,null],"b":1}"#);
    }

    #[test]
    fn escapes_like_python_ensure_ascii() {
        let v = json!("é🙂\u{7f}/");
        assert_eq!(
            canonical_json(&v).unwrap(),
            b"\"\\u00e9\\ud83d\\ude42\\u007f/\"".to_vec()
        );
    }

    #[test]
    fn refuses_floats() {
        assert_eq!(canonical_json(&json!(1.5)), Err(CanonicalError::Float));
    }
}
