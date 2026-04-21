"""
MDP2P Review — Data formats and selection for peer review.

This package holds the protocol-agnostic pieces of the review system:

  - record.py: signed records for reviewer opt-in and for a review itself.
  - selection.py: deterministic reviewer selection from a pool, verifiable
    by any peer that knows the current pool.

Wire protocol and naming-server integration live in peer/ and naming.py.
"""

from review.record import (
    MAX_REVIEW_DRIFT_SECONDS,
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
from review.selection import select_reviewers
from review.taxonomy import (
    CATEGORY_SLUGS,
    is_valid_slug,
    label,
    labeled_categories,
    validate_categories,
)

__all__ = [
    "CATEGORY_SLUGS",
    "MAX_REVIEW_DRIFT_SECONDS",
    "build_review_assignment",
    "build_review_record",
    "build_reviewer_opt_in",
    "is_valid_slug",
    "label",
    "labeled_categories",
    "select_reviewers",
    "sign_review_assignment",
    "sign_review_record",
    "sign_reviewer_opt_in",
    "validate_categories",
    "verify_review_assignment",
    "verify_review_record",
    "verify_reviewer_opt_in",
]
