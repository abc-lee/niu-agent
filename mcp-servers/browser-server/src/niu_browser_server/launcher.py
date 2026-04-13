"""
Browser Launcher: Find system default browser and launch with our Chrome Extension loaded.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional
from loguru import logger


# Extension path (relative to project root)
EXTENSION_DIR = Path(__file__).parent.parent.parent.parent.parent / "extensions" / "niu-browser-ext"

# User data directory (separate profile to avoid conflicts with user's regular browser)
USER_DATA_DIR = Path.home() / ".niu" / "browser_ext_profile"


def _find_default_browser() -> Optional[str]:
    """Find Windows system default browser executable path."""
    if sys.platform != "win32":
        # On non-Windows, try common paths
        for path in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
            if Path(path).is_file():
                return path
        return None

    import winreg

    # 1. Get ProgId for HTTPS protocol handler
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except (FileNotFoundError, OSError):
        prog_id = None

    # 2. ProgId -> App Paths registry key
    progid_to_appkey = {
        "MSEdgeHTM": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        "ChromeHTML": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
    }

    if prog_id in progid_to_appkey:
        app_key = progid_to_appkey[prog_id]
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, app_key) as key:
                    exe_path, _ = winreg.QueryValueEx(key, None)
                    if exe_path and Path(exe_path).is_file():
                        return exe_path
            except (FileNotFoundError, OSError):
                continue

    # 3. Parse shell\open\command from ProgId
    if prog_id:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\shell\\open\\command"
            ) as key:
                command, _ = winreg.QueryValueEx(key, None)
                if command.startswith('"'):
                    end = command.index('"', 1)
                    exe_path = command[1:end]
                    if Path(exe_path).is_file():
                        return exe_path
        except (FileNotFoundError, OSError, ValueError):
            pass

    return None


def launch_browser(url: Optional[str] = None) -> subprocess.Popen:
    """
    Launch system default browser with Niu Browser Extension loaded.

    Args:
        url: Optional initial URL. If None, opens about:blank.

    Returns:
        Browser process handle
    """
    exe_path = _find_default_browser()
    if not exe_path:
        raise RuntimeError(
            "Cannot find system default browser. Please install Chrome or Edge."
        )

    extension_path = str(EXTENSION_DIR.resolve())
    if not Path(extension_path, "manifest.json").is_file():
        raise FileNotFoundError(
            f"Extension not found: {extension_path}\n"
            "Please refer to the system manual for installing Niu Browser Extension."
        )

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        exe_path,
        f"--load-extension={extension_path}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
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
