"""Sudo-chown helper to recover from PermissionError on the config directory."""

import getpass
import subprocess
import sys
from pathlib import Path

from . import colors as c
from .config import DEFAULT_CONFIG_DIR
from .i18n import t
from .ui import prompt_input


def resolve_chown_target(error_path: str) -> str:
    """Determine the root directory to chown from the failing file path."""
    config_dir = str(DEFAULT_CONFIG_DIR)
    if error_path.startswith(config_dir):
        return config_dir
    p = Path(error_path)
    return str(p.parent if not p.is_dir() else p)


def fix_permissions(e: PermissionError) -> bool:
    """Offer to fix permissions via sudo chown. Returns True if fixed."""
    error_path = e.filename or str(e)
    target = resolve_chown_target(error_path)

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
