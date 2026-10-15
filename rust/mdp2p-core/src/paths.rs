//! Site names (`md://<uri>`) and file paths inside a bundle.

/// Longest accepted site name, in characters.
pub const MAX_URI_LENGTH: usize = 255;
/// Most `/`-separated segments a bundle file path may have.
pub const MAX_PATH_DEPTH: usize = 10;

/// A site name: 1 to 255 characters from `[A-Za-z0-9._-]`, starting with an
/// alphanumeric character, and never containing `..`.
pub fn validate_uri(uri: &str) -> bool {
    let mut chars = uri.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    uri.chars().count() <= MAX_URI_LENGTH
        && first.is_ascii_alphanumeric()
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        && !uri.contains("..")
}

/// A bundle file path: 1 to [`MAX_PATH_DEPTH`] `/`-separated segments, none
/// empty, `.` or `..`, and no backslash or NUL anywhere. Such a path is
/// already normalized, so it can never leave the site directory and two
/// distinct paths never name the same file.
pub fn validate_path(path: &str) -> bool {
    if path.contains('\\') || path.contains('\0') {
        return false;
    }
    let segments: Vec<&str> = path.split('/').collect();
    segments.len() <= MAX_PATH_DEPTH && segments.iter().all(|s| !matches!(*s, "" | "." | ".."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uri_rules() {
        assert!(validate_uri("blog.alice"));
        assert!(!validate_uri("blog\n"));
        assert!(!validate_uri("a..b"));
    }

    #[test]
    fn path_rules() {
        assert!(validate_path("posts/one.md"));
        assert!(!validate_path("a/../b.md"));
        assert!(!validate_path("/etc/passwd.md"));
    }
}
