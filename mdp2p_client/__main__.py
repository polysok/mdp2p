#!/usr/bin/env python3
"""
MDP2P Client — Interactive terminal interface for managing seeded sites.

Usage:
    mdp2p                  # Interactive mode
    mdp2p list             # List all seeded sites
    mdp2p browse           # List all sites registered on the naming server
    mdp2p pins             # List pinned public keys (TOFU)
    mdp2p unpin blog       # Remove a pinned key
    mdp2p publish --uri blog --site ./mon_site       # or --uri md://blog
    mdp2p remove blog                                # or md://blog
    mdp2p setup --author alice --naming /ip4/1.2.3.4/tcp/1707/p2p/12D3Koo...
    mdp2p status
"""

import argparse
import getpass
import secrets
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr

sys.path.insert(0, str(Path(__file__).parent.parent))

from bundle import generate_keypair, make_key_name
from naming import client_list
from peer import run_peer
from pinstore import load_pinstore, unpin_key

from .config import (
    ClientConfig,
    SeededSite,
    get_seeded_sites,
    ensure_config,
    DEFAULT_CONFIG_FILE,
    DEFAULT_CONFIG_DIR,
)
from . import colors as c
from .i18n import t, load_language, current_language

SUPPORTED_LANGUAGES = ["fr", "en", "zh", "ar", "hi"]

MD_SCHEME_PREFIX = "md://"


def strip_uri_scheme(uri: str) -> str:
    """Remove the md:// prefix if present, returning only the bare identifier."""
    if uri.startswith(MD_SCHEME_PREFIX):
        return uri[len(MD_SCHEME_PREFIX):]
    return uri


# ── UI helpers ──────────────────────────────────────────────────────


def clear_screen():
    """Clear the terminal screen."""
    print("\033[2J\033[H", end="", flush=True)


def format_size(size: int) -> str:
    """Format size in bytes to human readable."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_timestamp(ts: int) -> str:
    """Format Unix timestamp to readable date."""
    if ts == 0:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def print_banner():
    """Print the application banner."""
    title = t("app_title")
    width = max(len(title) + 6, 48)
    print()
    print(f"  {c.CYAN}{c.BOLD}{'═' * width}{c.RESET}")
    print(f"  {c.CYAN}{c.BOLD}   {title.center(width - 6)}   {c.RESET}")
    print(f"  {c.CYAN}{c.BOLD}{'═' * width}{c.RESET}")


def print_menu(seed_count: int):
    """Print the interactive menu."""
    print()
    print(f"  {c.DIM}{t('active_seeds')} : {c.RESET}{c.BOLD}{seed_count}{c.RESET}")
    print()
    print(f"  {c.BRIGHT_CYAN}[1]{c.RESET} {t('menu_list')}")
    print(f"  {c.BRIGHT_GREEN}[2]{c.RESET} {t('menu_add')}")
    print(f"  {c.BRIGHT_RED}[3]{c.RESET} {t('menu_remove')}")
    print(f"  {c.BRIGHT_BLUE}[4]{c.RESET} {t('menu_browse')}")
    print(f"  {c.BRIGHT_YELLOW}[5]{c.RESET} {t('menu_config')}")
    print(f"  {c.DIM}[6]{c.RESET} {t('menu_pins')}")
    print(f"  {c.DIM}[r]{c.RESET} {t('menu_refresh')}")
    print(f"  {c.DIM}[q]{c.RESET} {t('menu_quit')}")
    print()


def print_seeds_table(sites: list[SeededSite]):
    """Print a colored table of seeded sites."""
    if not sites:
        print(f"\n  {c.YELLOW}{t('no_seeds')}{c.RESET}")
        print(f"  {c.DIM}{t('no_seeds_hint')}{c.RESET}")
        return

    headers = [
        t("col_site"),
        t("col_size"),
        t("col_files"),
        t("col_published"),
        t("col_path"),
    ]
    rows = []
    for site in sites:
        key_hint = f" ({site.public_key[:8]})" if site.public_key else ""
        rows.append([
            f"{site.author}{key_hint}@{site.uri}",
            format_size(site.total_size),
            t("files_unit", count=site.file_count),
            format_timestamp(site.manifest_timestamp),
            site.site_dir,
        ])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    print()
    header_line = "  " + " │ ".join(
        f"{c.BOLD}{h.ljust(col_widths[i])}{c.RESET}" for i, h in enumerate(headers)
    )
    print(header_line)
    separator = "─┼─".join("─" * w for w in col_widths)
    print(f"  {c.DIM}{separator}{c.RESET}")

    for row in rows:
        cells = [
            f"{c.MAGENTA}{row[0].ljust(col_widths[0])}{c.RESET}",
            f"{c.BLUE}{row[1].ljust(col_widths[1])}{c.RESET}",
            f"{row[2].ljust(col_widths[2])}",
            f"{c.DIM}{row[3].ljust(col_widths[3])}{c.RESET}",
            f"{c.DIM}{row[4].ljust(col_widths[4])}{c.RESET}",
        ]
        print("  " + " │ ".join(cells))

    print(f"\n  {t('total_seeds', count=len(sites))}")


def print_browse_table(sites: list[dict]):
    """Print a colored table of sites registered on the naming server."""
    if not sites:
        print(f"\n  {c.YELLOW}{t('browse_no_sites')}{c.RESET}")
        return

    headers = [
        t("col_uri"),
        t("col_author"),
        t("col_published"),
    ]
    rows = []
    for site in sites:
        author = site.get("author", "?")
        raw_key = site.get("public_key", "")
        key_hint = f" ({raw_key[:8]})" if raw_key else ""
        rows.append([
            f"md://{site['uri']}",
            f"{author}{key_hint}",
            format_timestamp(site.get("timestamp", 0)),
        ])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    print()
    print(f"  {c.BOLD}{c.CYAN}── {t('browse_header')} ──{c.RESET}\n")
    header_line = "  " + " │ ".join(
        f"{c.BOLD}{h.ljust(col_widths[i])}{c.RESET}" for i, h in enumerate(headers)
    )
    print(header_line)
    separator = "─┼─".join("─" * w for w in col_widths)
    print(f"  {c.DIM}{separator}{c.RESET}")

    for row in rows:
        cells = [
            f"{c.MAGENTA}{row[0].ljust(col_widths[0])}{c.RESET}",
            f"{c.CYAN}{row[1].ljust(col_widths[1])}{c.RESET}",
            f"{c.DIM}{row[2].ljust(col_widths[2])}{c.RESET}",
        ]
        print("  " + " │ ".join(cells))

    print(f"\n  {t('browse_total', count=len(sites))}")


def prompt_input(label: str, hint: str = "") -> str:
    """Prompt for user input with colored label."""
    hint_text = f" {c.DIM}{hint}{c.RESET}" if hint else ""
    try:
        return input(f"  {c.CYAN}{label}{c.RESET}{hint_text} : ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _resolve_chown_target(error_path: str) -> str:
    """Determine the root directory to chown from the failing file path."""
    config_dir = str(DEFAULT_CONFIG_DIR)
    if error_path.startswith(config_dir):
        return config_dir
    p = Path(error_path)
    return str(p.parent if not p.is_dir() else p)


def fix_permissions(e: PermissionError) -> bool:
    """Offer to fix permissions via sudo chown. Returns True if fixed."""
    error_path = e.filename or str(e)
    target = _resolve_chown_target(error_path)

    print(f"\n  {c.YELLOW}{t('perm_error', path=error_path)}{c.RESET}")
    confirm = prompt_input(t("perm_fix_prompt"))
    if confirm.lower() != t("confirm_yes"):
        return False

    print(f"  {c.DIM}{t('perm_fixing')}{c.RESET}")
    user = getpass.getuser()
    result = subprocess.run(
        ["sudo", "chown", "-R", f"{user}:staff", target],
        stdin=sys.stdin,
    )
    if result.returncode == 0:
        print(f"  {c.GREEN}{t('perm_fixed')}{c.RESET}")
        return True

    print(f"  {c.RED}{t('perm_fix_failed')}{c.RESET}")
    return False


# ── Publish helper ─────────────────────────────────────────────────


def _require_naming(config: ClientConfig) -> str:
    if not config.naming_multiaddr:
        raise RuntimeError(
            "No naming server configured. Run `mdp2p setup --author NAME "
            "--naming /ip4/.../tcp/1707/p2p/12D3Koo...`."
        )
    return config.naming_multiaddr


async def _do_publish(config: ClientConfig, uri: str, site_path: Path) -> None:
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

    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(_require_naming(config)))
    async with run_peer(
        data_dir=str(peer_data_dir),
        port=config.port,
        naming_info=naming_info,
    ) as peer:
        await peer.publish(uri, config.author, str(peer_data_dir), str(priv_path))


@asynccontextmanager
async def _ephemeral_host():
    """A short-lived libp2p host used for one-off RPC calls (browse, etc.)."""
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    listen = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        try:
            yield host
        finally:
            nursery.cancel_scope.cancel()


async def _list_registered_sites(config: ClientConfig) -> list[dict]:
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(_require_naming(config)))
    async with _ephemeral_host() as host:
        response = await client_list(host, naming_info)
        if response.get("type") != "names":
            raise RuntimeError(response.get("msg", "unknown naming error"))
        return response.get("records", [])


# ── Interactive actions ─────────────────────────────────────────────


async def action_add(config: ClientConfig):
    """Interactive add/publish a seed."""
    print(f"\n  {c.BOLD}{c.GREEN}── {t('menu_add')} ──{c.RESET}\n")

    uri = prompt_input(t("add_uri_prompt"), t("cancel_hint"))
    if not uri:
        return

    site_dir_str = prompt_input(t("add_site_prompt"), t("cancel_hint"))
    if not site_dir_str:
        return

    site_path = Path(site_dir_str).expanduser().resolve()
    if not site_path.exists():
        print(f"\n  {c.RED}{t('add_error_no_dir', path=site_path)}{c.RESET}")
        return

    md_files = list(site_path.rglob("*.md"))
    if not md_files:
        print(f"\n  {c.RED}{t('add_error_no_md', path=site_path)}{c.RESET}")
        return

    print(f"\n  {t('add_publishing', author=config.author, uri=uri)}")
    print(f"  {c.DIM}{t('add_naming', addr=config.naming_multiaddr or '—')}{c.RESET}")
    print(f"  {c.DIM}{t('add_md_found', count=len(md_files))}{c.RESET}")

    for attempt in range(2):
        try:
            await _do_publish(config, uri, site_path)
            print(f"\n  {c.GREEN}{c.BOLD}{t('add_success')}{c.RESET}")
            return
        except PermissionError as e:
            if attempt == 0 and fix_permissions(e):
                continue
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return
        except Exception as e:
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return


def _get_pinstore_path() -> str:
    """Return the default pinstore path."""
    return str(DEFAULT_CONFIG_DIR / "known_keys.json")


def print_pins_table(pinstore: dict):
    """Print a colored table of pinned keys."""
    if not pinstore:
        print(f"\n  {c.YELLOW}{t('pins_no_keys')}{c.RESET}")
        return

    headers = [
        t("col_uri"),
        t("col_author"),
        t("col_key"),
        t("pins_first_seen"),
        t("pins_last_seen"),
    ]
    rows = []
    for uri, entry in sorted(pinstore.items()):
        rows.append([
            f"md://{uri}",
            entry.get("author", "?"),
            entry["public_key"][:16] + "...",
            format_timestamp(entry.get("first_seen", 0)),
            format_timestamp(entry.get("last_seen", 0)),
        ])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    print()
    print(f"  {c.BOLD}{c.CYAN}── {t('pins_header')} ──{c.RESET}\n")
    header_line = "  " + " │ ".join(
        f"{c.BOLD}{h.ljust(col_widths[i])}{c.RESET}" for i, h in enumerate(headers)
    )
    print(header_line)
    separator = "─┼─".join("─" * w for w in col_widths)
    print(f"  {c.DIM}{separator}{c.RESET}")

    for row in rows:
        cells = [
            f"{c.MAGENTA}{row[0].ljust(col_widths[0])}{c.RESET}",
            f"{c.CYAN}{row[1].ljust(col_widths[1])}{c.RESET}",
            f"{c.DIM}{row[2].ljust(col_widths[2])}{c.RESET}",
            f"{c.DIM}{row[3].ljust(col_widths[3])}{c.RESET}",
            f"{c.DIM}{row[4].ljust(col_widths[4])}{c.RESET}",
        ]
        print("  " + " │ ".join(cells))

    print(f"\n  {t('pins_total', count=len(pinstore))}")


def action_pins():
    """Interactive: view and manage pinned keys."""
    path = _get_pinstore_path()
    pinstore = load_pinstore(path)
    print_pins_table(pinstore)

    if not pinstore:
        return

    print()
    choice = prompt_input(t("unpin_prompt"), t("cancel_hint"))
    if not choice:
        return

    uri_to_unpin = None
    sorted_uris = sorted(pinstore.keys())
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(sorted_uris):
            uri_to_unpin = sorted_uris[idx]
    else:
        bare = strip_uri_scheme(choice)
        if bare in pinstore:
            uri_to_unpin = bare

    if not uri_to_unpin:
        print(f"\n  {c.RED}{t('unpin_not_found', uri=choice)}{c.RESET}")
        return

    print(f"\n  {c.YELLOW}{c.BOLD}{t('unpin_warning')}{c.RESET}")
    confirm = prompt_input(t("unpin_confirm", uri=uri_to_unpin))
    if confirm.lower() != t("confirm_yes"):
        print(f"\n  {c.DIM}{t('unpin_cancelled')}{c.RESET}")
        return

    if unpin_key(uri_to_unpin, path):
        print(f"\n  {c.GREEN}{t('unpin_success', uri=uri_to_unpin)}{c.RESET}")
    else:
        print(f"\n  {c.RED}{t('unpin_not_found', uri=uri_to_unpin)}{c.RESET}")


async def action_browse(config: ClientConfig):
    """Interactive browse: list all sites registered on the naming server."""
    try:
        records = await _list_registered_sites(config)
        print_browse_table(records)
    except Exception as e:
        print(f"\n  {c.RED}{t('browse_error', error=e)}{c.RESET}")


async def action_remove(config: ClientConfig):
    """Interactive remove a seed."""
    sites = get_seeded_sites(config.data_dir)

    if not sites:
        print(f"\n  {c.YELLOW}{t('no_seeds')}{c.RESET}")
        return

    print(f"\n  {c.BOLD}{c.RED}── {t('menu_remove')} ──{c.RESET}\n")

    for i, site in enumerate(sites, 1):
        key_hint = f" ({site.public_key[:8]})" if site.public_key else ""
        print(
            f"  {c.DIM}[{i}]{c.RESET} {c.MAGENTA}{site.author}{key_hint}@{site.uri}{c.RESET}"
            f"  {c.DIM}({format_size(site.total_size)}, "
            f"{t('files_unit', count=site.file_count)}){c.RESET}"
        )

    print()
    choice = prompt_input(t("remove_uri_prompt"), t("cancel_hint"))
    if not choice:
        return

    site_to_remove = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(sites):
            site_to_remove = sites[idx]
    else:
        for site in sites:
            if site.uri == choice:
                site_to_remove = site
                break

    if not site_to_remove:
        print(f"\n  {c.RED}{t('remove_not_found', uri=choice)}{c.RESET}")
        return

    print(f"\n  {c.YELLOW}{c.BOLD}{t('remove_warning')}{c.RESET}")
    print(f"  {t('remove_info_size', size=format_size(site_to_remove.total_size))}")
    print(f"  {t('remove_info_files', count=site_to_remove.file_count)}")
    print()

    confirm = prompt_input(t("remove_confirm", uri=site_to_remove.uri))
    if confirm.lower() != t("confirm_yes"):
        print(f"\n  {c.DIM}{t('remove_cancelled')}{c.RESET}")
        return

    try:
        shutil.rmtree(site_to_remove.site_dir)
        print(f"\n  {c.GREEN}{t('remove_success')}{c.RESET}")
    except Exception as e:
        print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")


def action_config(config: Optional[ClientConfig]) -> ClientConfig:
    """Interactive configuration editor."""
    print(f"\n  {c.BOLD}{c.YELLOW}── {t('config_header')} ──{c.RESET}\n")

    if config:
        sites = get_seeded_sites(config.data_dir)
        print(f"  {c.BOLD}{t('config_author'):<14}{c.RESET} : {c.CYAN}{config.author}{c.RESET}")
        print(f"  {c.BOLD}{t('config_naming'):<14}{c.RESET} : {config.naming_multiaddr or '—'}")
        print(f"  {c.BOLD}{t('config_keys_dir'):<14}{c.RESET} : {c.DIM}{config.keys_dir}{c.RESET}")
        print(f"  {c.BOLD}{t('config_data_dir'):<14}{c.RESET} : {c.DIM}{config.data_dir}{c.RESET}")
        print(f"  {c.BOLD}{t('config_language'):<14}{c.RESET} : {config.language}")
        print(f"  {c.BOLD}{t('config_seeds_count'):<14}{c.RESET} : {len(sites)}")
        print()

        edit = prompt_input(t("config_edit_prompt"))
        if edit.lower() != t("confirm_yes"):
            return config
    else:
        config = ClientConfig(author="anonymous")

    print()
    author = prompt_input(t("config_author_prompt"), f"[{config.author}]")
    if author:
        config.author = author

    naming = prompt_input(
        t("config_naming_prompt"),
        f"[{config.naming_multiaddr or '—'}]",
    )
    if naming:
        config.naming_multiaddr = naming

    lang = prompt_input(t("config_lang_prompt"), f"[{config.language}]")
    if lang and lang in SUPPORTED_LANGUAGES:
        config.language = lang
        load_language(lang)

    for attempt in range(2):
        try:
            config = ensure_config(config)
            config.save()
            print(f"\n  {c.GREEN}{t('config_saved')}{c.RESET}")
            return config
        except PermissionError as e:
            if attempt == 0 and fix_permissions(e):
                continue
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return config

    return config


# ── Interactive loop ────────────────────────────────────────────────


async def interactive_mode(config: Optional[ClientConfig]):
    """Run the interactive terminal interface."""
    if config is None:
        load_language("fr")
        clear_screen()
        print_banner()
        print(f"\n  {c.YELLOW}{t('error_not_configured')}{c.RESET}")
        print(f"  {c.DIM}{t('error_setup_first')}{c.RESET}")
        config = action_config(None)
        input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")
    else:
        config = ensure_config(config)
        load_language(config.language)

    while True:
        clear_screen()
        print_banner()
        sites = get_seeded_sites(config.data_dir)
        print_menu(len(sites))

        try:
            choice = input(f"  {c.BOLD}{t('prompt_choice')}{c.RESET} > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n  {c.GREEN}{t('goodbye')}{c.RESET}\n")
            break

        if choice == "1":
            print_seeds_table(sites)
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "2":
            await action_add(config)
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "3":
            await action_remove(config)
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "4":
            await action_browse(config)
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "5":
            config = action_config(config)
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "6":
            action_pins()
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")

        elif choice == "r":
            continue

        elif choice in ("q", "quit", "exit"):
            print(f"\n  {c.GREEN}{t('goodbye')}{c.RESET}\n")
            break

        else:
            print(f"\n  {c.RED}{t('invalid_choice')}{c.RESET}")
            input(f"\n  {c.DIM}{t('press_enter')}{c.RESET}")


# ── CLI subcommands (for scripting) ─────────────────────────────────


async def cli_list(config: ClientConfig):
    """CLI: List all seeded sites."""
    sites = get_seeded_sites(config.data_dir)
    print_seeds_table(sites)


def cli_pins():
    """CLI: List all pinned keys."""
    path = _get_pinstore_path()
    pinstore = load_pinstore(path)
    print_pins_table(pinstore)


def cli_unpin(uri: str):
    """CLI: Remove a pinned key."""
    uri = strip_uri_scheme(uri)
    path = _get_pinstore_path()
    if unpin_key(uri, path):
        print(f"  {c.GREEN}{t('unpin_success', uri=uri)}{c.RESET}")
        return 0
    print(f"  {c.RED}{t('unpin_not_found', uri=uri)}{c.RESET}")
    return 1


async def cli_browse(config: ClientConfig):
    """CLI: List all sites registered on the naming server."""
    try:
        records = await _list_registered_sites(config)
        print_browse_table(records)
        return 0
    except Exception as e:
        print(f"  {c.RED}{t('browse_error', error=e)}{c.RESET}")
        return 1


async def cli_publish(config: ClientConfig, uri: str, site_dir: str):
    """CLI: Publish a new site."""
    site_path = Path(site_dir).resolve()

    if not site_path.exists():
        print(f"  {c.RED}{t('add_error_no_dir', path=site_path)}{c.RESET}")
        return 1

    md_files = list(site_path.rglob("*.md"))
    if not md_files:
        print(f"  {c.RED}{t('add_error_no_md', path=site_path)}{c.RESET}")
        return 1

    print(f"\n  {t('add_publishing', author=config.author, uri=uri)}")
    print(f"  {c.DIM}{t('add_md_found', count=len(md_files))}{c.RESET}")

    for attempt in range(2):
        try:
            await _do_publish(config, uri, site_path)
            print(f"\n  {c.GREEN}{c.BOLD}{t('add_success')}{c.RESET}")
            return 0
        except PermissionError as e:
            if attempt == 0 and fix_permissions(e):
                continue
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return 1
        except Exception as e:
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return 1

    return 1


async def cli_remove(config: ClientConfig, uri: str):
    """CLI: Remove a seeded site."""
    uri = strip_uri_scheme(uri)
    sites = get_seeded_sites(config.data_dir)
    site_to_remove = None
    for site in sites:
        if site.uri == uri:
            site_to_remove = site
            break

    if not site_to_remove:
        print(f"  {c.RED}{t('remove_not_found', uri=uri)}{c.RESET}")
        return 1

    print(f"\n  {c.YELLOW}{c.BOLD}{t('remove_warning')}{c.RESET}")
    print(f"  {t('remove_info_size', size=format_size(site_to_remove.total_size))}")
    confirm = input(f"  {t('remove_confirm', uri=uri)} ").strip()

    if confirm.lower() != t("confirm_yes"):
        print(f"  {c.DIM}{t('remove_cancelled')}{c.RESET}")
        return 0

    try:
        shutil.rmtree(site_to_remove.site_dir)
        print(f"  {c.GREEN}{t('remove_success')}{c.RESET}")
        return 0
    except Exception as e:
        print(f"  {c.RED}{t('add_error', error=e)}{c.RESET}")
        return 1


def cli_setup(
    config: Optional[ClientConfig],
    author: str,
    naming: Optional[str] = None,
    language: Optional[str] = None,
):
    """CLI: Setup configuration."""
    if config is None:
        config = ClientConfig(author=author)
    else:
        config.author = author

    if naming:
        config.naming_multiaddr = naming

    if language and language in SUPPORTED_LANGUAGES:
        config.language = language

    load_language(config.language)

    for attempt in range(2):
        try:
            config = ensure_config(config)
            config.save()
            print(f"\n  {c.GREEN}{c.BOLD}{t('config_saved')}{c.RESET}")
            print(f"  {c.BOLD}{t('config_author')}{c.RESET}   : {c.CYAN}{config.author}{c.RESET}")
            print(f"  {c.BOLD}{t('config_naming')}{c.RESET}  : {config.naming_multiaddr or '—'}")
            print(f"  {c.BOLD}{t('config_language')}{c.RESET}  : {config.language}")
            return 0
        except PermissionError as e:
            if attempt == 0 and fix_permissions(e):
                continue
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return 1

    return 1


def cli_status(config: ClientConfig):
    """CLI: Show status."""
    sites = get_seeded_sites(config.data_dir)

    print(f"\n  {c.BOLD}{c.CYAN}── {t('config_header')} ──{c.RESET}\n")
    print(f"  {c.BOLD}{t('config_author'):<14}{c.RESET} : {c.CYAN}{config.author}{c.RESET}")
    print(f"  {c.BOLD}{t('config_naming'):<14}{c.RESET} : {config.naming_multiaddr or '—'}")
    print(f"  {c.BOLD}{t('config_keys_dir'):<14}{c.RESET} : {c.DIM}{config.keys_dir}{c.RESET}")
    print(f"  {c.BOLD}{t('config_data_dir'):<14}{c.RESET} : {c.DIM}{config.data_dir}{c.RESET}")
    print(f"  {c.BOLD}{t('config_language'):<14}{c.RESET} : {config.language}")
    print(f"  {c.BOLD}{t('config_seeds_count'):<14}{c.RESET} : {len(sites)}")


# ── Entry point ─────────────────────────────────────────────────────


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="MDP2P Client — Manage your seeded sites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("list", help="List all seeded sites")

    publish_parser = subparsers.add_parser("publish", help="Publish a new site")
    publish_parser.add_argument("--uri", required=True, help="Site URI (e.g., md://blog)")
    publish_parser.add_argument("--site", required=True, help="Directory containing .md files")

    remove_parser = subparsers.add_parser("remove", help="Remove a seeded site")
    remove_parser.add_argument("uri", help="Site URI to remove")

    setup_parser = subparsers.add_parser("setup", help="Setup client configuration")
    setup_parser.add_argument("--author", required=True, help="Author name")
    setup_parser.add_argument(
        "--naming",
        help="Naming server multiaddr (e.g., /ip4/1.2.3.4/tcp/1707/p2p/12D3Koo...)",
    )
    setup_parser.add_argument("--language", help="Language (fr/en/zh/ar/hi)")

    subparsers.add_parser("browse", help="List all sites registered on the naming server")

    subparsers.add_parser("pins", help="List pinned public keys (TOFU)")

    unpin_parser = subparsers.add_parser("unpin", help="Remove a pinned key")
    unpin_parser.add_argument("uri", help="Site URI to unpin")

    subparsers.add_parser("status", help="Show client status")

    subparsers.add_parser(
        "tui",
        help="Open the Textual TUI reader (requires the [tui] extra)",
    )

    args = parser.parse_args()
    config = ClientConfig.load()

    if config:
        load_language(config.language)
    else:
        load_language("fr")

    if not args.command:
        await interactive_mode(config)
        return 0

    if args.command == "setup":
        return cli_setup(config, args.author, args.naming, args.language)

    if args.command == "pins":
        cli_pins()
        return 0
    elif args.command == "unpin":
        return cli_unpin(args.uri)
    elif args.command == "tui":
        try:
            from .tui import run as run_tui
        except ImportError as e:
            print(
                f"  {c.RED}TUI dependencies missing: {e}{c.RESET}\n"
                f"  {c.DIM}Install with: pip install -e \".[tui]\"{c.RESET}"
            )
            return 1
        run_tui()
        return 0

    if config is None:
        print(f"  {c.RED}{t('error_not_configured')}{c.RESET}")
        print(f"  {c.DIM}mdp2p setup --author yourname --naming /ip4/.../tcp/1707/p2p/...{c.RESET}")
        return 1

    config = ensure_config(config)

    if args.command == "list":
        await cli_list(config)
        return 0
    elif args.command == "browse":
        return await cli_browse(config)
    elif args.command == "publish":
        return await cli_publish(config, args.uri, args.site)
    elif args.command == "remove":
        return await cli_remove(config, args.uri)
    elif args.command == "status":
        cli_status(config)
        return 0

    parser.print_help()
    return 0


def run():
    """Entry point for the mdp2p console command."""
    sys.exit(trio.run(main) or 0)


if __name__ == "__main__":
    run()
