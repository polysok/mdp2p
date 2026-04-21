import time

import pytest

from bundle.crypto import (
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_to_b64,
)
from review.record import (
    build_review_record,
    build_reviewer_opt_in,
    sign_review_record,
    sign_reviewer_opt_in,
    verify_review_record,
    verify_reviewer_opt_in,
)


@pytest.fixture
def keypair(tmp_path):
    priv_path, pub_path = generate_keypair(str(tmp_path), "reviewer")
    priv = load_private_key(priv_path)
    pub_b64 = public_key_to_b64(load_public_key(pub_path))
    return priv, pub_b64


@pytest.fixture
def other_keypair(tmp_path):
    priv_path, pub_path = generate_keypair(str(tmp_path), "other")
    priv = load_private_key(priv_path)
    pub_b64 = public_key_to_b64(load_public_key(pub_path))
    return priv, pub_b64


class TestReviewerOptInRoundtrip:
    def test_build_and_verify(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, categories=["tech", "fr"])
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert ok, err

    def test_empty_categories_default(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64)
        assert record["categories"] == []
        signature = sign_reviewer_opt_in(record, priv)
        ok, _ = verify_reviewer_opt_in(record, signature)
        assert ok

    def test_sign_with_wrong_key_raises(self, keypair, other_keypair):
        _, pub_b64 = keypair
        other_priv, _ = other_keypair
        record = build_reviewer_opt_in(pub_b64)
        with pytest.raises(ValueError, match="does not match"):
            sign_reviewer_opt_in(record, other_priv)

    def test_tampered_categories_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, categories=["tech"])
        signature = sign_reviewer_opt_in(record, priv)
        record["categories"] = ["tech", "politics"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_missing_field_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64)
        signature = sign_reviewer_opt_in(record, priv)
        del record["categories"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "missing fields" in err

    def test_non_list_categories_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64)
        record["categories"] = "tech"
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "categories must be a list" in err

    def test_expired_timestamp_rejected(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_reviewer_opt_in(pub_b64, timestamp=old)
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "drift" in err

    def test_expired_timestamp_accepted_when_drift_disabled(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_reviewer_opt_in(pub_b64, timestamp=old)
        signature = sign_reviewer_opt_in(record, priv)
        ok, _ = verify_reviewer_opt_in(record, signature, max_drift=None)
        assert ok


class TestReviewRecordRoundtrip:
    def test_build_and_verify(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_record(
            content_key="/mdp2p/abc123",
            reviewer_pubkey_b64=pub_b64,
            verdict="ok",
            comment="reads fine",
        )
        signature = sign_review_record(record, priv)
        ok, err = verify_review_record(record, signature)
        assert ok, err

    def test_all_verdicts_accepted(self, keypair):
        priv, pub_b64 = keypair
        for verdict in ("ok", "warn", "reject"):
            record = build_review_record("c1", pub_b64, verdict)
            signature = sign_review_record(record, priv)
            ok, err = verify_review_record(record, signature)
            assert ok, f"{verdict}: {err}"

    def test_invalid_verdict_rejected_at_build(self, keypair):
        _, pub_b64 = keypair
        with pytest.raises(ValueError, match="verdict"):
            build_review_record("c1", pub_b64, "spam")

    def test_tampered_verdict_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_record("c1", pub_b64, "ok")
        signature = sign_review_record(record, priv)
        record["verdict"] = "reject"
        ok, err = verify_review_record(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_tampered_content_key_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_record("c1", pub_b64, "reject")
        signature = sign_review_record(record, priv)
        record["content_key"] = "c2"
        ok, _ = verify_review_record(record, signature)
        assert not ok

    def test_sign_with_wrong_key_raises(self, keypair, other_keypair):
        _, pub_b64 = keypair
        other_priv, _ = other_keypair
        record = build_review_record("c1", pub_b64, "ok")
        with pytest.raises(ValueError, match="does not match"):
            sign_review_record(record, other_priv)

    def test_missing_field_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_record("c1", pub_b64, "ok")
        signature = sign_review_record(record, priv)
        del record["comment"]
        ok, err = verify_review_record(record, signature)
        assert not ok
        assert "missing fields" in err

    def test_expired_timestamp_rejected(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_review_record("c1", pub_b64, "ok", timestamp=old)
        signature = sign_review_record(record, priv)
        ok, err = verify_review_record(record, signature)
        assert not ok
        assert "drift" in err

    def test_injected_invalid_verdict_rejected_at_verify(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_record("c1", pub_b64, "ok")
        signature = sign_review_record(record, priv)
        record["verdict"] = "banana"  # would only be injected post-signing
        ok, err = verify_review_record(record, signature)
        assert not ok
        assert "invalid verdict" in err
