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
    cli_list,
    cli_pins,
    cli_publish,
    cli_remove,
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

    fetch_parser = subparsers.add_parser(
        "fetch", help="Download a site from the network"
    )
    fetch_parser.add_argument("--uri", required=True, help="Site URI (e.g., md://blog)")
    fetch_parser.add_argument(
        "--naming",
        help="Override the configured naming multiaddr (optional)",
    )

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
        return cli_setup(config, args.author, args.naming, args.language)
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
        return await cli_publish(config, args.uri, args.site)
    if args.command == "fetch":
        return await cli_fetch(config, args.uri, args.naming)
    if args.command == "remove":
        return await cli_remove(config, args.uri)
    if args.command == "status":
        cli_status(config)
        return 0

    parser.print_help()
    return 0


def run() -> None:
    """Entry point for the mdp2p console command and standalone binaries."""
    sys.exit(trio.run(main) or 0)
