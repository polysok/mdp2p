import pytest

from review.selection import select_reviewers


POOL_10 = [f"pubkey_{i:02d}" for i in range(10)]


class TestBasicBehavior:
    def test_empty_pool_returns_empty(self):
        assert select_reviewers("c1", [], 3) == []

    def test_zero_n_returns_empty(self):
        assert select_reviewers("c1", POOL_10, 0) == []

    def test_negative_n_returns_empty(self):
        assert select_reviewers("c1", POOL_10, -1) == []

    def test_n_larger_than_pool_returns_all(self):
        result = select_reviewers("c1", POOL_10[:3], 10)
        assert sorted(result) == sorted(POOL_10[:3])

    def test_exact_n_match(self):
        result = select_reviewers("c1", POOL_10, 3)
        assert len(result) == 3
        assert set(result).issubset(set(POOL_10))


class TestDeterminism:
    def test_same_inputs_same_output(self):
        a = select_reviewers("content-xyz", POOL_10, 3)
        b = select_reviewers("content-xyz", POOL_10, 3)
        assert a == b

    def test_different_content_keys_yield_different_rankings(self):
        # It's astronomically unlikely both keys rank the same 3 in the
        # same order across a pool of 10.
        a = select_reviewers("content-a", POOL_10, 3)
        b = select_reviewers("content-b", POOL_10, 3)
        assert a != b

    def test_pool_order_does_not_matter(self):
        a = select_reviewers("c1", POOL_10, 3)
        b = select_reviewers("c1", list(reversed(POOL_10)), 3)
        assert a == b


class TestDeduplication:
    def test_duplicate_entries_do_not_inflate_selection(self):
        pool = ["k1", "k1", "k1", "k2", "k3"]
        result = select_reviewers("c1", pool, 5)
        assert len(result) == 3
        assert sorted(result) == ["k1", "k2", "k3"]

    def test_duplicate_has_no_extra_selection_probability(self):
        # If "hot" was counted 5x it would usually win top-1 in a
        # pool of 10 unique + 5 clones, but dedup prevents that.
        hot = "hot"
        pool = [hot] * 5 + POOL_10
        counts = {k: 0 for k in set(pool)}
        for i in range(200):
            picked = select_reviewers(f"c{i}", pool, 1)
            counts[picked[0]] += 1
        # No item should dominate dramatically; the hot key's count should be
        # comparable to the others — not 5x as large.
        mean = sum(counts.values()) / len(counts)
        assert counts[hot] < 3 * mean


class TestDistribution:
    def test_approximately_uniform_over_many_content_keys(self):
        trials = 2000
        counts = {k: 0 for k in POOL_10}
        for i in range(trials):
            for picked in select_reviewers(f"content-{i}", POOL_10, 1):
                counts[picked] += 1
        expected = trials / len(POOL_10)
        # Each reviewer should fall within ±30 % of the expected share.
        for k, c in counts.items():
            assert abs(c - expected) < 0.3 * expected, (k, c, expected)


class TestVerifiability:
    def test_selection_reproducible_from_public_inputs_only(self):
        # A verifier that only knows (content_key, pool, n) can always
        # recompute the selection without any secret state.
        content_key = "/mdp2p/some-digest"
        pool = POOL_10
        n = 3
        publisher_view = select_reviewers(content_key, pool, n)
        verifier_view = select_reviewers(content_key, pool, n)
        assert publisher_view == verifier_view
