"""
Deterministic reviewer selection.

Given a content key and a pool of reviewer public keys, `select_reviewers`
returns the N reviewers who should be contacted. The selection is

  - deterministic: same inputs always yield the same output, so any peer
    can reproduce the selection without contacting the publisher;
  - content-dependent: the ranking varies with the content key, so a given
    reviewer is not systematically paired with the same author;
  - uniform: over many content keys, each reviewer's selection probability
    is ~ n / |pool|, so no peer accumulates disproportionate review load;
  - cheap to compute and verify: O(|pool| log |pool|) with a single SHA-256
    per candidate.

The ranking key is `SHA-256(content_key || pubkey)`. Candidates are sorted
ascending by this digest and the top N are picked. This is the same
hash-sortition idea used in a number of well-known random-beacon and
leader-election schemes, minus the cryptographic randomness (we do not need
unpredictability — only a fair and reproducible ordering).
"""

import hashlib
from typing import Iterable


def select_reviewers(
    content_key: str,
    pool: Iterable[str],
    n: int,
) -> list[str]:
    """Return up to `n` reviewer public keys selected from `pool`.

    The pool is deduplicated before ranking so duplicate entries cannot
    inflate a reviewer's selection probability.
    """
    if n <= 0:
        return []

    unique_pool = sorted(set(pool))
    if not unique_pool:
        return []

    content_bytes = content_key.encode("utf-8")

    def rank(pubkey: str) -> bytes:
        h = hashlib.sha256()
        h.update(content_bytes)
        h.update(b"\x00")  # separator to avoid collisions across fields
        h.update(pubkey.encode("utf-8"))
        return h.digest()

    ranked = sorted(unique_pool, key=rank)
    return ranked[: min(n, len(ranked))]
