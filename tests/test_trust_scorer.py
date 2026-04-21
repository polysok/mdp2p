import pytest

from trust.policy import ModeratorSubscription, Policy, default_policy
from trust.scorer import score_content
from trust.signal import Signal


NOW = 1_700_000_000
DAY = 86_400


def _store_with(peers):
    return {"peers": dict(peers)}


def _signal(source, verdict="reject", timestamp=NOW, content_key="c1"):
    return Signal(
        kind="report",
        content_key=content_key,
        source_pubkey=source,
        verdict=verdict,
        timestamp=timestamp,
    )


class TestEmptyAndTrivial:
    def test_no_signals_score_zero_and_show(self):
        result = score_content([], _store_with({}), default_policy(), now=NOW)
        assert result.score == 0.0
        assert result.decision == "show"
        assert result.breakdown == []

    def test_ok_verdict_contributes_zero(self):
        signals = [_signal("p1", verdict="ok")]
        result = score_content(signals, _store_with({}), default_policy(), now=NOW)
        assert result.score == 0.0
        assert result.decision == "show"


class TestThresholds:
    def test_warn_threshold_crossed(self):
        policy = Policy(
            threshold_warn=0.2,
            threshold_hide=10.0,
            default_weight_unknown=0.5,
        )
        # contribution = 0.5 * 3.0 * 1.0 = 1.5 → above 0.2, below 10.0
        signals = [_signal("unknown_peer", verdict="reject")]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert result.decision == "warn"

    def test_hide_threshold_crossed_by_moderator(self):
        mod_key = "mod_pub"
        policy = Policy(
            threshold_warn=1.0,
            threshold_hide=2.0,
            subscribed_moderators=[
                ModeratorSubscription(pubkey=mod_key, weight=1.0)
            ],
        )
        # 1.0 * 3.0 * 1.0 = 3.0 → above hide
        signals = [_signal(mod_key, verdict="reject")]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert result.decision == "hide"


class TestWeightResolution:
    def test_subscribed_moderator_overrides_store(self):
        # Unknown in store but subscribed with weight 2 → moderator weight wins.
        mod_key = "mod"
        policy = Policy(
            subscribed_moderators=[
                ModeratorSubscription(pubkey=mod_key, weight=2.0)
            ],
            max_weight_per_peer=2.0,
            default_weight_unknown=0.1,
        )
        signals = [_signal(mod_key, verdict="reject")]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        # 2.0 * 3.0 * 1.0 = 6.0
        assert result.score == pytest.approx(6.0)

    def test_explicit_trust_overrides_learned(self):
        peer_key = "p"
        policy = default_policy()
        store = _store_with(
            {
                peer_key: {
                    "explicit_trust": 1.0,
                    "confirmed_signals": 99,
                    "disputed_signals": 0,
                    "last_seen": NOW,
                }
            }
        )
        signals = [_signal(peer_key, verdict="reject")]
        result = score_content(signals, store, policy, now=NOW)
        # Explicit 1.0 used even though learned would cap higher.
        assert result.breakdown[0].weight == 1.0

    def test_learned_weight_from_confirmed_history(self):
        peer_key = "p"
        policy = Policy(
            default_weight_unknown=0.1,
            learning_rate_per_signal=0.1,
            max_weight_per_peer=5.0,
        )
        store = _store_with(
            {
                peer_key: {
                    "explicit_trust": None,
                    "confirmed_signals": 5,
                    "disputed_signals": 1,
                    "last_seen": NOW,
                }
            }
        )
        signals = [_signal(peer_key, verdict="reject")]
        result = score_content(signals, store, policy, now=NOW)
        # 0.1 + 0.1 * (5 - 1) = 0.5
        assert result.breakdown[0].weight == pytest.approx(0.5)

    def test_weight_is_clamped_to_max(self):
        peer_key = "p"
        policy = Policy(
            default_weight_unknown=0.1,
            learning_rate_per_signal=1.0,
            max_weight_per_peer=1.5,
        )
        store = _store_with(
            {
                peer_key: {
                    "explicit_trust": None,
                    "confirmed_signals": 100,
                    "disputed_signals": 0,
                    "last_seen": NOW,
                }
            }
        )
        signals = [_signal(peer_key, verdict="reject")]
        result = score_content(signals, store, policy, now=NOW)
        assert result.breakdown[0].weight == 1.5

    def test_weight_is_clamped_to_zero_on_bad_history(self):
        peer_key = "p"
        policy = Policy(
            default_weight_unknown=0.1,
            learning_rate_per_signal=0.1,
        )
        store = _store_with(
            {
                peer_key: {
                    "explicit_trust": None,
                    "confirmed_signals": 0,
                    "disputed_signals": 100,
                    "last_seen": NOW,
                }
            }
        )
        signals = [_signal(peer_key, verdict="reject")]
        result = score_content(signals, store, policy, now=NOW)
        assert result.breakdown[0].weight == 0.0
        assert result.score == 0.0


class TestDecay:
    def test_decay_halves_at_one_half_life(self):
        policy = Policy(
            default_weight_unknown=1.0,
            decay_half_life_days=30.0,
            threshold_warn=100.0,  # avoid triggering
            threshold_hide=1000.0,
        )
        signals = [_signal("p", verdict="reject", timestamp=NOW - 30 * DAY)]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        # 1.0 * 3.0 * 0.5 = 1.5
        assert result.score == pytest.approx(1.5)

    def test_fresh_signal_no_decay(self):
        policy = Policy(
            default_weight_unknown=1.0,
            decay_half_life_days=30.0,
            threshold_warn=100.0,
            threshold_hide=1000.0,
        )
        signals = [_signal("p", verdict="reject", timestamp=NOW)]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert result.breakdown[0].decay == 1.0

    def test_timestamp_zero_treated_as_fresh(self):
        policy = Policy(default_weight_unknown=1.0)
        signals = [_signal("p", verdict="reject", timestamp=0)]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert result.breakdown[0].decay == 1.0

    def test_future_timestamp_treated_as_fresh(self):
        policy = Policy(default_weight_unknown=1.0)
        signals = [_signal("p", verdict="reject", timestamp=NOW + DAY)]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert result.breakdown[0].decay == 1.0


class TestAntiAmplification:
    def test_same_peer_counts_once(self):
        # 5 signals from same peer → still one contribution.
        policy = Policy(default_weight_unknown=1.0, threshold_warn=100.0, threshold_hide=1000.0)
        signals = [_signal("spammer", verdict="reject") for _ in range(5)]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert len(result.breakdown) == 1
        assert result.score == pytest.approx(3.0)

    def test_keeps_most_severe_signal_from_same_peer(self):
        policy = default_policy()
        signals = [
            _signal("peer", verdict="ok"),
            _signal("peer", verdict="reject"),
            _signal("peer", verdict="warn"),
        ]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert len(result.breakdown) == 1
        assert result.breakdown[0].verdict == "reject"

    def test_different_peers_each_contribute(self):
        policy = Policy(default_weight_unknown=1.0, threshold_warn=100.0, threshold_hide=1000.0)
        signals = [
            _signal("p1", verdict="reject"),
            _signal("p2", verdict="reject"),
            _signal("p3", verdict="reject"),
        ]
        result = score_content(signals, _store_with({}), policy, now=NOW)
        assert len(result.breakdown) == 3
        assert result.score == pytest.approx(9.0)
