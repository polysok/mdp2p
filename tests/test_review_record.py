import time

import pytest

from bundle.crypto import (
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_to_b64,
)
from review.record import (
    build_review_assignment,
    build_review_record,
    build_reviewer_opt_in,
    sign_review_assignment,
    sign_review_record,
    sign_reviewer_opt_in,
    verify_review_assignment,
    verify_review_record,
    verify_reviewer_opt_in,
)


PEER_ID = "12D3KooWTestPeerIdForReviewerRecords111111"
ADDRS = ["/ip4/127.0.0.1/tcp/4001"]


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
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["tech", "fr"])
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert ok, err

    def test_empty_categories_default(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        assert record["categories"] == []
        signature = sign_reviewer_opt_in(record, priv)
        ok, _ = verify_reviewer_opt_in(record, signature)
        assert ok

    def test_sign_with_wrong_key_raises(self, keypair, other_keypair):
        _, pub_b64 = keypair
        other_priv, _ = other_keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        with pytest.raises(ValueError, match="does not match"):
            sign_reviewer_opt_in(record, other_priv)

    def test_tampered_categories_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["tech"])
        signature = sign_reviewer_opt_in(record, priv)
        record["categories"] = ["tech", "politics"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_missing_field_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        signature = sign_reviewer_opt_in(record, priv)
        del record["categories"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "missing fields" in err

    def test_non_list_categories_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        record["categories"] = "tech"
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "categories must be a list" in err

    def test_expired_timestamp_rejected(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, timestamp=old)
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "drift" in err

    def test_expired_timestamp_accepted_when_drift_disabled(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, timestamp=old)
        signature = sign_reviewer_opt_in(record, priv)
        ok, _ = verify_reviewer_opt_in(record, signature, max_drift=None)
        assert ok

    def test_tampered_addrs_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        signature = sign_reviewer_opt_in(record, priv)
        record["addrs"] = ["/ip4/evil.example/tcp/1"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_tampered_peer_id_fails_verification(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        signature = sign_reviewer_opt_in(record, priv)
        record["peer_id"] = "12D3KooWOtherPeerIdImpostor111111111111111"
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_missing_addrs_field_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        signature = sign_reviewer_opt_in(record, priv)
        del record["addrs"]
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "missing fields" in err

    def test_non_list_addrs_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
        record["addrs"] = "/ip4/127.0.0.1/tcp/4001"
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "addrs must be a list" in err

    def test_empty_peer_id_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_reviewer_opt_in(pub_b64, "", ADDRS)
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert not ok
        assert "peer_id must be a non-empty string" in err

    def test_empty_addrs_list_allowed(self, keypair):
        priv, pub_b64 = keypair
        # A reviewer with no known addrs is valid; the publisher just
        # won't be able to dial them until they re-announce with addrs.
        record = build_reviewer_opt_in(pub_b64, PEER_ID, [])
        signature = sign_reviewer_opt_in(record, priv)
        ok, err = verify_reviewer_opt_in(record, signature)
        assert ok, err


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


class TestReviewAssignmentRoundtrip:
    DEADLINE = int(time.time()) + 3 * 86400  # 3 days out

    def test_build_and_verify(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_assignment(
            uri="blog",
            publisher_pubkey_b64=pub_b64,
            reviewer_pubkeys_b64=["r1", "r2", "r3"],
            deadline=self.DEADLINE,
        )
        signature = sign_review_assignment(record, priv)
        ok, err = verify_review_assignment(record, signature)
        assert ok, err

    def test_tampered_reviewer_list_fails(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_assignment(
            "blog", pub_b64, ["r1", "r2"], self.DEADLINE
        )
        signature = sign_review_assignment(record, priv)
        record["reviewer_public_keys"] = ["r1", "r2", "attacker"]
        ok, err = verify_review_assignment(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_tampered_deadline_fails(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_assignment(
            "blog", pub_b64, ["r1"], self.DEADLINE
        )
        signature = sign_review_assignment(record, priv)
        record["deadline"] = self.DEADLINE + 365 * 86400
        ok, err = verify_review_assignment(record, signature)
        assert not ok
        assert err == "invalid signature"

    def test_sign_with_wrong_key_raises(self, keypair, other_keypair):
        _, pub_b64 = keypair
        other_priv, _ = other_keypair
        record = build_review_assignment(
            "blog", pub_b64, ["r1"], self.DEADLINE
        )
        with pytest.raises(ValueError, match="does not match"):
            sign_review_assignment(record, other_priv)

    def test_zero_deadline_rejected_at_build(self, keypair):
        _, pub_b64 = keypair
        with pytest.raises(ValueError, match="deadline"):
            build_review_assignment("blog", pub_b64, ["r1"], deadline=0)

    def test_missing_field_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_assignment(
            "blog", pub_b64, ["r1"], self.DEADLINE
        )
        signature = sign_review_assignment(record, priv)
        del record["deadline"]
        ok, err = verify_review_assignment(record, signature)
        assert not ok
        assert "missing fields" in err

    def test_non_list_reviewers_rejected(self, keypair):
        priv, pub_b64 = keypair
        record = build_review_assignment(
            "blog", pub_b64, ["r1"], self.DEADLINE
        )
        record["reviewer_public_keys"] = "r1"
        signature = sign_review_assignment(record, priv)
        ok, err = verify_review_assignment(record, signature)
        assert not ok
        assert "reviewer_public_keys must be a list" in err

    def test_expired_timestamp_rejected(self, keypair):
        priv, pub_b64 = keypair
        old = int(time.time()) - 10_000
        record = build_review_assignment(
            "blog", pub_b64, ["r1"], self.DEADLINE, timestamp=old
        )
        signature = sign_review_assignment(record, priv)
        ok, err = verify_review_assignment(record, signature)
        assert not ok
        assert "drift" in err
