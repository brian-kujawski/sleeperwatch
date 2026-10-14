"""Find an installed Chromium-family browser and start it with a DevTools
port that Playwright can attach to, on Linux, macOS and Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Command names (Linux/BSD, looked up on PATH) and fixed install paths per OS,
# in order of preference for each channel.
_LINUX = {
    "chrome": ["google-chrome-stable", "google-chrome", "/opt/google/chrome/chrome"],
    "chromium": ["chromium", "chromium-browser"],
    "msedge": ["microsoft-edge-stable", "microsoft-edge", "/opt/microsoft/msedge/msedge"],
    "flatpak": [
        "/var/lib/flatpak/exports/bin/com.google.Chrome",
        "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
        "~/.local/share/flatpak/exports/bin/com.google.Chrome",
        "~/.local/share/flatpak/exports/bin/org.chromium.Chromium",
    ],
}
_MAC = {
    "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
}


def _win_paths() -> dict[str, list[str]]:
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    roots = [r for r in roots if r]
    return {
        "chrome": [str(Path(r, "Google", "Chrome", "Application", "chrome.exe")) for r in roots],
        "chromium": [str(Path(r, "Chromium", "Application", "chrome.exe")) for r in roots],
        "msedge": [str(Path(r, "Microsoft", "Edge", "Application", "msedge.exe")) for r in roots],
    }


def _table() -> dict[str, list[str]]:
    if sys.platform == "win32":
        return _win_paths()
    if sys.platform == "darwin":
        return _MAC
    return _LINUX


def _order(channel: str) -> list[str]:
    # Prefer what was asked for, then any other Chromium-family browser.
    order = {"chrome": ["chrome", "chromium", "msedge", "flatpak"],
             "msedge": ["msedge", "chrome", "chromium", "flatpak"]}
    return order.get(channel, [channel])


def candidate_names(channel: str) -> list[str]:
    table = _table()
    return [c for key in _order(channel) for c in table.get(key, [])]


def find_browser(channel: str = "chrome") -> str | None:
    for cand in candidate_names(channel):
        if os.sep in cand or "/" in cand:
            p = Path(cand).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def profile_dir_for(exe: str, default: Path) -> Path:
    """Snap and Flatpak browsers can't write to hidden folders like
    ~/.local/share, so give them a profile inside their own sandbox area."""
    real = os.path.realpath(exe)
    if "/snap/" in real or exe.startswith("/snap/") or _is_snap_wrapper(real):
        name = Path(exe).name.split(".")[0] or "chromium"
        snap = "chromium" if "chromium" in name else name
        return Path.home() / "snap" / snap / "common" / "sleeperwatch-profile"
    if "/flatpak/exports/bin/" in exe:
        return Path.home() / ".var" / "app" / Path(exe).name / "sleeperwatch-profile"
    return default


def _is_snap_wrapper(path: str) -> bool:
    """Ubuntu's /usr/bin/chromium-browser is a small shell script that runs
    the snap; the real binary is never on PATH."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"snap" in head


def start_with_debug_port(exe: str, profile: Path, headless: bool = False,
                          timeout: float = 30.0) -> tuple[subprocess.Popen, str]:
    """Start the browser on a free DevTools port; return (process, endpoint).

    Port 0 lets the browser pick a free port and write it to
    <profile>/DevToolsActivePort, which avoids races between processes."""
    profile.mkdir(parents=True, exist_ok=True)
    port_file = profile / "DevToolsActivePort"
    try:
        port_file.unlink()
    except FileNotFoundError:
        pass

    args = [
        exe,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1366,900",
        # An open debugging port alone makes Chromium report
        # navigator.webdriver = true (verified on Chromium 141), which bot
        # detection checks first. This turns that back off. Chrome shows an
        # "unsupported command-line flag" bar for it; that's cosmetic.
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")

    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, **kwargs)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8", errors="replace").split()
            if lines and lines[0].isdigit():
                return proc, f"http://127.0.0.1:{lines[0]}"
        if proc.poll() is not None:
            raise SystemExit(
                f"The browser exited immediately (code {proc.returncode}). Usually this means a "
                f"sleeperwatch browser window is already open on the same profile ({profile}); "
                "close it and try again.")
        time.sleep(0.25)
    stop(proc)
    raise SystemExit(f"Timed out waiting for the browser to open its debugging port ({exe}).")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
