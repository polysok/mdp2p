"""Scriptable CLI subcommands (list, publish, remove, browse, pins, setup, ...)."""

import shutil
import sys
from pathlib import Path
from typing import Optional

# Root-level modules (pinstore) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pinstore import load_pinstore, unpin_key

from . import colors as c
from .config import ClientConfig, ensure_config, get_seeded_sites
from .formatting import format_size, strip_uri_scheme
from .i18n import SUPPORTED_LANGUAGES, load_language, t
from .permissions import fix_permissions
from .publish_flow import do_publish, get_pinstore_path, list_registered_sites
from .ui import print_browse_table, print_pins_table, print_seeds_table


async def cli_list(config: ClientConfig) -> None:
    """CLI: List all seeded sites."""
    sites = get_seeded_sites(config.data_dir)
    print_seeds_table(sites)


def cli_pins() -> None:
    """CLI: List all pinned keys."""
    path = get_pinstore_path()
    pinstore = load_pinstore(path)
    print_pins_table(pinstore)


def cli_unpin(uri: str) -> int:
    """CLI: Remove a pinned key."""
    uri = strip_uri_scheme(uri)
    path = get_pinstore_path()
    if unpin_key(uri, path):
        print(f"  {c.GREEN}{t('unpin_success', uri=uri)}{c.RESET}")
        return 0
    print(f"  {c.RED}{t('unpin_not_found', uri=uri)}{c.RESET}")
    return 1


async def cli_browse(config: ClientConfig) -> int:
    """CLI: List all sites registered on the naming server."""
    try:
        records = await list_registered_sites(config)
        print_browse_table(records)
        return 0
    except Exception as e:
        print(f"  {c.RED}{t('browse_error', error=e)}{c.RESET}")
        return 1


async def cli_publish(config: ClientConfig, uri: str, site_dir: str) -> int:
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
            await do_publish(config, uri, site_path)
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


async def cli_remove(config: ClientConfig, uri: str) -> int:
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
) -> int:
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


def cli_status(config: ClientConfig) -> None:
    """CLI: Show status."""
    sites = get_seeded_sites(config.data_dir)

    print(f"\n  {c.BOLD}{c.CYAN}── {t('config_header')} ──{c.RESET}\n")
    print(f"  {c.BOLD}{t('config_author'):<14}{c.RESET} : {c.CYAN}{config.author}{c.RESET}")
    print(f"  {c.BOLD}{t('config_naming'):<14}{c.RESET} : {config.naming_multiaddr or '—'}")
    print(f"  {c.BOLD}{t('config_keys_dir'):<14}{c.RESET} : {c.DIM}{config.keys_dir}{c.RESET}")
    print(f"  {c.BOLD}{t('config_data_dir'):<14}{c.RESET} : {c.DIM}{config.data_dir}{c.RESET}")
    print(f"  {c.BOLD}{t('config_language'):<14}{c.RESET} : {config.language}")
    print(f"  {c.BOLD}{t('config_seeds_count'):<14}{c.RESET} : {len(sites)}")
