"""
MDP2P Trust Policy — User-defined moderation configuration.

Exposes the knobs each user controls over their own filtering:
  - thresholds (warn / hide)
  - default weight granted to unknown peers
  - learning rate applied to confirmed/disputed history
  - cap on per-peer weight (anti-accumulation)
  - time decay half-life for old signals
  - severity map per verdict
  - explicit list of subscribed moderators with their weights

Persisted as JSON at `~/.mdp2p/policy.json`. Intended to be human-editable.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ModeratorSubscription:
    """A peer the user explicitly trusts as a moderator."""

    pubkey: str
    weight: float = 1.0
    label: str = ""


@dataclass
class Policy:
    """User moderation policy. All knobs have sensible defaults."""

    threshold_warn: float = 2.0
    threshold_hide: float = 5.0
    default_weight_unknown: float = 0.1
    max_weight_per_peer: float = 2.0
    learning_rate_per_signal: float = 0.1
    decay_half_life_days: float = 90.0
    severity: dict[str, float] = field(
        default_factory=lambda: {"ok": 0.0, "warn": 1.0, "reject": 3.0}
    )
    subscribed_moderators: list[ModeratorSubscription] = field(default_factory=list)


def default_policy() -> Policy:
    """Return a Policy populated with default values."""
    return Policy()


def load_policy(path: str) -> Policy:
    """Load a Policy from JSON, or return defaults when the file is missing."""
    p = Path(path)
    if not p.exists():
        return default_policy()

    data = json.loads(p.read_text(encoding="utf-8"))
    return _policy_from_dict(data)


def save_policy(policy: Policy, path: str) -> None:
    """Save a Policy to JSON, creating parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(policy), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def is_subscribed_moderator(policy: Policy, pubkey: str) -> Optional[float]:
    """Return the explicit weight for a subscribed moderator, or None."""
    for sub in policy.subscribed_moderators:
        if sub.pubkey == pubkey:
            return sub.weight
    return None


def _policy_from_dict(data: dict) -> Policy:
    """Build a Policy from a JSON dict, dropping unknown keys (forward compat)."""
    defaults = default_policy()
    subs_raw = data.get("subscribed_moderators") or []
    subs = [
        ModeratorSubscription(
            pubkey=s["pubkey"],
            weight=float(s.get("weight", 1.0)),
            label=s.get("label", ""),
        )
        for s in subs_raw
        if isinstance(s, dict) and "pubkey" in s
    ]
    severity = data.get("severity")
    if not isinstance(severity, dict):
        severity = defaults.severity

    return Policy(
        threshold_warn=float(data.get("threshold_warn", defaults.threshold_warn)),
        threshold_hide=float(data.get("threshold_hide", defaults.threshold_hide)),
        default_weight_unknown=float(
            data.get("default_weight_unknown", defaults.default_weight_unknown)
        ),
        max_weight_per_peer=float(
            data.get("max_weight_per_peer", defaults.max_weight_per_peer)
        ),
        learning_rate_per_signal=float(
            data.get("learning_rate_per_signal", defaults.learning_rate_per_signal)
        ),
        decay_half_life_days=float(
            data.get("decay_half_life_days", defaults.decay_half_life_days)
        ),
        severity={k: float(v) for k, v in severity.items()},
        subscribed_moderators=subs,
    )
