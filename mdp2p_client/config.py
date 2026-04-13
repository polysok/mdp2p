"""
MDP2P Client Config — Configuration management for the MDP2P client.

Stores author identity, tracker settings, and local seeding data.
"""

import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG_DIR = Path.home() / ".mdp2p"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_DATA_DIR = DEFAULT_CONFIG_DIR / "sites"


@dataclass
class ClientConfig:
    """Client configuration."""

    author: str
    tracker_host: str = "localhost"
    tracker_port: int = 1707
    keys_dir: str = str(DEFAULT_CONFIG_DIR / "keys")
    data_dir: str = str(DEFAULT_DATA_DIR)
    port: int = 0
    language: str = "fr"

    def save(self, path: Optional[Path] = None):
        """Save configuration to file."""
        path = path or DEFAULT_CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> Optional["ClientConfig"]:
        """Load configuration from file. Returns None if not found."""
        path = path or DEFAULT_CONFIG_FILE
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return cls(**data)


@dataclass
class SeededSite:
    """Information about a locally seeded site."""

    uri: str
    author: str
    site_dir: str
    key_path: str
    manifest_timestamp: int
    file_count: int
    total_size: int
    public_key: str = ""


def get_seeded_sites(data_dir: str = str(DEFAULT_DATA_DIR)) -> list[SeededSite]:
    """Get list of all locally seeded sites."""
    data_path = Path(data_dir)
    sites = []

    if not data_path.exists():
        return sites

    for site_dir in data_path.iterdir():
        if not site_dir.is_dir():
            continue

        manifest_path = site_dir / "manifest.json"
        sig_path = site_dir / "manifest.sig"

        if not manifest_path.exists() or not sig_path.exists():
            continue

        try:
            manifest = json.loads(manifest_path.read_text())
            sites.append(
                SeededSite(
                    uri=manifest.get("uri", site_dir.name),
                    author=manifest.get("author", "unknown"),
                    site_dir=str(site_dir),
                    key_path="",  # Will be populated from config
                    manifest_timestamp=manifest.get("timestamp", 0),
                    file_count=manifest.get("file_count", 0),
                    total_size=manifest.get("total_size", 0),
                    public_key=manifest.get("public_key", ""),
                )
            )
        except (json.JSONDecodeError, KeyError):
            continue

    return sorted(sites, key=lambda s: s.uri)


def remove_seeded_site(uri: str, data_dir: str = str(DEFAULT_DATA_DIR)) -> bool:
    """Remove a seeded site and its local content."""
    data_path = Path(data_dir)

    for site_dir in data_path.iterdir():
        if not site_dir.is_dir():
            continue

        manifest_path = site_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                if manifest.get("uri") == uri:
                    shutil.rmtree(site_dir)
                    return True
            except json.JSONDecodeError:
                continue

    return False


def ensure_config(config: Optional[ClientConfig] = None) -> ClientConfig:
    """Ensure a valid configuration exists. Create default if needed."""
    if config is None:
        config = ClientConfig(author="anonymous")

    Path(config.keys_dir).mkdir(parents=True, exist_ok=True)
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)

    return config
