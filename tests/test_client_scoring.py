"""Tests for mdp2p_client.scoring: offline TUI scoring from disk cache."""

import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import public_key_to_b64
from mdp2p_client.scoring import score_from_cache
from review import build_review_record, sign_review_record
from trust import ModeratorSubscription, Policy, save_policy


def _write_attachments(site_dir: Path, content_key: str, records_with_sigs: list[dict]) -> None:
    (site_dir / "attachments.json").write_text(
        json.dumps(
            {"content_key": content_key, "records": records_with_sigs},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _signed_attachment(content_key: str, verdict: str) -> tuple[dict, str]:
    priv = Ed25519PrivateKey.generate()
    pub = public_key_to_b64(priv.public_key())
    record = build_review_record(content_key, pub, verdict)
    signature = sign_review_record(record, priv)
    return {"record": record, "signature": signature}, pub


class TestScoreFromCache:
    def test_no_attachments_file_neutral_score(self, tmp_path):
        result = score_from_cache(str(tmp_path))
        assert result.score == 0.0
        assert result.decision == "show"

    def test_corrupt_attachments_treated_as_empty(self, tmp_path):
        (tmp_path / "attachments.json").write_text("not-json{", encoding="utf-8")
        result = score_from_cache(str(tmp_path))
        assert result.score == 0.0
        assert result.decision == "show"

    def test_single_valid_attachment_scored(self, tmp_path):
        content_key = "ck1"
        policy_path = tmp_path / "policy.json"
        save_policy(
            Policy(
                threshold_warn=0.2,
                threshold_hide=10.0,
                default_weight_unknown=0.5,
            ),
            str(policy_path),
        )
        entry, _ = _signed_attachment(content_key, "reject")
        _write_attachments(tmp_path, content_key, [entry])

        result = score_from_cache(str(tmp_path), policy_path=str(policy_path))
        assert result.score == pytest.approx(0.5 * 3.0)
        assert result.decision == "warn"

    def test_subscribed_moderator_dominates(self, tmp_path):
        content_key = "ck2"
        entry, mod_pub = _signed_attachment(content_key, "reject")
        policy_path = tmp_path / "policy.json"
        save_policy(
            Policy(
                threshold_warn=1.0,
                threshold_hide=2.0,
                subscribed_moderators=[
                    ModeratorSubscription(pubkey=mod_pub, weight=1.0)
                ],
            ),
            str(policy_path),
        )
        _write_attachments(tmp_path, content_key, [entry])

        result = score_from_cache(str(tmp_path), policy_path=str(policy_path))
        # 1.0 * severity(reject)=3.0 → decision=hide
        assert result.decision == "hide"

    def test_tampered_record_dropped(self, tmp_path):
        content_key = "ck3"
        entry, _ = _signed_attachment(content_key, "ok")
        entry["record"]["verdict"] = "reject"  # post-sign tamper
        _write_attachments(tmp_path, content_key, [entry])
        result = score_from_cache(str(tmp_path))
        assert result.score == 0.0
        assert result.decision == "show"

    def test_mismatched_content_key_dropped(self, tmp_path):
        # Attachment file claims content_key="ck-a" but stored records
        # are for "ck-b" — the adapter must refuse to mix them.
        entry, _ = _signed_attachment("ck-b", "reject")
        _write_attachments(tmp_path, "ck-a", [entry])
        result = score_from_cache(str(tmp_path))
        assert result.score == 0.0

    def test_malformed_entries_ignored(self, tmp_path):
        entry, _ = _signed_attachment("ck4", "reject")
        _write_attachments(
            tmp_path,
            "ck4",
            [None, {}, {"record": "not-a-dict"}, {"record": {}}, entry],
        )
        # Only the valid entry survives; default policy leaves it below warn.
        result = score_from_cache(str(tmp_path))
        assert len(result.breakdown) == 1
