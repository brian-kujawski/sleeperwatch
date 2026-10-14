"""Where sleeperwatch keeps its files, per platform.

Defaults follow each OS's conventions so nothing depends on the directory
you happen to run from, and nothing lands inside the git checkout:

               config (watchlist.toml)              data (DB, browser profile, charts)
  Linux/BSD    $XDG_CONFIG_HOME or ~/.config         $XDG_DATA_HOME or ~/.local/share
  macOS        ~/Library/Application Support         ~/Library/Application Support
  Windows      %APPDATA%                             %LOCALAPPDATA%

(each followed by /sleeperwatch). Override with --watchlist / --data-dir or
the SLEEPERWATCH_WATCHLIST / SLEEPERWATCH_DATA environment variables.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP = "sleeperwatch"


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(_home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / APP


def data_dir() -> Path:
    env = os.environ.get("SLEEPERWATCH_DATA")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(_home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(_home() / ".local" / "share")
    return Path(base) / APP


def watchlist_path(explicit: str | None = None) -> Path:
    """--watchlist, then $SLEEPERWATCH_WATCHLIST, then ./watchlist.toml if it
    exists (handy when working in a checkout), then the config directory."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("SLEEPERWATCH_WATCHLIST")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "watchlist.toml"
    if local.exists():
        return local
    return config_dir() / "watchlist.toml"


def safe_filename(name: str) -> str:
    """Make a string safe as a file name on Windows, macOS and Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned or "unnamed"


def has_display() -> bool:
    """Whether a visible browser window can open. Only Linux/BSD can lack
    one (servers, SSH sessions); macOS and Windows always have a desktop."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
