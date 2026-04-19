"""
User-level service installer for the mdp2p seeder.

Provides platform-specific integration so `mdp2p serve` can be run as a
long-lived background service that starts on login:

- macOS  : LaunchAgent (`~/Library/LaunchAgents/net.mdp2p.seeder.plist`)
- Linux  : systemd --user unit (`~/.config/systemd/user/mdp2p-seeder.service`)
- Windows: Task Scheduler task (`mdp2p-seeder`)

Pure stdlib. No external dependencies.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LAUNCHD_LABEL = "net.mdp2p.seeder"
SYSTEMD_UNIT_NAME = "mdp2p-seeder.service"
WINDOWS_TASK_NAME = "mdp2p-seeder"


# ---------------------------------------------------------------------------
# Platform detection and launch command
# ---------------------------------------------------------------------------


def get_platform() -> str:
    """Return one of: "darwin", "linux", "windows", "unsupported"."""
    p = sys.platform
    if p == "darwin":
        return "darwin"
    if p.startswith("linux"):
        return "linux"
    if p in ("win32", "cygwin"):
        return "windows"
    return "unsupported"


def _is_frozen() -> bool:
    """True when running from a PyInstaller-style bundle."""
    return bool(getattr(sys, "frozen", False))


def get_launch_command() -> list[str]:
    """Return the argv list used to launch the seeder.

    For frozen bundles, sys.executable is the mdp2p binary itself, so
    we pass `serve` as a subcommand. For source installs, we invoke the
    same Python interpreter with `-m mdp2p_client serve` so the installed
    service picks up the exact interpreter (and thus venv) that installed it.
    """
    if _is_frozen():
        return [sys.executable, "serve"]
    return [sys.executable, "-m", "mdp2p_client", "serve"]


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing both streams, never raising unless asked."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# macOS — LaunchAgent
# ---------------------------------------------------------------------------


def _darwin_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _darwin_log_paths() -> tuple[Path, Path]:
    log_dir = Path.home() / "Library" / "Logs"
    return log_dir / "mdp2p-seeder.log", log_dir / "mdp2p-seeder.err"


def _darwin_service_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def _darwin_domain() -> str:
    return f"gui/{os.getuid()}"


def _darwin_write_plist(path: Path) -> None:
    """Write the LaunchAgent plist using stdlib plistlib."""
    out_log, err_log = _darwin_log_paths()
    out_log.parent.mkdir(parents=True, exist_ok=True)

    program_args = get_launch_command()
    plist_data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
        "ProcessType": "Background",
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist_data, f)


def _darwin_install() -> tuple[bool, str, str]:
    plist = _darwin_plist_path()
    _darwin_write_plist(plist)

    # Prefer modern `bootstrap`; fall back to legacy `load` on older macOS
    # or when the agent is already bootstrapped.
    bootstrap = _run(
        ["launchctl", "bootstrap", _darwin_domain(), str(plist)]
    )
    if bootstrap.returncode == 0:
        return True, str(plist), "LaunchAgent installed and bootstrapped."

    load = _run(["launchctl", "load", str(plist)])
    if load.returncode == 0:
        return True, str(plist), "LaunchAgent installed (loaded via legacy launchctl load)."

    # Both failed — surface the exact error so the user can decide.
    msg = (
        "launchctl bootstrap failed:\n"
        f"  {bootstrap.stderr.strip() or bootstrap.stdout.strip()}\n"
        "launchctl load also failed:\n"
        f"  {load.stderr.strip() or load.stdout.strip()}"
    )
    return False, str(plist), msg


def _darwin_uninstall() -> tuple[bool, str]:
    plist = _darwin_plist_path()

    if not plist.exists():
        return True, "Nothing to uninstall (plist not found)."

    # Modern boot-out, then legacy unload. Ignore individual failures;
    # the goal is to leave the system clean.
    _run(["launchctl", "bootout", _darwin_service_target()])
    _run(["launchctl", "unload", str(plist)])

    try:
        plist.unlink()
    except OSError as e:
        return False, f"Failed to delete {plist}: {e}"

    return True, f"LaunchAgent removed ({plist})."


def _darwin_status() -> dict:
    plist = _darwin_plist_path()
    installed = plist.exists()

    running = False
    details = ""
    if installed:
        result = _run(["launchctl", "print", _darwin_service_target()])
        if result.returncode == 0:
            # `state = running` means the process is live;
            # "not running" or absence of that line means loaded-but-exited.
            for line in result.stdout.splitlines():
                if "state" in line and "=" in line:
                    if "running" in line.split("=", 1)[1]:
                        running = True
                        break
            details = "loaded"
            if running:
                details += ", running"
            else:
                details += ", not running"
        else:
            details = "plist present but not loaded in launchd"

    return {
        "platform": "darwin",
        "installed": installed,
        "running": running,
        "path": str(plist) if installed else None,
        "details": details or "not installed",
    }


# ---------------------------------------------------------------------------
# Linux — systemd --user
# ---------------------------------------------------------------------------


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _systemd_exec_start() -> str:
    """Build a properly quoted ExecStart= value."""
    return " ".join(shlex.quote(arg) for arg in get_launch_command())


def _systemd_unit_content() -> str:
    return (
        "[Unit]\n"
        "Description=MDP2P seeder (keeps your sites alive on the P2P network)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={_systemd_exec_start()}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_linger_hint() -> Optional[str]:
    """Return a hint string if user lingering is not enabled, else None."""
    user = os.environ.get("USER") or ""
    if not user:
        return None

    result = _run(
        ["loginctl", "show-user", shlex.quote(user), "-p", "Linger", "--value"]
    )
    if result.returncode != 0:
        return None

    if result.stdout.strip().lower() != "yes":
        return (
            f"Run `loginctl enable-linger {user}` so the seeder keeps running "
            "after logout (requires sudo on most distros)."
        )
    return None


def _linux_install() -> tuple[bool, str, str]:
    unit = _systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_systemd_unit_content())

    reload_result = _run(["systemctl", "--user", "daemon-reload"])
    if reload_result.returncode != 0:
        return False, str(unit), (
            "systemctl --user daemon-reload failed:\n"
            f"  {reload_result.stderr.strip() or reload_result.stdout.strip()}"
        )

    enable_result = _run(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME]
    )
    if enable_result.returncode != 0:
        return False, str(unit), (
            "systemctl --user enable --now failed:\n"
            f"  {enable_result.stderr.strip() or enable_result.stdout.strip()}"
        )

    msg = "systemd --user unit installed and started."
    hint = _systemd_linger_hint()
    if hint:
        msg += f"\nHint: {hint}"
    return True, str(unit), msg


def _linux_uninstall() -> tuple[bool, str]:
    unit = _systemd_unit_path()

    # Best-effort disable — ignore non-zero exit.
    _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])

    if unit.exists():
        try:
            unit.unlink()
        except OSError as e:
            return False, f"Failed to delete {unit}: {e}"

    _run(["systemctl", "--user", "daemon-reload"])
    return True, f"systemd --user unit removed ({unit})."


def _linux_status() -> dict:
    unit = _systemd_unit_path()
    installed = unit.exists()

    running = False
    details_parts: list[str] = []

    if installed:
        is_active = _run(
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME]
        )
        active_state = is_active.stdout.strip() or is_active.stderr.strip()
        running = is_active.returncode == 0 and active_state == "active"
        details_parts.append(f"active={active_state or 'unknown'}")

        is_enabled = _run(
            ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME]
        )
        enabled_state = is_enabled.stdout.strip() or is_enabled.stderr.strip()
        details_parts.append(f"enabled={enabled_state or 'unknown'}")

    return {
        "platform": "linux",
        "installed": installed,
        "running": running,
        "path": str(unit) if installed else None,
        "details": ", ".join(details_parts) if details_parts else "not installed",
    }


# ---------------------------------------------------------------------------
# Windows — Task Scheduler (schtasks)
# ---------------------------------------------------------------------------


def _windows_command_string() -> str:
    """Build the command string for `schtasks /tr`, quoting each piece."""
    parts: list[str] = []
    for arg in get_launch_command():
        # schtasks expects a single string; quote anything with spaces.
        if " " in arg and not (arg.startswith('"') and arg.endswith('"')):
            parts.append(f'"{arg}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _windows_install() -> tuple[bool, str, str]:
    cmd_string = _windows_command_string()
    result = _run(
        [
            "schtasks",
            "/create",
            "/tn",
            WINDOWS_TASK_NAME,
            "/tr",
            cmd_string,
            "/sc",
            "onlogon",
            "/f",
        ]
    )
    if result.returncode != 0:
        return False, WINDOWS_TASK_NAME, (
            "schtasks /create failed:\n"
            f"  {result.stderr.strip() or result.stdout.strip()}"
        )
    return True, WINDOWS_TASK_NAME, "Scheduled Task created (trigger: at logon)."


def _windows_uninstall() -> tuple[bool, str]:
    result = _run(
        ["schtasks", "/delete", "/tn", WINDOWS_TASK_NAME, "/f"]
    )
    if result.returncode != 0:
        # Task not found is still "nothing to do" — report as success.
        stderr = (result.stderr or "").lower()
        if "cannot find" in stderr or "does not exist" in stderr:
            return True, "Nothing to uninstall (task not found)."
        return False, (
            "schtasks /delete failed:\n"
            f"  {result.stderr.strip() or result.stdout.strip()}"
        )
    return True, f"Scheduled Task removed ({WINDOWS_TASK_NAME})."


def _windows_status() -> dict:
    result = _run(
        [
            "schtasks",
            "/query",
            "/tn",
            WINDOWS_TASK_NAME,
            "/fo",
            "csv",
            "/nh",
        ]
    )

    if result.returncode != 0:
        return {
            "platform": "windows",
            "installed": False,
            "running": False,
            "path": None,
            "details": "not installed",
        }

    running = False
    status_field = ""
    last_result_field = ""

    # CSV format: "TaskName","Next Run Time","Status"
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if line:
        fields = [f.strip('"') for f in line.split(",")]
        if len(fields) >= 3:
            status_field = fields[2]
            running = status_field.lower() == "running"

    # A second query with /v would give Last Result; kept lightweight.
    details = f"status={status_field or 'unknown'}"
    if last_result_field:
        details += f", last_result={last_result_field}"

    return {
        "platform": "windows",
        "installed": True,
        "running": running,
        "path": WINDOWS_TASK_NAME,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install() -> tuple[bool, str, str]:
    """Install the seeder as a user-level auto-starting service."""
    plat = get_platform()
    if plat == "darwin":
        return _darwin_install()
    if plat == "linux":
        return _linux_install()
    if plat == "windows":
        return _windows_install()
    return False, "", f"Unsupported platform: {sys.platform}"


def uninstall() -> tuple[bool, str]:
    """Remove the seeder service. ok=True even if nothing was installed."""
    plat = get_platform()
    if plat == "darwin":
        return _darwin_uninstall()
    if plat == "linux":
        return _linux_uninstall()
    if plat == "windows":
        return _windows_uninstall()
    return False, f"Unsupported platform: {sys.platform}"


def status() -> dict:
    """Return current install/run state for the service on this platform."""
    plat = get_platform()
    if plat == "darwin":
        return _darwin_status()
    if plat == "linux":
        return _linux_status()
    if plat == "windows":
        return _windows_status()
    return {
        "platform": "unsupported",
        "installed": False,
        "running": False,
        "path": None,
        "details": f"Unsupported platform: {sys.platform}",
    }


# ---------------------------------------------------------------------------
# First-run prompt helpers (hooked into first publish / first fetch)
# ---------------------------------------------------------------------------


def should_offer(config) -> bool:
    """Return True if we should offer to install the auto-seeder service.

    The offer fires the FIRST time the user does something that produces a
    local site worth seeding (publish or fetch). We skip when:
    - the user has already been asked (regardless of their answer),
    - the service is already installed,
    - the platform has no integration path.
    """
    if getattr(config, "auto_seed_prompted", False):
        return False
    if get_platform() == "unsupported":
        return False
    try:
        if status().get("installed"):
            # Already installed: remember that and stop asking.
            _mark_prompted(config)
            return False
    except Exception:
        return False
    return True


def _mark_prompted(config) -> None:
    """Persist that the user has been offered the service prompt."""
    config.auto_seed_prompted = True
    try:
        config.save()
    except Exception:
        # Best-effort — if the config can't be saved we'll re-ask next time.
        pass


def offer_interactive(config) -> bool:
    """Prompt stdin to install the auto-seeder service. Returns True on install.

    No-op when stdin is not a TTY (e.g., when invoked as a subprocess by the
    TUI), so piped runs never hang on input. The TUI provides its own modal.
    """
    if not should_offer(config):
        return False
    if not sys.stdin.isatty():
        return False

    print()
    print("  Your sites only stay reachable on the P2P network while")
    print("  an mdp2p process is running. Install a background service")
    print("  so seeding starts automatically at login?")
    print()
    try:
        answer = input("  Enable auto-seeding? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        _mark_prompted(config)
        return False

    if answer in ("", "y", "yes", "o", "oui"):
        ok, path, msg = install()
        if ok:
            print(f"  ✓ {msg}")
            print(f"    Service file: {path}")
            print("    Run `mdp2p service status` to check, or `mdp2p service uninstall` to remove.")
        else:
            print(f"  ✗ {msg}")
        _mark_prompted(config)
        return ok

    print("  Skipped. You can install it later with: mdp2p service install")
    _mark_prompted(config)
    return False
