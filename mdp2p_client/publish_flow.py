"""Shared publish and naming business logic used by interactive and CLI modes."""

import secrets
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr

# Root-level modules (bundle, peer, naming) live one directory up; make them
# importable when this module is loaded as part of the installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from bundle import generate_keypair, make_key_name
from naming import client_list
from peer import run_peer

from . import colors as c
from .config import ClientConfig, DEFAULT_CONFIG_DIR
from .formatting import strip_uri_scheme
from .i18n import t


def require_naming(config: ClientConfig) -> str:
    """Return the configured naming multiaddr or raise a helpful error."""
    if not config.naming_multiaddr:
        raise RuntimeError(
            "No naming server configured. Run `mdp2p setup --author NAME "
            "--naming /ip4/.../tcp/1707/p2p/12D3Koo...`."
        )
    return config.naming_multiaddr


async def do_publish(
    config: ClientConfig,
    uri: str,
    site_path: Path,
    categories: list[str] = None,
) -> None:
    """Core publish logic shared by interactive and CLI modes."""
    uri = strip_uri_scheme(uri)
    key_name = make_key_name(config.author, uri)
    keys_path = Path(config.keys_dir)
    keys_path.mkdir(parents=True, exist_ok=True)
    priv_path = keys_path / f"{key_name}.key"

    if priv_path.exists():
        print(f"  {c.DIM}{t('add_key_existing', path=priv_path)}{c.RESET}")
    else:
        priv_path_str, _ = generate_keypair(config.keys_dir, key_name)
        print(f"  {c.DIM}{t('add_key_generated', path=priv_path_str)}{c.RESET}")

    peer_data_dir = Path(config.data_dir) / key_name
    peer_data_dir.mkdir(parents=True, exist_ok=True)

    # Copy .md files from source to the seeds directory
    for md_file in site_path.rglob("*.md"):
        rel = md_file.relative_to(site_path)
        dest = peer_data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(md_file, dest)

    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(require_naming(config)))
    async with run_peer(
        data_dir=str(peer_data_dir),
        port=config.port,
        naming_info=naming_info,
    ) as peer:
        await peer.publish(
            uri,
            config.author,
            str(peer_data_dir),
            str(priv_path),
            categories=categories,
        )


@asynccontextmanager
async def ephemeral_host():
    """A short-lived libp2p host used for one-off RPC calls (browse, etc.)."""
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    listen = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        try:
            yield host
        finally:
            nursery.cancel_scope.cancel()


async def list_registered_sites(config: ClientConfig) -> list[dict]:
    """Query the configured naming server and return its list of records."""
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(require_naming(config)))
    async with ephemeral_host() as host:
        response = await client_list(host, naming_info)
        if response.get("type") != "names":
            raise RuntimeError(response.get("msg", "unknown naming error"))
        return response.get("records", [])


def get_pinstore_path() -> str:
    """Return the default pinstore path."""
    return str(DEFAULT_CONFIG_DIR / "known_keys.json")
