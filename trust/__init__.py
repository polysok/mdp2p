"""
MDP2P Trust — Local moderation state and scoring.

The trust package holds all local-only state backing the moderation system:
per-peer weights learned from observed behavior, the user's moderation
policy (thresholds, severity map, subscribed moderators) and the pure
scoring function that turns a list of Signals into a display decision.

Nothing here touches the network. Network-facing modules (review protocol,
report protocol) consume and feed this package.
"""

from trust.policy import (
    ModeratorSubscription,
    Policy,
    default_policy,
    is_subscribed_moderator,
    load_policy,
    save_policy,
)
from trust.scorer import ScoreResult, SignalContribution, score_content
from trust.signal import Signal
from trust.store import (
    get_peer,
    load_store,
    record_confirmed,
    record_disputed,
    save_store,
    set_explicit_trust,
)

__all__ = [
    "ModeratorSubscription",
    "Policy",
    "ScoreResult",
    "Signal",
    "SignalContribution",
    "default_policy",
    "get_peer",
    "is_subscribed_moderator",
    "load_policy",
    "load_store",
    "record_confirmed",
    "record_disputed",
    "save_policy",
    "save_store",
    "score_content",
    "set_explicit_trust",
]
