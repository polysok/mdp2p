"""
mdp2p TUI — Textual-based terminal reader for Markdown sites served over libp2p.

Layout:
    ┌──────────────────────────────────────────────────────────────────┐
    │ Header                                                           │
    ├─────────────────┬────────────────────────────────────────────────┤
    │ Sidebar         │ Markdown viewport                              │
    │ ┌─ 🔍 search ─┐ │                                                │
    │ │              │ │                                                │
    │ └──────────────┘ │                                                │
    │ • alice/blog    │                                                │
    │ • poly/notes    │                                                │
    │                 │                                                │
    ├─────────────────┴────────────────────────────────────────────────┤
    │ Footer / bindings                                                │
    └──────────────────────────────────────────────────────────────────┘

Keys: ↑↓ navigate, enter open, / search, f fetch new URI, r refresh, q quit.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
)

from bundle import load_bundle
from mdp2p_client import service as svc
from mdp2p_client.config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_DATA_DIR,
    ClientConfig,
    SeededSite,
    get_seeded_sites,
    load_or_create_config,
)
from mdp2p_client.formatting import format_size
from mdp2p_client.scoring import score_from_cache
from trust import ScoreResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PINSTORE = str(DEFAULT_CONFIG_DIR / "known_keys.json")
DEFAULT_TRUST_STORE = str(DEFAULT_CONFIG_DIR / "trust.json")
DEFAULT_POLICY_PATH = str(DEFAULT_CONFIG_DIR / "policy.json")


DECISION_BADGE = {
    "show": "",
    "warn": "⚠",
    "hide": "⛔",
}

VERDICT_LABEL = {
    "ok": "[green]ok[/]",
    "warn": "[yellow]warn[/]",
    "reject": "[red]reject[/]",
}


# ─── Data helpers ───────────────────────────────────────────────────────

@dataclass
class SiteView:
    """Everything the TUI needs to display one site."""

    uri: str
    author: str
    site_dir: str
    file_count: int
    total_size: int
    markdown: str
    score: Optional[ScoreResult] = None

    @classmethod
    def from_seeded(cls, site: SeededSite) -> "SiteView":
        try:
            score = score_from_cache(
                site.site_dir,
                policy_path=DEFAULT_POLICY_PATH,
                trust_store_path=DEFAULT_TRUST_STORE,
            )
        except Exception:
            score = None
        return cls(
            uri=site.uri,
            author=site.author,
            site_dir=site.site_dir,
            file_count=site.file_count,
            total_size=site.total_size,
            markdown=_assemble_markdown(site.site_dir),
            score=score,
        )

    def sidebar_badge(self) -> str:
        if self.score is None:
            return ""
        return DECISION_BADGE.get(self.score.decision, "")

    def score_banner_markdown(self) -> str:
        """Render a compact score banner shown above the content."""
        if self.score is None:
            return ""
        decision = self.score.decision
        breakdown = self.score.breakdown
        reviewer_count = len(breakdown)
        if reviewer_count == 0:
            return (
                "> *non revu* — aucune review pour l'instant."
                " Le contenu peut être en attente de validation.\n\n"
            )

        header = {
            "show": (
                f"> ✓ revu par **{reviewer_count}** peer(s), "
                f"score {self.score.score:.2f}\n\n"
            ),
            "warn": (
                f"> ⚠ **à lire avec précaution** — score {self.score.score:.2f} "
                f"sur {reviewer_count} review(s)\n\n"
            ),
            "hide": (
                f"> ⛔ **contenu signalé comme problématique** — score "
                f"{self.score.score:.2f} sur {reviewer_count} review(s)\n\n"
            ),
        }.get(decision, "")

        lines = [header]
        for contrib in breakdown:
            verdict = VERDICT_LABEL.get(contrib.verdict, contrib.verdict)
            lines.append(
                f"> - {verdict} par `{contrib.source_pubkey[:10]}…` "
                f"(poids {contrib.weight:.2f})\n"
            )
        lines.append("\n---\n\n")
        return "".join(lines)


def _assemble_markdown(site_dir: str) -> str:
    """Concatenate every file listed in the manifest, prefixed with its path."""
    try:
        manifest, _ = load_bundle(site_dir)
    except Exception as e:
        return f"> **error loading manifest**: `{e}`"

    parts: list[str] = []
    base = Path(site_dir)
    files = manifest.get("files", [])
    if len(files) > 1:
        # Index page at the top if there are several files.
        parts.append(f"# `md://{manifest.get('uri', site_dir)}`\n\n")
        parts.append(f"*{manifest.get('author', 'unknown')} — "
                     f"{len(files)} pages, version {manifest.get('version', '?')}*\n\n")
        parts.append("---\n\n")

    for entry in files:
        path = base / entry["path"]
        if not path.exists():
            continue
        if len(files) > 1:
            parts.append(f"\n\n## `{entry['path']}`\n\n")
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except Exception as e:
            parts.append(f"> error reading `{entry['path']}`: {e}")
        parts.append("\n")

    return "".join(parts) or "_(empty site)_"


# ─── Fetch modal ────────────────────────────────────────────────────────

class FetchModal(ModalScreen[tuple[str, str] | None]):
    """Collects (uri, naming_multiaddr) from the user."""

    CSS = """
    FetchModal { align: center middle; }
    #dialog {
        width: 76;
        height: auto;
        background: $panel;
        border: tall $primary;
        padding: 1 2;
    }
    #dialog > Label { padding: 0 1; }
    #dialog > Input { margin-bottom: 1; }
    #buttons { layout: horizontal; height: auto; align-horizontal: right; }
    #buttons > Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_naming: str = "") -> None:
        super().__init__()
        self._default_naming = default_naming

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Fetch a new site[/]")
            yield Label("URI (e.g. blog.alice):")
            yield Input(placeholder="blog.alice", id="uri-input")
            yield Label("Naming server multiaddr:")
            yield Input(
                value=self._default_naming,
                placeholder="/dns4/relay.mdp2p.net/tcp/1707/p2p/…",
                id="naming-input",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Fetch", variant="primary", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        uri = self.query_one("#uri-input", Input).value.strip()
        naming = self.query_one("#naming-input", Input).value.strip()
        if not uri or not naming:
            self.app.notify("URI and naming multiaddr are required", severity="warning")
            return
        self.dismiss((uri, naming))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ─── Publish modal ──────────────────────────────────────────────────────

class PublishModal(ModalScreen[tuple[str, str] | None]):
    """Collects (uri, site_path) for publishing a local folder of .md files."""

    CSS = """
    PublishModal { align: center middle; }
    #dialog {
        width: 76;
        height: auto;
        background: $panel;
        border: tall $success;
        padding: 1 2;
    }
    #dialog > Label { padding: 0 1; }
    #dialog > Input { margin-bottom: 1; }
    #buttons { layout: horizontal; height: auto; align-horizontal: right; }
    #buttons > Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Publish a new site[/]")
            yield Label("URI (e.g. blog):")
            yield Input(placeholder="blog", id="uri-input")
            yield Label("Path to folder containing .md files:")
            yield Input(
                placeholder="~/Documents/my_site",
                id="site-input",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Publish", variant="primary", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        uri = self.query_one("#uri-input", Input).value.strip()
        path = self.query_one("#site-input", Input).value.strip()
        if not uri or not path:
            self.app.notify("URI and path are required", severity="warning")
            return
        self.dismiss((uri, path))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ─── Reviewer inbox modal ──────────────────────────────────────────────


class InboxModal(ModalScreen[Optional[tuple[str, dict]]]):
    """Shows pending review assignments and lets the reviewer pick a verdict.

    Dismisses with a ``(action, record)`` tuple where action is one of
    ``"fetch" | "ok" | "warn" | "reject"``, or ``None`` on cancel. The
    surrounding App then performs the matching subprocess call.
    """

    CSS = """
    InboxModal { align: center middle; }
    #dialog {
        width: 90;
        height: 30;
        background: $panel;
        border: tall $accent;
        padding: 1 2;
    }
    #dialog > Label { padding: 0 1; }
    #inbox-list { height: 1fr; margin: 1 0; }
    #buttons { layout: horizontal; height: auto; align-horizontal: right; }
    #buttons > Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, pending: list[dict]) -> None:
        super().__init__()
        self._pending = pending

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]Inbox — {len(self._pending)} review(s) pending[/]")
            yield ListView(id="inbox-list")
            with Horizontal(id="buttons"):
                yield Button("Fetch", variant="primary", id="fetch")
                yield Button("Ok", variant="success", id="ok")
                yield Button("Warn", variant="warning", id="warn")
                yield Button("Reject", variant="error", id="reject")
                yield Button("Close", variant="default", id="cancel")

    async def on_mount(self) -> None:
        list_view = self.query_one("#inbox-list", ListView)
        if not self._pending:
            await list_view.append(
                ListItem(
                    Static("inbox is empty", classes="muted"), disabled=True
                )
            )
            return
        for idx, entry in enumerate(self._pending):
            r = entry["record"]
            deadline_str = _format_deadline(int(r.get("deadline", 0)))
            label = Label(
                f"[b]md://{r.get('uri', '—')}[/]\n"
                f"  deadline: {deadline_str}\n"
                f"  [dim]content_key {r.get('content_key', '')[:24]}…[/]"
            )
            await list_view.append(ListItem(label, id=f"inbox-{idx}"))
        list_view.focus()

    def _highlighted_record(self) -> Optional[dict]:
        list_view = self.query_one("#inbox-list", ListView)
        idx = list_view.index
        if idx is None or idx < 0 or idx >= len(self._pending):
            return None
        return self._pending[idx]["record"]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        record = self._highlighted_record()
        if record is None:
            self.app.notify("select an assignment first", severity="warning")
            return
        self.dismiss((event.button.id, record))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _format_deadline(ts: int) -> str:
    if ts <= 0:
        return "—"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ─── Auto-seed confirmation modal ──────────────────────────────────────

class AutoSeedModal(ModalScreen[bool]):
    """One-shot offer to install the auto-seeder background service."""

    CSS = """
    AutoSeedModal { align: center middle; }
    #dialog {
        width: 72;
        height: auto;
        background: $panel;
        border: tall $warning;
        padding: 1 2;
    }
    #dialog > Label { padding: 0 1; }
    #buttons { layout: horizontal; height: auto; align-horizontal: right; }
    #buttons > Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Skip")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Keep your sites online?[/]")
            yield Label(
                "Your sites are only reachable on the P2P network while an "
                "mdp2p process is running."
            )
            yield Label(
                "Install a background service so seeding starts automatically "
                "at login?"
            )
            yield Label("")
            with Horizontal(id="buttons"):
                yield Button("Skip", variant="default", id="skip")
                yield Button("Enable", variant="primary", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        self.dismiss(False)


# ─── Main app ───────────────────────────────────────────────────────────

class Mdp2pTUI(App[None]):
    CSS = """
    Screen { layers: base overlay; }

    #sidebar {
        width: 32;
        height: 100%;
        border-right: tall $primary-background;
    }
    #search {
        margin: 1 1 0 1;
    }
    #sites {
        height: 1fr;
    }
    #sites-empty {
        padding: 2 2;
        color: $text-muted;
        text-align: center;
    }

    #content {
        padding: 1 2;
    }
    #render {
        height: auto;
    }

    .muted { color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "fetch", "Fetch new URI"),
        Binding("p", "publish", "Publish site"),
        Binding("r", "refresh", "Refresh"),
        Binding("i", "inbox", "Inbox"),
        Binding("slash", "focus_search", "Search"),
        Binding("ctrl+f", "fetch", show=False),
        Binding("ctrl+p", "publish", show=False),
        Binding("escape", "focus_list", show=False),
    ]

    TITLE = "mdp2p"

    def __init__(self, config: Optional[ClientConfig] = None) -> None:
        super().__init__()
        self.config = config or ClientConfig(author="anonymous")
        self._sites: list[SiteView] = []

    # ── layout ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Input(placeholder="🔍  Filter…", id="search")
                yield ListView(id="sites")
            with VerticalScroll(id="content"):
                yield Markdown(self._welcome_markdown(), id="render")
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = self._subtitle_text()
        await self._load_sites()

    # ── state helpers ──────────────────────────────────────────────

    def _subtitle_text(self) -> str:
        if self.config.naming_multiaddr:
            short = self.config.naming_multiaddr
            if "/p2p/" in short:
                prefix, pid = short.rsplit("/p2p/", 1)
                short = f"{prefix}/p2p/{pid[:10]}…"
            return short
        return "no naming server configured"

    def _welcome_markdown(self) -> str:
        naming = self.config.naming_multiaddr or "—"
        host = naming.split("/")[2] if naming.startswith(("/dns4/", "/ip4/")) else naming[:40]
        return (
            f"# Welcome, **{self.config.author}**\n\n"
            f"Connected to `{host}` — browse any site other peers have "
            "published on the network.\n\n"
            "## What you can do\n\n"
            "- Pick a site in the sidebar, or\n"
            "- Press **`f`** to fetch a new URI by name\n"
            "- Press **`p`** to publish a folder of `.md` files as a new site\n"
            "- Press **`/`** to search your locally cached sites\n"
            "- Press **`r`** to refresh the list, **`q`** to quit\n\n"
            "---\n\n"
            f"*Local cache:* `{self.config.data_dir}`\n"
        )

    async def _load_sites(self) -> None:
        seeded = get_seeded_sites(str(self.config.data_dir))
        self._sites = [SiteView.from_seeded(s) for s in seeded]
        await self._apply_filter(self.query_one("#search", Input).value)

    async def _apply_filter(self, query: str) -> None:
        q = (query or "").lower().strip()
        list_view = self.query_one("#sites", ListView)
        # ListView.clear() is async — schedules removal on the next cycle;
        # awaiting it prevents trying to re-append with the same id before
        # the previous nodes are torn down (DuplicateIds otherwise).
        await list_view.clear()

        visible = [
            s for s in self._sites
            if not q or q in s.uri.lower() or q in s.author.lower()
        ]

        if not visible:
            msg = (
                "no sites yet — press [b]f[/] to fetch one"
                if not self._sites
                else "no match for your filter"
            )
            await list_view.append(
                ListItem(Static(msg, classes="muted"), disabled=True)
            )
            return

        for site in visible:
            badge = site.sidebar_badge()
            badge_str = f" {badge}" if badge else ""
            label = Label(
                f"[b]{site.author}[/]{badge_str}\n"
                f"  md://{site.uri}\n"
                f"  [dim]{site.file_count} file(s) · {format_size(site.total_size)}[/]"
            )
            await list_view.append(ListItem(label, id=f"site-{site.uri}"))

    def _render_uri(self, uri: str) -> None:
        site = next((s for s in self._sites if s.uri == uri), None)
        md = self.query_one("#render", Markdown)
        if site is None:
            md.update(self._welcome_markdown())
            return
        md.update(site.score_banner_markdown() + site.markdown)
        try:
            content = self.query_one("#content", VerticalScroll)
            content.scroll_home(animate=False)
        except Exception:
            pass

    # ── event handlers ─────────────────────────────────────────────

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            await self._apply_filter(event.value)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if item is None or not item.id or not item.id.startswith("site-"):
            return
        self._render_uri(item.id[len("site-"):])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter key: same as highlighted, but also move focus to the viewer.
        item = event.item
        if item and item.id and item.id.startswith("site-"):
            self._render_uri(item.id[len("site-"):])
            self.query_one("#content", VerticalScroll).focus()

    # ── actions ────────────────────────────────────────────────────

    async def action_refresh(self) -> None:
        await self._load_sites()
        self.notify("sites reloaded", timeout=2)

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_list(self) -> None:
        self.query_one("#sites", ListView).focus()

    def action_fetch(self) -> None:
        def _handle(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            uri, naming_maddr = result
            self.notify(f"Fetching md://{uri}…", timeout=3)
            self._run_fetch(uri, naming_maddr)

        self.push_screen(
            FetchModal(default_naming=self.config.naming_multiaddr),
            callback=_handle,
        )

    @work(thread=True, exclusive=True, group="fetch")
    def _run_fetch(self, uri: str, naming_maddr: str) -> None:
        """Delegate fetching to `mdp2p fetch` in a subprocess.

        Same rationale as `_run_publish`: one process boundary isolates
        the libp2p + DHT machinery from the TUI event loop. `sys.frozen`
        picks the bundled binary in a PyInstaller release, and falls
        back to `python -m mdp2p_client` from source.
        """
        import subprocess
        import sys as _sys

        if getattr(_sys, "frozen", False):
            cmd = [_sys.executable, "fetch", "--uri", uri, "--naming", naming_maddr]
        else:
            cmd = [
                _sys.executable, "-m", "mdp2p_client",
                "fetch", "--uri", uri, "--naming", naming_maddr,
            ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            self.call_from_thread(
                self.notify,
                f"md://{uri}: fetch timed out after 60s",
                severity="error",
                timeout=10,
            )
            return
        except Exception as e:
            self.call_from_thread(
                self.notify, f"fetch failed: {e}", severity="error", timeout=10
            )
            return

        if result.returncode == 0:
            self.call_from_thread(
                self.notify, f"md://{uri} fetched ✓", severity="information"
            )
            self.call_from_thread(self._load_sites)
            self.call_from_thread(self._maybe_offer_auto_seed)
        else:
            tail = (result.stderr or result.stdout).strip().splitlines()
            reason = tail[-1] if tail else f"exit code {result.returncode}"
            self.call_from_thread(
                self.notify,
                f"md://{uri}: {reason}",
                severity="warning",
                timeout=10,
            )

    def action_inbox(self) -> None:
        if not self.config.reviewer_mode:
            self.notify(
                "reviewer mode disabled — set reviewer_mode=true in config",
                severity="warning",
            )
            return
        self._open_inbox()

    @work(thread=True, exclusive=True, group="inbox")
    def _open_inbox(self) -> None:
        """Fetch the pending inbox in a worker thread, then show the modal."""
        import json
        import subprocess
        import sys as _sys

        if getattr(_sys, "frozen", False):
            cmd = [_sys.executable, "inbox", "--json"]
        else:
            cmd = [_sys.executable, "-m", "mdp2p_client", "inbox", "--json"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(PROJECT_ROOT), timeout=30,
            )
        except Exception as e:
            self.call_from_thread(
                self.notify, f"inbox: {e}", severity="error", timeout=10,
            )
            return

        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()
            reason = tail[-1] if tail else f"exit code {result.returncode}"
            self.call_from_thread(
                self.notify, f"inbox: {reason}", severity="warning", timeout=10,
            )
            return

        try:
            pending = json.loads(result.stdout)
        except Exception as e:
            self.call_from_thread(
                self.notify, f"inbox: malformed JSON ({e})", severity="error",
            )
            return

        self.call_from_thread(self._show_inbox_modal, pending)

    def _show_inbox_modal(self, pending: list[dict]) -> None:
        def _handle(result: tuple[str, dict] | None) -> None:
            if result is None:
                return
            action, record = result
            uri = record.get("uri", "")
            content_key = record.get("content_key", "")
            if action == "fetch":
                self.notify(f"Fetching md://{uri}…", timeout=3)
                self._run_fetch(uri, self.config.naming_multiaddr)
            elif action in ("ok", "warn", "reject"):
                self.notify(f"Posting review ({action}) for md://{uri}…", timeout=3)
                self._run_review(content_key, action)

        self.push_screen(InboxModal(pending), callback=_handle)

    @work(thread=True, exclusive=True, group="review")
    def _run_review(self, content_key: str, verdict: str) -> None:
        import subprocess
        import sys as _sys

        if getattr(_sys, "frozen", False):
            cmd = [
                _sys.executable, "review",
                "--content-key", content_key, "--verdict", verdict,
            ]
        else:
            cmd = [
                _sys.executable, "-m", "mdp2p_client", "review",
                "--content-key", content_key, "--verdict", verdict,
            ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(PROJECT_ROOT), timeout=30,
            )
        except Exception as e:
            self.call_from_thread(
                self.notify, f"review failed: {e}", severity="error", timeout=10,
            )
            return

        if result.returncode == 0:
            self.call_from_thread(
                self.notify, f"review posted ({verdict}) ✓",
                severity="information",
            )
        else:
            tail = (result.stderr or result.stdout).strip().splitlines()
            reason = tail[-1] if tail else f"exit code {result.returncode}"
            self.call_from_thread(
                self.notify, f"review: {reason}", severity="warning", timeout=10,
            )

    def action_publish(self) -> None:
        if not self.config.naming_multiaddr:
            self.notify(
                "No naming server configured. Run `mdp2p setup` first.",
                severity="error",
            )
            return

        def _handle(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            uri, site_path = result
            self.notify(f"Publishing md://{uri}…", timeout=3)
            self._run_publish(uri, site_path)

        self.push_screen(PublishModal(), callback=_handle)

    @work(thread=True, exclusive=True, group="publish")
    def _run_publish(self, uri: str, site_path: str) -> None:
        """Delegate publishing to the `mdp2p publish` CLI in a subprocess.

        Same rationale as `_run_fetch`: one process boundary, one test
        surface. `sys.frozen` lets us invoke the subcommand via the
        bundled binary when running from a PyInstaller release, and via
        `python -m mdp2p_client` when running from source.
        """
        import subprocess
        import sys as _sys

        site = Path(site_path).expanduser()
        if not site.exists():
            self.call_from_thread(
                self.notify, f"Path not found: {site_path}", severity="error",
            )
            return
        if not site.is_dir():
            self.call_from_thread(
                self.notify, f"Not a directory: {site_path}", severity="error",
            )
            return
        if not list(site.rglob("*.md")):
            self.call_from_thread(
                self.notify,
                f"No .md files found under {site_path}",
                severity="error",
            )
            return

        if getattr(_sys, "frozen", False):
            cmd = [_sys.executable, "publish", "--uri", uri, "--site", str(site)]
        else:
            cmd = [
                _sys.executable, "-m", "mdp2p_client",
                "publish", "--uri", uri, "--site", str(site),
            ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.call_from_thread(
                self.notify,
                f"md://{uri}: publish timed out after 120s",
                severity="error",
                timeout=10,
            )
            return
        except Exception as e:
            self.call_from_thread(
                self.notify, f"publish failed: {e}", severity="error", timeout=10
            )
            return

        if result.returncode == 0:
            self.call_from_thread(
                self.notify, f"md://{uri} published ✓", severity="information"
            )
            self.call_from_thread(self._load_sites)
            self.call_from_thread(self._maybe_offer_auto_seed)
        else:
            tail = (result.stderr or result.stdout).strip().splitlines()
            reason = tail[-1] if tail else f"exit code {result.returncode}"
            self.call_from_thread(
                self.notify,
                f"md://{uri}: {reason}",
                severity="warning",
                timeout=10,
            )

    # ── auto-seed one-shot offer ──────────────────────────────────

    def _maybe_offer_auto_seed(self) -> None:
        """Pop the auto-seed modal the first time a publish/fetch succeeds."""
        # Re-read config from disk: subprocess invocations (publish/fetch) may
        # have flipped auto_seed_prompted while we were waiting on them.
        try:
            fresh = ClientConfig.load()
            if fresh is not None:
                self.config = fresh
        except Exception:
            pass

        if not svc.should_offer(self.config):
            return

        def _handle(accepted: bool | None) -> None:
            self.config.auto_seed_prompted = True
            try:
                self.config.save()
            except Exception:
                pass
            if accepted:
                self.notify("Installing auto-seeder service…", timeout=3)
                self._install_service()

        self.push_screen(AutoSeedModal(), callback=_handle)

    @work(thread=True, exclusive=True, group="service")
    def _install_service(self) -> None:
        """Run `mdp2p service install` in a worker thread."""
        import subprocess
        import sys as _sys

        if getattr(_sys, "frozen", False):
            cmd = [_sys.executable, "service", "install"]
        else:
            cmd = [_sys.executable, "-m", "mdp2p_client", "service", "install"]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        except Exception as e:
            self.call_from_thread(
                self.notify, f"service install failed: {e}",
                severity="error", timeout=10,
            )
            return

        if result.returncode == 0:
            self.call_from_thread(
                self.notify, "Auto-seeder enabled ✓", severity="information"
            )
        else:
            tail = (result.stderr or result.stdout).strip().splitlines()
            reason = tail[-1] if tail else f"exit code {result.returncode}"
            self.call_from_thread(
                self.notify, f"service install: {reason}",
                severity="warning", timeout=10,
            )


def run() -> None:
    """Entry point wired into the mdp2p CLI subcommand."""
    from mdp2p_logging import silence_libp2p_noise

    silence_libp2p_noise()
    config = load_or_create_config()
    Mdp2pTUI(config=config).run()


if __name__ == "__main__":
    run()
