"""Chrome 浏览器启动器"""
import subprocess
import sys
from pathlib import Path
from typing import Optional


class BrowserLauncher:
    """启动 Chrome 浏览器并加载扩展"""

    def __init__(
        self,
        extension_path: str,
        port: int = 38401,
        chrome_binary: Optional[str] = None
    ):
        self.extension_path = extension_path
        self.port = port
        self.chrome_binary = chrome_binary
        self.process: Optional[subprocess.Popen] = None

    def launch(self) -> subprocess.Popen:
        """启动浏览器"""
        chrome_path = self.chrome_binary or self._find_chrome_binary()

        cmd = [
            chrome_path,
            f"--load-extension={self.extension_path}",
            f"--disable-extensions-except={self.extension_path}",
            f"--app=http://localhost:{self.port}",  # 打开 hub 页面
            "--no-first-run",
            "--no-default-browser-check",
        ]

        # Windows 下需要特殊处理
        if sys.platform == "win32":
            # 使用 CREATE_NEW_PROCESS_GROUP 避免父进程退出时子进程被杀
            self.process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            self.process = subprocess.Popen(cmd)

        return self.process

    def shutdown(self):
        """关闭浏览器"""
        if self.process:
            try:
                self.process.kill()
            except Exception:
                pass
            finally:
                self.process = None

    def _find_chrome_binary(self) -> str:
        """自动检测 Chrome 可执行文件路径"""
        if sys.platform == "win32":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            ]
        elif sys.platform == "darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        else:  # Linux
            candidates = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium-browser",
            ]

        for path in candidates:
            if isinstance(path, str):
                if Path(path).exists():
                    return path
            else:
                if path.exists():
                    return str(path)

        raise FileNotFoundError("Chrome not found. Please specify chrome_binary parameter.")
