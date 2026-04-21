"""Scriptable CLI subcommands (list, publish, remove, browse, pins, setup, ...)."""

import shutil
import sys
from pathlib import Path
from typing import Optional

import trio

# Root-level modules (pinstore, mdp2p_logging) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mdp2p_logging import silence_libp2p_noise
from pinstore import load_pinstore, unpin_key

from . import colors as c
from . import service as svc
from .config import ClientConfig, ensure_config, get_seeded_sites
from .formatting import format_size, strip_uri_scheme
from .i18n import SUPPORTED_LANGUAGES, load_language, t
from .permissions import fix_permissions
from .fetch_flow import do_fetch
from .publish_flow import do_publish, get_pinstore_path, list_registered_sites, require_naming
from .review_flow import do_attach_review, do_list_inbox
from .serve_flow import do_serve
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


async def cli_publish(
    config: ClientConfig,
    uri: str,
    site_dir: str,
    categories: Optional[list[str]] = None,
) -> int:
    """CLI: Publish a new site."""
    from review import validate_categories

    if categories is not None:
        try:
            validate_categories(categories)
        except ValueError as e:
            print(f"  {c.RED}{e}{c.RESET}")
            return 1

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
            await do_publish(config, uri, site_path, categories=categories)
            print(f"\n  {c.GREEN}{c.BOLD}{t('add_success')}{c.RESET}")
            svc.offer_interactive(config)
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


async def cli_fetch(
    config: ClientConfig, uri: str, naming: Optional[str] = None
) -> int:
    """CLI: Download a site from the network."""
    bare = strip_uri_scheme(uri)
    try:
        ok = await do_fetch(config, uri, naming_multiaddr=naming)
    except Exception as e:
        print(f"  {c.RED}fetch failed: {e}{c.RESET}")
        return 1

    if ok:
        print(f"  {c.GREEN}md://{bare} fetched ✓{c.RESET}")
        svc.offer_interactive(config)
        return 0
    print(f"  {c.RED}fetch failed for md://{bare}{c.RESET}")
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
    reviewer: Optional[bool] = None,
    reviewer_categories: Optional[list[str]] = None,
) -> int:
    """CLI: Setup configuration."""
    from review import validate_categories

    if reviewer_categories is not None:
        try:
            validate_categories(reviewer_categories)
        except ValueError as e:
            print(f"\n  {c.RED}{e}{c.RESET}")
            return 1

    if config is None:
        config = ClientConfig(author=author)
    else:
        config.author = author

    if naming:
        config.naming_multiaddr = naming

    if language and language in SUPPORTED_LANGUAGES:
        config.language = language

    if reviewer is not None:
        config.reviewer_mode = reviewer
    if reviewer_categories is not None:
        config.reviewer_categories = list(reviewer_categories)

    load_language(config.language)

    for attempt in range(2):
        try:
            config = ensure_config(config)
            if config.reviewer_mode:
                _ensure_reviewer_identity_quietly(config)
            config.save()
            print(f"\n  {c.GREEN}{c.BOLD}{t('config_saved')}{c.RESET}")
            print(f"  {c.BOLD}{t('config_author')}{c.RESET}   : {c.CYAN}{config.author}{c.RESET}")
            print(f"  {c.BOLD}{t('config_naming')}{c.RESET}  : {config.naming_multiaddr or '—'}")
            print(f"  {c.BOLD}{t('config_language')}{c.RESET}  : {config.language}")
            print(
                f"  {c.BOLD}Reviewer{c.RESET}  : "
                f"{_fmt_bool(config.reviewer_mode)}"
                + (
                    f" {c.DIM}({', '.join(config.reviewer_categories)}){c.RESET}"
                    if config.reviewer_categories
                    else ""
                )
            )
            return 0
        except PermissionError as e:
            if attempt == 0 and fix_permissions(e):
                continue
            print(f"\n  {c.RED}{t('add_error', error=e)}{c.RESET}")
            return 1

    return 1


# ─── Reviewer management ───────────────────────────────────────────────


def _ensure_reviewer_identity_quietly(config: ClientConfig) -> str:
    """Create the reviewer keypair if needed; return the base64 public key."""
    from peer.reviewer_daemon import ensure_reviewer_identity
    _priv, pub_b64 = ensure_reviewer_identity(config.reviewer_dir)
    return pub_b64


def _load_reviewer_pubkey(config: ClientConfig) -> Optional[str]:
    """Return the reviewer public key if one is already on disk, else None."""
    from bundle import load_public_key, public_key_to_b64
    pub_path = Path(config.reviewer_dir).expanduser() / "reviewer.pub"
    if not pub_path.exists():
        return None
    try:
        return public_key_to_b64(load_public_key(str(pub_path)))
    except Exception:
        return None


def _print_restart_hint() -> None:
    print(f"  {c.DIM}restart the seeder to apply the change:{c.RESET}")
    print(f"    {c.CYAN}mdp2p serve{c.RESET}")
    print(f"  {c.DIM}or, if you use the auto-seeder service, "
          f"reinstall it:{c.RESET}")
    print(f"    {c.CYAN}mdp2p service uninstall && mdp2p service install{c.RESET}")


def cli_reviewer_enable(
    config: ClientConfig,
    categories: Optional[list[str]] = None,
) -> int:
    """Enable reviewer mode + generate the reviewer identity if missing."""
    from review import validate_categories

    if categories is not None:
        try:
            validate_categories(categories)
        except ValueError as e:
            print(f"\n  {c.RED}{e}{c.RESET}")
            return 1

    was_enabled = config.reviewer_mode
    config.reviewer_mode = True
    if categories is not None:
        config.reviewer_categories = list(categories)

    try:
        pub_b64 = _ensure_reviewer_identity_quietly(config)
        config.save()
    except PermissionError as e:
        print(f"\n  {c.RED}permission error: {e}{c.RESET}")
        return 1
    except Exception as e:
        print(f"\n  {c.RED}could not enable reviewer mode: {e}{c.RESET}")
        return 1

    status = "already" if was_enabled else "now"
    print(f"\n  {c.GREEN}{c.BOLD}Reviewer mode is {status} enabled ✓{c.RESET}\n")
    print(f"  {c.BOLD}Pubkey{c.RESET}     : {c.DIM}{pub_b64}{c.RESET}")
    print(
        f"  {c.BOLD}Categories{c.RESET} : "
        f"{', '.join(config.reviewer_categories) if config.reviewer_categories else '(any)'}"
    )
    print(f"  {c.BOLD}Directory{c.RESET}  : {c.DIM}{config.reviewer_dir}{c.RESET}")
    print()
    _print_restart_hint()
    return 0


def cli_reviewer_disable(config: ClientConfig) -> int:
    """Turn reviewer mode off in config. The identity on disk is preserved."""
    if not config.reviewer_mode:
        print(f"  {c.DIM}reviewer mode is already disabled{c.RESET}")
        return 0

    config.reviewer_mode = False
    try:
        config.save()
    except Exception as e:
        print(f"\n  {c.RED}could not save config: {e}{c.RESET}")
        return 1

    print(f"\n  {c.GREEN}Reviewer mode disabled ✓{c.RESET}")
    print(
        f"  {c.DIM}your reviewer identity is preserved under "
        f"{config.reviewer_dir} — enable again to reuse the same pubkey.{c.RESET}"
    )
    print()
    _print_restart_hint()
    return 0


def cli_reviewer_status(config: ClientConfig) -> int:
    """Show the current reviewer configuration and identity, offline."""
    pub_b64 = _load_reviewer_pubkey(config)

    print(f"\n  {c.BOLD}{c.CYAN}── Reviewer ──{c.RESET}\n")
    print(f"  {c.BOLD}{'Enabled':<11}{c.RESET} : {_fmt_bool(config.reviewer_mode)}")
    if pub_b64:
        print(f"  {c.BOLD}{'Pubkey':<11}{c.RESET} : {c.DIM}{pub_b64}{c.RESET}")
    else:
        print(
            f"  {c.BOLD}{'Pubkey':<11}{c.RESET} : {c.DIM}(no identity yet — "
            f"run `mdp2p reviewer enable`){c.RESET}"
        )
    print(
        f"  {c.BOLD}{'Categories':<11}{c.RESET} : "
        f"{', '.join(config.reviewer_categories) if config.reviewer_categories else '(any)'}"
    )
    print(f"  {c.BOLD}{'Directory':<11}{c.RESET} : {c.DIM}{config.reviewer_dir}{c.RESET}")
    return 0


async def cli_inbox(config: ClientConfig, as_json: bool = False) -> int:
    """CLI: List pending review assignments for the local reviewer identity."""
    import json
    try:
        pending = await do_list_inbox(config)
    except Exception as e:
        print(f"  {c.RED}inbox error: {e}{c.RESET}")
        return 1

    if as_json:
        print(json.dumps(pending, indent=2, sort_keys=True))
        return 0

    if not pending:
        print(f"  {c.DIM}inbox is empty{c.RESET}")
        return 0

    print(f"\n  {c.BOLD}{len(pending)} pending review(s){c.RESET}\n")
    for entry in pending:
        r = entry["record"]
        deadline_str = ""
        try:
            import datetime as _dt
            deadline_str = _dt.datetime.fromtimestamp(
                int(r["deadline"])
            ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        print(
            f"  {c.BOLD}md://{r['uri']}{c.RESET}\n"
            f"    content_key : {c.DIM}{r['content_key']}{c.RESET}\n"
            f"    publisher   : {c.DIM}{r['publisher_public_key'][:12]}…{c.RESET}\n"
            f"    deadline    : {deadline_str}\n"
        )
    return 0


async def cli_review(
    config: ClientConfig,
    content_key: str,
    verdict: str,
    comment: str = "",
) -> int:
    """CLI: Sign and post a review record for a content_key."""
    try:
        await do_attach_review(config, content_key, verdict, comment)
    except Exception as e:
        print(f"  {c.RED}review failed: {e}{c.RESET}")
        return 1
    print(f"  {c.GREEN}review posted ({verdict}) for {content_key[:24]}…{c.RESET}")
    return 0


async def cli_serve(config: ClientConfig) -> int:
    """CLI: Run as a long-running seeder daemon (foreground)."""
    silence_libp2p_noise()

    # Service managers (launchd, systemd, Task Scheduler) send SIGTERM to stop
    # the process. Trio only hooks SIGINT, so re-route SIGTERM onto SIGINT so
    # the host closes its streams gracefully instead of being hard-killed.
    import os
    import signal as _signal
    _signal.signal(_signal.SIGTERM, lambda *_: os.kill(os.getpid(), _signal.SIGINT))

    site_count = len(get_seeded_sites(config.data_dir))

    print(f"\n  {c.BOLD}{c.CYAN}── MDP2P Seeder ──{c.RESET}\n")
    print(f"  {c.BOLD}Naming{c.RESET}   : {require_naming(config)}")
    print(f"  {c.BOLD}Data dir{c.RESET} : {c.DIM}{config.data_dir}{c.RESET}")
    print(f"  {c.BOLD}Sites{c.RESET}    : {site_count}")
    print()

    try:
        await do_serve(config)
        # do_serve only returns once the peer is fully shut down. run_peer's
        # finally explicitly cancels its own scope, which absorbs the Cancelled
        # raised by SIGINT/SIGTERM — so the normal return path IS the graceful
        # shutdown path. Anything raising out of do_serve is a real error.
        print(f"\n  {c.DIM}Seeder stopped.{c.RESET}")
        return 0
    except Exception as e:
        print(f"\n  {c.RED}Seeder error: {e}{c.RESET}")
        return 1


def _fmt_bool(value: bool) -> str:
    """Colorize a boolean for status output."""
    return f"{c.GREEN}yes{c.RESET}" if value else f"{c.YELLOW}no{c.RESET}"


def cli_service(action: str, config: Optional[ClientConfig]) -> int:
    """CLI: Manage the mdp2p seeder as a user-level auto-starting service."""
    platform = svc.get_platform()

    if platform == "unsupported":
        print(
            f"  {c.RED}Unsupported platform for automated service install.{c.RESET}\n"
            f"  {c.DIM}Fallback: run `mdp2p serve` inside tmux or screen.{c.RESET}"
        )
        return 1

    if action == "status":
        info = svc.status()
        print(f"\n  {c.BOLD}{c.CYAN}── MDP2P Service ──{c.RESET}\n")
        print(f"  {c.BOLD}{'Platform':<12}{c.RESET} : {info['platform']}")
        print(f"  {c.BOLD}{'Installed':<12}{c.RESET} : {_fmt_bool(info['installed'])}")
        print(f"  {c.BOLD}{'Running':<12}{c.RESET} : {_fmt_bool(info['running'])}")
        path = info.get("path") or "—"
        print(f"  {c.BOLD}{'Path':<12}{c.RESET} : {c.DIM}{path}{c.RESET}")
        details = info.get("details") or ""
        if details:
            print(f"  {c.BOLD}{'Details':<12}{c.RESET} : {c.DIM}{details}{c.RESET}")
        return 0

    if action == "install":
        ok, path, message = svc.install()
        if ok:
            print(f"  {c.GREEN}{message}{c.RESET}")
            if path:
                print(f"  {c.DIM}Unit file: {path}{c.RESET}")
            print(f"  {c.DIM}Check status with: mdp2p service status{c.RESET}")
            print(f"  {c.DIM}Remove with:       mdp2p service uninstall{c.RESET}")
            return 0
        print(f"  {c.RED}Install failed.{c.RESET}")
        if path:
            print(f"  {c.DIM}Unit file path: {path}{c.RESET}")
        print(f"  {c.YELLOW}{message}{c.RESET}")
        return 1

    if action == "uninstall":
        ok, message = svc.uninstall()
        if ok:
            print(f"  {c.GREEN}{message}{c.RESET}")
            return 0
        print(f"  {c.RED}{message}{c.RESET}")
        return 1

    print(f"  {c.RED}Unknown action: {action}{c.RESET}")
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
