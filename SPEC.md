# MDP2P protocol specification

Status: draft, version 1 (`/mdp2p/*/1.0.0`). This document describes what
the Python reference implementation does today. Two implementations agree
when they reach the same verdict on every case in `spec/vectors/`.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Conformance

- `spec/vectors/*.json` are generated from the reference by
  `python scripts/gen_vectors.py` (fixed keys, pinned clock, reproducible).
- `tests/test_vectors.py` replays them against Python;
  `rust/mdp2p-core/tests/vectors.rs` replays them against Rust.
- A deliberate behaviour change updates this file, regenerates the
  vectors, and keeps every implementation green in the same pull request.

## 2. Encodings

### 2.1 Canonical JSON

Every signature and every hash of a structure is computed over its
canonical JSON, which is byte-identical to Python's
`json.dumps(obj, separators=(",", ":"), sort_keys=True)`:

- no whitespace; object keys sorted by Unicode code point;
- strings: `"` → `\"`, `\` → `\\`, and `\b \f \n \r \t`; every other
  character outside printable ASCII (U+0020 to U+007E), including the
  remaining control characters, DEL and all non-ASCII characters, is
  written as `\uXXXX` with lowercase hex, using a UTF-16 surrogate pair
  above U+FFFF. `/` is not escaped;
- integers in decimal; `true`, `false`, `null`.

Signed structures MUST NOT contain non-integer numbers. Implementations
MUST refuse to canonicalize them.

### 2.2 Keys, signatures, hashes

- **Identity** is an Ed25519 key pair. A site's identity is its author key.
  The libp2p PeerID only identifies a transport endpoint.
- Public keys travel as the raw 32 bytes and signatures as the raw 64
  bytes, both in standard padded base64.
- Signatures are verified over the canonical JSON of the whole structure,
  as received (unknown fields included). Verifiers SHOULD use strict
  verification (RFC 8032 §5.1.7, reject non-canonical encodings).
- Hashes are lowercase hex SHA-256.

### 2.3 Names and paths

- **Site name** (`uri`, written `md://<uri>`): 1 to 255 characters from
  `[A-Za-z0-9._-]`, the first one alphanumeric, never containing `..`.
- **Bundle file path**: 1 to 10 segments separated by `/`. No segment may
  be empty, `.` or `..`, and the path MUST NOT contain `\` or NUL. Such a
  path is already normalized: it stays inside the site directory and two
  distinct paths never name the same file. Writers MUST NOT follow
  symbolic links inside the site directory.

### 2.4 Derived identifiers

- `content_key(uri, pk) = "/mdp2p/" + hex(SHA-256(uri + ":" + pk))`. It is
  the DHT key under which seeders of any version of that site advertise
  themselves, and the key that reviews attach to.
- `manifest_ref(manifest) = hex(SHA-256(canonical_json(manifest)))`, for
  the signed manifest including its `public_key` field.

## 3. Structures

All timestamps are integer Unix seconds.

### 3.1 Manifest

| Field | Type | Meaning |
| --- | --- | --- |
| `uri` | string | site name |
| `author` | string | display name (not authenticated beyond the signature) |
| `version` | int | author-side counter |
| `timestamp` | int | creation time |
| `expires_at` | int | after this time the manifest MUST be refused |
| `categories` | list of slugs | from `review/taxonomy.py` |
| `files` | list of `{path, hash, size}` | `hash` = SHA-256 of the raw file bytes |
| `total_size`, `file_count` | int | sums over `files` |
| `public_key` | string | author key, added by the signer |

It is signed by the author key and stored as `manifest.json` +
`manifest.sig` next to the files. Limits: 1000 files and 50 MiB in total.
Only `.md` files are carried.

### 3.2 Naming record

`{uri, author, public_key, manifest_ref, timestamp}`, signed by
`public_key`. It binds a name to one exact manifest.

### 3.3 Review structures

- **Reviewer opt-in** `{public_key, peer_id, addrs[], categories[], timestamp}`,
  signed by `public_key`.
- **Review assignment** `{uri, content_key, publisher_public_key,
  reviewer_public_keys[], deadline, timestamp}`, signed by the publisher.
  `content_key` MUST equal `content_key(uri, publisher_public_key)`.
- **Review record** `{content_key, reviewer_public_key, verdict, comment,
  timestamp}`, signed by the reviewer. `verdict ∈ {ok, warn, reject}`.

**Reviewer selection** is deterministic. Rank the deduplicated pool by
`SHA-256(content_key ‖ 0x00 ‖ pubkey)` in ascending order and take the
first `n`.

## 4. Wire protocols

Each message is a 4-byte big-endian length followed by that many bytes of
UTF-8 JSON. One request and one response per stream. Senders MUST split
large frames into writes of at most 32 KiB, because the Noise transport
caps a single write at 65535 bytes.

### 4.1 `/mdp2p/bundle/1.0.0` (seeders)

- `{"type":"get_bundle","uri"}` → `{"type":"bundle","manifest","signature","files":{path: utf8_text}}`
- `{"type":"get_manifest","uri"}` → `{"type":"manifest","manifest","signature"}`
- Errors: `{"type":"error","msg"}`.
- Requests are at most 64 KiB and MUST arrive within 10 s. Responses are
  at most 100 MiB. File contents are the exact file bytes decoded as
  UTF-8: no newline translation.

### 4.2 `/mdp2p/naming/1.0.0` (naming server, untrusted)

| Request | Response |
| --- | --- |
| `register {record, signature}` | `ok` / `error` |
| `resolve {uri}` | `record {record, signature}` / `error` |
| `list {offset?, limit?}` | `names {records[], next_offset}` |
| `register_reviewer {record, signature}` | `ok` / `error` |
| `list_reviewers {offset?, limit?}` | `reviewers {records[{record, signature}], next_offset}` |
| `post_assignment {record, signature}` | `ok` / `error` |
| `list_assignments {reviewer_public_key}` | `assignments {records[]}` |
| `attach_review {record, signature}` | `ok` / `error` |
| `get_attachments {content_key}` | `attachments {records[]}` |

Server rules: requests are at most 64 KiB and arrive within 10 s;
listings are paginated (`next_offset = null` on the last page, at most 1000
per page); records are verified with a 300 s clock drift.

- `register`: a name stays with its first key, and its timestamp only
  increases.
- `post_assignment`: at most 16 distinct reviewers, all of them registered.
- `attach_review`: only accepted from a reviewer assigned to that
  `content_key`, with a review timestamp ≤ the assignment deadline. Each
  reviewer can amend a review with a newer timestamp.

## 5. Client acceptance rules

The naming server and the seeders are untrusted. Clients MUST apply all
of the following before showing or seeding content (`mdp2p_core::fetch`).

**Resolve.** The response is `{type: "record", record, signature}`. The
record verifies against its own `public_key` (no clock check), and
`record.uri` MUST equal the requested name.

**Pin (TOFU with rollback protection).** For each name the client stores
the author key and the newest record timestamp it accepted. The result is:

- *unknown*: accept and pin;
- *mismatch* (the key differs): refuse;
- *rollback* (the record timestamp is older than the stored one): refuse;
- otherwise *match*.

A client MUST fail closed if its pin store is unreadable.

**Bundle.** For each seeder response, checks run in this order, each with
its reason code:

1. shape: `malformed`;
2. manifest signature with the resolved key, and the embedded
   `public_key` equal to it: `bad_signature`;
3. `manifest.uri` equals the requested name: `uri_mismatch`;
4. `manifest_ref` equals the record's: `ref_mismatch`;
5. not expired: `expired`;
6. file count: `too_many_files`;
7. every path valid (§2.3): `bad_path`;
8. total size: `too_large`;
9. the set of files equals the manifest's: `files_mismatch`;
10. every file is `.md`: `not_markdown`;
11. every hash matches: `corrupted`.

The order among files is unspecified.

A failing seeder MUST NOT fail the whole fetch. The client keeps the first
accepted bundle, cancels the other downloads, and bounds each attempt with
a timeout. An accepted bundle is written to a staging directory, then
swapped in atomically.

**Updates.** A manifest offered as an update counts only if it is signed by
the pinned key for the same name.

## 6. Known gaps (planned for v2)

- Signatures have no domain separation: every structure is signed as bare
  canonical JSON.
- No key rotation or revocation.
- Name squatting: first registration wins, and the naming server is a
  de-facto authority for unknown names.
- Seeding reveals who reads what: the DHT announces the reader's PeerID
  and addresses for each site.
- Registering as a reviewer costs nothing.
- An expired manifest makes the site unreadable.
