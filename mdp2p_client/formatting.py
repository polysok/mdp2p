"""Shared formatting helpers for the mdp2p client (URIs, sizes, timestamps)."""

from __future__ import annotations

from datetime import datetime

MD_SCHEME_PREFIX = "md://"


def strip_uri_scheme(uri: str) -> str:
    """Remove the md:// prefix if present, returning only the bare identifier."""
    if uri.startswith(MD_SCHEME_PREFIX):
        return uri[len(MD_SCHEME_PREFIX):]
    return uri


def format_size(size: int) -> str:
    """Format size in bytes to a human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_timestamp(ts: int) -> str:
    """Format a Unix timestamp to a readable date, or an em dash if zero."""
    if ts == 0:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
