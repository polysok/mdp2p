"""Tests for review.taxonomy: the closed list of category slugs."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.taxonomy import (
    CATEGORY_SLUGS,
    is_valid_slug,
    label,
    labeled_categories,
    validate_categories,
)


class TestCatalog:
    def test_exact_size(self):
        # Guards against accidental removals which would break existing
        # signed manifests referencing the slug.
        assert len(CATEGORY_SLUGS) == 30

    def test_slugs_are_unique(self):
        assert len(set(CATEGORY_SLUGS)) == len(CATEGORY_SLUGS)

    def test_slugs_are_ascii_snake_case(self):
        for slug in CATEGORY_SLUGS:
            assert slug == slug.lower()
            assert slug.replace("_", "").isalnum()

    def test_other_present(self):
        # "other" is the fallback for uncategorized content; removing it
        # would break create_manifest's default.
        assert "other" in CATEGORY_SLUGS


class TestIsValidSlug:
    def test_known_slugs(self):
        for slug in CATEGORY_SLUGS:
            assert is_valid_slug(slug)

    def test_unknown_slugs(self):
        assert not is_valid_slug("tech")
        assert not is_valid_slug("COMPUTING")
        assert not is_valid_slug("")


class TestValidateCategories:
    def test_empty_list_passes(self):
        validate_categories([])

    def test_all_known_passes(self):
        validate_categories(["math", "physics", "other"])

    def test_single_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            validate_categories(["sorcery"])

    def test_mixed_known_and_unknown_raises(self):
        with pytest.raises(ValueError, match="sorcery"):
            validate_categories(["math", "sorcery"])

    def test_duplicates_in_error_message_deduplicated(self):
        with pytest.raises(ValueError) as info:
            validate_categories(["sorcery", "sorcery"])
        assert info.value.args[0].count("sorcery") == 1


class TestLabel:
    def test_french_label(self):
        assert label("math", "fr") == "Mathématiques"
        assert label("computing", "fr") == "Informatique"

    def test_english_label(self):
        assert label("math", "en") == "Mathematics"

    def test_chinese_label(self):
        assert label("math", "zh") == "数学"

    def test_arabic_label(self):
        assert label("math", "ar").strip() != ""

    def test_hindi_label(self):
        assert label("math", "hi").strip() != ""

    def test_unknown_language_falls_back_to_english(self):
        assert label("math", "xx") == label("math", "en")

    def test_unknown_slug_returns_slug_itself(self):
        assert label("sorcery", "fr") == "sorcery"


class TestLabeledCategories:
    def test_returns_all_slugs_in_canonical_order(self):
        entries = labeled_categories("fr")
        assert [slug for slug, _ in entries] == list(CATEGORY_SLUGS)

    def test_each_entry_has_nonempty_label(self):
        for lang in ("fr", "en", "zh", "ar", "hi"):
            for slug, lbl in labeled_categories(lang):
                assert lbl and lbl.strip(), f"empty label for {slug}/{lang}"
