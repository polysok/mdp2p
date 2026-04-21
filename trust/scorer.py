"""
MDP2P Trust Scorer — Pure scoring function.

Turns a list of (already-verified) Signals plus the user's trust state and
policy into a ScoreResult: an aggregate score, a display decision, and a
per-signal breakdown for UI display.

Contribution formula for a single signal:

    weight     = explicit_trust (user override)
               | moderator weight (if subscribed)
               | default_weight_unknown + learning_rate * (confirmed - disputed)
    weight     = clamp(weight, 0, max_weight_per_peer)
    severity   = policy.severity[verdict]
    decay      = 0.5 ** (age_days / half_life_days)
    contrib    = weight * severity * decay

Anti-amplification: a given source_pubkey contributes at most once per
content_key. If the same peer emitted several signals, the one with the
highest severity is kept.
"""

import time
from dataclasses import dataclass
from typing import Literal, Optional

from trust.policy import Policy, is_subscribed_moderator
from trust.signal import Signal


Decision = Literal["show", "warn", "hide"]


@dataclass(frozen=True)
class SignalContribution:
    """How a single signal contributed to the final score. Useful for UI."""

    source_pubkey: str
    verdict: str
    weight: float
    severity: float
    decay: float
    contribution: float


@dataclass(frozen=True)
class ScoreResult:
    """Output of score_content."""

    score: float
    decision: Decision
    breakdown: list[SignalContribution]


def score_content(
    signals: list[Signal],
    store: dict,
    policy: Policy,
    now: Optional[int] = None,
) -> ScoreResult:
    """Compute the aggregate score and decision for a piece of content."""
    effective_now = int(time.time()) if now is None else int(now)
    deduped = _dedupe_by_source(signals, policy)

    contributions: list[SignalContribution] = []
    total = 0.0
    for signal in deduped:
        weight = _weight_for(signal.source_pubkey, store, policy)
        severity = policy.severity.get(signal.verdict, 0.0)
        decay = _time_decay(signal.timestamp, effective_now, policy.decay_half_life_days)
        contrib = weight * severity * decay
        contributions.append(
            SignalContribution(
                source_pubkey=signal.source_pubkey,
                verdict=signal.verdict,
                weight=weight,
                severity=severity,
                decay=decay,
                contribution=contrib,
            )
        )
        total += contrib

    return ScoreResult(
        score=total,
        decision=_decide(total, policy),
        breakdown=contributions,
    )


def _dedupe_by_source(signals: list[Signal], policy: Policy) -> list[Signal]:
    """Keep one signal per source_pubkey — the most severe one."""
    best: dict[str, Signal] = {}
    for signal in signals:
        current = best.get(signal.source_pubkey)
        if current is None:
            best[signal.source_pubkey] = signal
            continue
        if policy.severity.get(signal.verdict, 0.0) > policy.severity.get(
            current.verdict, 0.0
        ):
            best[signal.source_pubkey] = signal
    return list(best.values())


def _weight_for(pubkey: str, store: dict, policy: Policy) -> float:
    """Resolve the effective weight for a signaling peer."""
    subscribed = is_subscribed_moderator(policy, pubkey)
    if subscribed is not None:
        return _clamp(subscribed, 0.0, policy.max_weight_per_peer)

    record = store.get("peers", {}).get(pubkey)
    if record is None:
        return policy.default_weight_unknown

    explicit = record.get("explicit_trust")
    if explicit is not None:
        return _clamp(float(explicit), 0.0, policy.max_weight_per_peer)

    base = policy.default_weight_unknown
    confirmed = int(record.get("confirmed_signals", 0))
    disputed = int(record.get("disputed_signals", 0))
    learned = base + policy.learning_rate_per_signal * (confirmed - disputed)
    return _clamp(learned, 0.0, policy.max_weight_per_peer)


def _time_decay(signal_ts: int, now: int, half_life_days: float) -> float:
    """Exponential decay applied to a signal based on its age.

    A signal with timestamp <= 0 or in the future is treated as fresh
    (decay = 1.0). This lets tests and signals without a trustworthy clock
    behave predictably.
    """
    if half_life_days <= 0 or signal_ts <= 0 or signal_ts >= now:
        return 1.0
    age_days = (now - signal_ts) / 86400.0
    return 0.5 ** (age_days / half_life_days)


def _decide(score: float, policy: Policy) -> Decision:
    if score >= policy.threshold_hide:
        return "hide"
    if score >= policy.threshold_warn:
        return "warn"
    return "show"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
