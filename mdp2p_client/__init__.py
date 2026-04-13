"""
MDP2P Client — Terminal interface for managing seeded sites.
"""

from .config import ClientConfig, get_seeded_sites, remove_seeded_site, ensure_config

__all__ = ["ClientConfig", "get_seeded_sites", "remove_seeded_site", "ensure_config"]
