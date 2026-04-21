import json
from pathlib import Path

import pytest

from trust.policy import (
    ModeratorSubscription,
    Policy,
    default_policy,
    is_subscribed_moderator,
    load_policy,
    save_policy,
)


@pytest.fixture
def policy_path(tmp_path):
    return str(tmp_path / "policy.json")


class TestDefaultPolicy:
    def test_thresholds_ordered(self):
        p = default_policy()
        assert 0 < p.threshold_warn < p.threshold_hide

    def test_severity_contains_all_verdicts(self):
        p = default_policy()
        assert {"ok", "warn", "reject"} <= set(p.severity.keys())
        assert p.severity["ok"] < p.severity["warn"] < p.severity["reject"]

    def test_no_subscriptions_by_default(self):
        assert default_policy().subscribed_moderators == []


class TestLoadSave:
    def test_load_nonexistent_returns_defaults(self, policy_path):
        loaded = load_policy(policy_path)
        assert loaded == default_policy()

    def test_roundtrip_preserves_all_fields(self, policy_path):
        policy = Policy(
            threshold_warn=1.5,
            threshold_hide=4.0,
            default_weight_unknown=0.2,
            max_weight_per_peer=3.0,
            learning_rate_per_signal=0.05,
            decay_half_life_days=60.0,
            severity={"ok": 0.0, "warn": 0.5, "reject": 2.0},
            subscribed_moderators=[
                ModeratorSubscription(
                    pubkey="modkey", weight=1.2, label="trusted-mod"
                )
            ],
        )
        save_policy(policy, policy_path)
        loaded = load_policy(policy_path)
        assert loaded == policy

    def test_save_creates_parent_dirs(self, tmp_path):
        deep_path = str(tmp_path / "a" / "b" / "policy.json")
        save_policy(default_policy(), deep_path)
        assert Path(deep_path).exists()

    def test_drops_unknown_fields_forward_compat(self, policy_path):
        raw = {"threshold_warn": 2.5, "unknown_future_field": "ignore_me"}
        Path(policy_path).write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_policy(policy_path)
        assert loaded.threshold_warn == 2.5
        assert loaded.threshold_hide == default_policy().threshold_hide

    def test_invalid_severity_falls_back_to_defaults(self, policy_path):
        raw = {"severity": "not-a-dict"}
        Path(policy_path).write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_policy(policy_path)
        assert loaded.severity == default_policy().severity


class TestIsSubscribedModerator:
    def test_none_when_not_subscribed(self):
        p = default_policy()
        assert is_subscribed_moderator(p, "anyone") is None

    def test_returns_weight_for_subscriber(self):
        p = Policy(
            subscribed_moderators=[
                ModeratorSubscription(pubkey="mod1", weight=1.5)
            ]
        )
        assert is_subscribed_moderator(p, "mod1") == 1.5

    def test_first_match_wins_on_duplicates(self):
        p = Policy(
            subscribed_moderators=[
                ModeratorSubscription(pubkey="mod1", weight=1.0),
                ModeratorSubscription(pubkey="mod1", weight=2.0),
            ]
        )
        assert is_subscribed_moderator(p, "mod1") == 1.0
