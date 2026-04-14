"""
Browser Launcher: Find system default browser and launch with our Chrome Extension loaded.

Uses user's default browser profile (shares cookies, logins, history).
If browser is already running, launch will fail - user must manually load extension.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional
from loguru import logger


# Extension path (relative to project root)
EXTENSION_DIR = Path(__file__).parent.parent.parent.parent.parent / "extensions" / "niu-browser-ext"


def _find_default_browser() -> Optional[str]:
    """Find a suitable Chromium-based browser.

    Prefers Edge over Chrome because Edge is more permissive with
    --load-extension (developer mode extensions). Chrome requires
    manual user action to enable unpacked extensions on first run.
    """
    if sys.platform != "win32":
        for path in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
            if Path(path).is_file():
                return path
        return None

    import winreg

    # Prefer Edge (more lenient with dev-mode extensions), then Chrome
    for app_key in [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
    ]:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, app_key) as key:
                    exe_path, _ = winreg.QueryValueEx(key, None)
                    if exe_path and Path(exe_path).is_file():
                        return exe_path
            except (FileNotFoundError, OSError):
                continue

    return None


def launch_browser(url: Optional[str] = None, browser_exe: Optional[str] = None) -> subprocess.Popen:
    """
    Launch browser with Niu Browser Extension loaded.

    Uses user's default browser profile (shares cookies, logins).
    If browser is already running, this will fail - user must manually load extension.

    Args:
        url: Optional initial URL. If None, opens about:blank.
        browser_exe: Optional browser executable path. If None, auto-detects default browser.

    Returns:
        Browser process handle

    Raises:
        RuntimeError: If browser launch fails (e.g., already running)
    """
    exe_path = browser_exe or _find_default_browser()
    if not exe_path:
        raise RuntimeError(
            "Cannot find system default browser. Please install Chrome or Edge."
        )

    extension_path = str(EXTENSION_DIR.resolve())
    if not Path(extension_path, "manifest.json").is_file():
        raise FileNotFoundError(
            f"Extension not found: {extension_path}\n"
            "Please check the extension directory."
        )

    # Launch with user's default profile (no --user-data-dir specified)
    # This shares cookies, logins, history with user's normal browser session.
    # If browser is already running, this will fail - user must manually load extension.
    args = [
        exe_path,
        f"--load-extension={extension_path}",
        f"--disable-extensions-except={extension_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-component-extensions-with-background-pages",
        url or "about:blank",
    ]

    # Windows: create detached process so browser survives Python exit
    if sys.platform == "win32":
        DETACHED = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = DETACHED | CREATE_NEW_PROCESS_GROUP
    else:
        creationflags = 0

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    logger.info(f"Browser launched (PID: {proc.pid}, exe: {exe_path})")
    return proc
