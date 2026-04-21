"""
MDP2P Trust — Signal type.

A Signal is the unified input to the scoring function. Reviews (pre-publication
peer opinions) and reports (post-publication flags) both serialize to the same
structure so the scorer treats them uniformly.

Signature verification is NOT performed here — callers are expected to pass in
already-verified signals. This keeps the scorer pure and unit-testable.
"""

from dataclasses import dataclass
from typing import Literal


SignalKind = Literal["review", "report"]
Verdict = Literal["ok", "warn", "reject"]


@dataclass(frozen=True)
class Signal:
    """A single signed opinion about a piece of content."""

    kind: SignalKind
    content_key: str
    source_pubkey: str
    verdict: Verdict
    reason: str = ""
    timestamp: int = 0
