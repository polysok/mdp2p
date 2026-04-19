"""Presentation-layer helpers: banner, menus, tables, prompts."""

from typing import Callable, Optional

from . import colors as c
from .config import SeededSite
from .formatting import format_size, format_timestamp
from .i18n import t


def clear_screen() -> None:
    """Clear the terminal screen."""
    print("\033[2J\033[H", end="", flush=True)


def print_banner() -> None:
    """Print the application banner."""
    title = t("app_title")
    width = max(len(title) + 6, 48)
    print()
    print(f"  {c.CYAN}{c.BOLD}{'═' * width}{c.RESET}")
    print(f"  {c.CYAN}{c.BOLD}   {title.center(width - 6)}   {c.RESET}")
    print(f"  {c.CYAN}{c.BOLD}{'═' * width}{c.RESET}")


def print_menu(seed_count: int) -> None:
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


# Type alias: given (cell, width) return the styled cell (already padded).
CellStyler = Callable[[str, int], str]


def _render_table(
    headers: list[str],
    rows: list[list[str]],
    styles: list[CellStyler],
    title: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    """Render a bordered, colored table with a consistent visual style.

    Keeps the exact output of the original hand-rolled tables: header bolded,
    dim separator line with '─┼─', title printed above as '── TITLE ──',
    footer as a plain localized line after the body.
    """
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    print()
    if title is not None:
        print(f"  {c.BOLD}{c.CYAN}── {title} ──{c.RESET}\n")

    header_line = "  " + " │ ".join(
        f"{c.BOLD}{h.ljust(col_widths[i])}{c.RESET}" for i, h in enumerate(headers)
    )
    print(header_line)
    separator = "─┼─".join("─" * w for w in col_widths)
    print(f"  {c.DIM}{separator}{c.RESET}")

    for row in rows:
        cells = [styles[i](row[i], col_widths[i]) for i in range(len(row))]
        print("  " + " │ ".join(cells))

    if footer is not None:
        print(f"\n  {footer}")


def print_seeds_table(sites: list[SeededSite]) -> None:
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

    styles: list[CellStyler] = [
        lambda cell, w: f"{c.MAGENTA}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.BLUE}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{cell.ljust(w)}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
    ]

    _render_table(
        headers=headers,
        rows=rows,
        styles=styles,
        title=None,
        footer=t("total_seeds", count=len(sites)),
    )


def print_browse_table(sites: list[dict]) -> None:
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

    styles: list[CellStyler] = [
        lambda cell, w: f"{c.MAGENTA}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.CYAN}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
    ]

    _render_table(
        headers=headers,
        rows=rows,
        styles=styles,
        title=t("browse_header"),
        footer=t("browse_total", count=len(sites)),
    )


def print_pins_table(pinstore: dict) -> None:
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

    styles: list[CellStyler] = [
        lambda cell, w: f"{c.MAGENTA}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.CYAN}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
        lambda cell, w: f"{c.DIM}{cell.ljust(w)}{c.RESET}",
    ]

    _render_table(
        headers=headers,
        rows=rows,
        styles=styles,
        title=t("pins_header"),
        footer=t("pins_total", count=len(pinstore)),
    )


def prompt_input(label: str, hint: str = "") -> str:
    """Prompt for user input with colored label."""
    hint_text = f" {c.DIM}{hint}{c.RESET}" if hint else ""
    try:
        return input(f"  {c.CYAN}{label}{c.RESET}{hint_text} : ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
