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
    build_review_record,
    build_reviewer_opt_in,
    sign_review_record,
    sign_reviewer_opt_in,
    verify_review_record,
    verify_reviewer_opt_in,
)
from review.selection import select_reviewers

__all__ = [
    "MAX_REVIEW_DRIFT_SECONDS",
    "build_review_record",
    "build_reviewer_opt_in",
    "select_reviewers",
    "sign_review_record",
    "sign_reviewer_opt_in",
    "verify_review_record",
    "verify_reviewer_opt_in",
]
