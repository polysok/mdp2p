"""
MDP2P Client — argparse routing and entry point.

Kept separate from __main__.py so external launchers (PyInstaller
bundles, pip console scripts) can import `run` without colliding with
the special __main__ module semantics.
"""

import argparse
import sys
from pathlib import Path

import trio

# Root-level modules (bundle, peer, naming, pinstore) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from . import colors as c
from .commands import (
    cli_browse,
    cli_fetch,
    cli_inbox,
    cli_list,
    cli_pins,
    cli_publish,
    cli_remove,
    cli_review,
    cli_reviewer_disable,
    cli_reviewer_enable,
    cli_reviewer_status,
    cli_serve,
    cli_service,
    cli_setup,
    cli_status,
    cli_unpin,
)
from .config import ClientConfig, ensure_config
from .i18n import load_language, t
from .interactive import interactive_mode


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser and all subcommands."""
    parser = argparse.ArgumentParser(
        description="MDP2P Client — Manage your seeded sites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("list", help="List all seeded sites")

    publish_parser = subparsers.add_parser("publish", help="Publish a new site")
    publish_parser.add_argument("--uri", required=True, help="Site URI (e.g., md://blog)")
    publish_parser.add_argument("--site", required=True, help="Directory containing .md files")
    publish_parser.add_argument(
        "--categories",
        help="Comma-separated category slugs (e.g., computing,education). "
        "Defaults to 'other' when omitted.",
    )

    fetch_parser = subparsers.add_parser(
        "fetch", help="Download a site from the network"
    )
    fetch_parser.add_argument("--uri", required=True, help="Site URI (e.g., md://blog)")
    fetch_parser.add_argument(
        "--naming",
        help="Override the configured naming multiaddr (optional)",
    )

    subparsers.add_parser("serve", help="Run as a seeder daemon (foreground)")

    remove_parser = subparsers.add_parser("remove", help="Remove a seeded site")
    remove_parser.add_argument("uri", help="Site URI to remove")

    setup_parser = subparsers.add_parser("setup", help="Setup client configuration")
    setup_parser.add_argument("--author", required=True, help="Author name")
    setup_parser.add_argument(
        "--naming",
        help="Naming server multiaddr (e.g., /ip4/1.2.3.4/tcp/1707/p2p/12D3Koo...)",
    )
    setup_parser.add_argument("--language", help="Language (fr/en/zh/ar/hi)")
    setup_parser.add_argument(
        "--reviewer",
        action="store_true",
        help="Opt in to reviewing content on the network",
    )
    setup_parser.add_argument(
        "--reviewer-categories",
        help="Comma-separated categories you accept to review (e.g., tech,fr)",
    )

    subparsers.add_parser("browse", help="List all sites registered on the naming server")
    subparsers.add_parser("pins", help="List pinned public keys (TOFU)")

    unpin_parser = subparsers.add_parser("unpin", help="Remove a pinned key")
    unpin_parser.add_argument("uri", help="Site URI to unpin")

    subparsers.add_parser("status", help="Show client status")
    subparsers.add_parser(
        "tui",
        help="Open the Textual TUI reader (requires the [tui] extra)",
    )

    service_parser = subparsers.add_parser(
        "service", help="Manage the mdp2p seeder as a user service"
    )
    service_parser.add_argument(
        "action",
        choices=["install", "uninstall", "status"],
        help="What to do with the service",
    )

    inbox_parser = subparsers.add_parser(
        "inbox", help="List pending review assignments for this peer"
    )
    inbox_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    review_parser = subparsers.add_parser(
        "review", help="Post a signed review verdict for a content_key"
    )
    review_parser.add_argument("--content-key", required=True)
    review_parser.add_argument(
        "--verdict", required=True, choices=["ok", "warn", "reject"]
    )
    review_parser.add_argument("--comment", default="")

    reviewer_parser = subparsers.add_parser(
        "reviewer", help="Manage reviewer mode (enable/disable/status)"
    )
    reviewer_parser.add_argument(
        "action",
        choices=["enable", "disable", "status"],
        help="What to do",
    )
    reviewer_parser.add_argument(
        "--categories",
        help="Comma-separated categories (enable only, e.g., tech,fr)",
    )

    return parser


async def main() -> int:
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = _build_parser()
    args = parser.parse_args()
    config = ClientConfig.load()

    load_language(config.language if config else "fr")

    if not args.command:
        await interactive_mode(config)
        return 0

    # Commands that do not require a loaded config.
    if args.command == "setup":
        cats = None
        if getattr(args, "reviewer_categories", None):
            cats = [s.strip() for s in args.reviewer_categories.split(",") if s.strip()]
        return cli_setup(
            config,
            args.author,
            args.naming,
            args.language,
            reviewer=(args.reviewer if args.reviewer else None),
            reviewer_categories=cats,
        )
    if args.command == "pins":
        cli_pins()
        return 0
    if args.command == "unpin":
        return cli_unpin(args.uri)
    if args.command == "tui":
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
    if args.command == "service":
        # Intentionally works without a loaded config so a fresh install can
        # run `mdp2p service status` before `mdp2p setup`.
        return cli_service(args.action, config)

    # Remaining commands need a valid config.
    if config is None:
        print(f"  {c.RED}{t('error_not_configured')}{c.RESET}")
        print(f"  {c.DIM}mdp2p setup --author yourname --naming /ip4/.../tcp/1707/p2p/...{c.RESET}")
        return 1

    config = ensure_config(config)

    if args.command == "list":
        await cli_list(config)
        return 0
    if args.command == "browse":
        return await cli_browse(config)
    if args.command == "publish":
        cats = None
        if getattr(args, "categories", None):
            cats = [s.strip() for s in args.categories.split(",") if s.strip()]
        return await cli_publish(config, args.uri, args.site, categories=cats)
    if args.command == "fetch":
        return await cli_fetch(config, args.uri, args.naming)
    if args.command == "serve":
        return await cli_serve(config)
    if args.command == "remove":
        return await cli_remove(config, args.uri)
    if args.command == "status":
        cli_status(config)
        return 0
    if args.command == "inbox":
        return await cli_inbox(config, as_json=args.json)
    if args.command == "review":
        return await cli_review(
            config, args.content_key, args.verdict, args.comment
        )
    if args.command == "reviewer":
        if args.action == "enable":
            cats = None
            if args.categories:
                cats = [s.strip() for s in args.categories.split(",") if s.strip()]
            return cli_reviewer_enable(config, categories=cats)
        if args.action == "disable":
            return cli_reviewer_disable(config)
        if args.action == "status":
            return cli_reviewer_status(config)

    parser.print_help()
    return 0


def run() -> None:
    """Entry point for the mdp2p console command and standalone binaries."""
    sys.exit(trio.run(main) or 0)
