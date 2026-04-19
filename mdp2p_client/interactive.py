"""Interactive (stdin) mode: menu loop and per-action handlers."""

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
from .ui import (
    clear_screen,
    print_banner,
    print_browse_table,
    print_menu,
    print_pins_table,
    print_seeds_table,
    prompt_input,
)


async def action_add(config: ClientConfig) -> None:
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
            await do_publish(config, uri, site_path)
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


async def action_remove(config: ClientConfig) -> None:
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


async def action_browse(config: ClientConfig) -> None:
    """Interactive browse: list all sites registered on the naming server."""
    try:
        records = await list_registered_sites(config)
        print_browse_table(records)
    except Exception as e:
        print(f"\n  {c.RED}{t('browse_error', error=e)}{c.RESET}")


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


def action_pins() -> None:
    """Interactive: view and manage pinned keys."""
    path = get_pinstore_path()
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


async def interactive_mode(config: Optional[ClientConfig]) -> None:
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
