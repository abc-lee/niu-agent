"""
Code Run Tool - Execute arbitrary code with safety measures

This is the most powerful tool - it can dynamically create any capability.
Security: dangerous command blacklist, sensitive data filtering, timeout.
"""

import os
import sys
import re
import asyncio
import subprocess
import tempfile
from typing import Dict, Any, List
from loguru import logger

# 危险命令黑名单
DANGEROUS_PATTERNS = [
    # 文件系统破坏
    r"\brm\s+-rf\b",
    r"\brm\s+-fr\b",
    r"\bdel\s+/[sS]\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\bchkdsk\s+/[fF]",
    r"\bfsck\b",
    # 权限提升
    r"\bsudo\s+chmod\s+[0-7]*777",
    r"\bchmod\s+[0-7]*777\s+/",
    r"\bicacls\b.*\/grant\b.*:F\b",
    # 网络危险操作
    r"\bnetsh\b.*firewall\b",
    r"\biptables\b.*-F\b",
    r"\broute\b.*delete\b",
    # 进程/服务
    r"\btaskkill\s+/[fF]",
    r"\bkill\s+-9\s+1\b",
    r"\bsystemctl\b.*(stop|disable)\b",
    # 敏感文件
    r"\b/etc/shadow\b",
    r"\b/etc/passwd\b",
    r"\b\\.ssh\\",
    r"\bauthorized_keys\b",
    # 远程执行
    r"\bcurl\b.*\|\s*bash\b",
    r"\bwget\b.*\|\s*bash\b",
]

# 敏感信息模式（日志中过滤）
SENSITIVE_PATTERNS = [
    (r'api[_-]?key\s*=\s*["\']?[\w-]{20,}["\']?', "api_key=***"),
    (r'password\s*=\s*["\']?[\w-]{8,}["\']?', "password=***"),
    (r'token\s*=\s*["\']?[\w-]{20,}["\']?', "token=***"),
    (r'secret\s*=\s*["\']?[\w-]{10,}["\']?', "secret=***"),
]


def check_dangerous_commands(code: str, code_type: str) -> List[str]:
    """检查代码中是否包含危险命令"""
    warnings = []
    code_lower = code.lower()

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            warnings.append(f"检测到危险命令模式: {pattern}")

    return warnings


def sanitize_for_log(text: str) -> str:
    """过滤敏感信息用于日志"""
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


async def code_run(
    code: str,
    code_type: str = "python",
    timeout: int = 60,
    cwd: str = None,
    stop_signal: List = None,
    skip_safety_check: bool = False,
) -> Dict[str, Any]:
    """
    Execute code and return results

    Args:
        code: Code to execute
        code_type: 'python', 'powershell', 'bash'
        timeout: Timeout in seconds (default 60)
        cwd: Working directory
        stop_signal: External stop signal (set non-empty to abort)
        skip_safety_check: Skip dangerous command check (dangerous!)

    Returns:
        {
            'status': 'success' | 'error' | 'blocked',
            'stdout': str,
            'stderr': str,
            'exit_code': int,
            'warnings': list
        }
    """
    stop_signal = stop_signal or []
    warnings = []

    # 安全检查
    if not skip_safety_check:
        danger_warnings = check_dangerous_commands(code, code_type)
        if danger_warnings:
            logger.warning(f"Blocked dangerous code: {danger_warnings}")
            return {
                "status": "blocked",
                "msg": "代码包含危险命令，已被阻止执行。如确需执行，请设置 skip_safety_check=True",
                "warnings": danger_warnings,
            }

    # 日志预览（过滤敏感信息）
    safe_preview = sanitize_for_log(code[:100].replace("\n", " "))
    if len(code) > 100:
        safe_preview += "..."
    logger.info(f"[code_run] {code_type}: {safe_preview}")

    # Default cwd
    if cwd is None:
        cwd = os.path.join(os.path.dirname(__file__), "..", "..", "temp")
    os.makedirs(cwd, exist_ok=True)

    # Prepare command
    if code_type == "python":
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".ai.py", delete=False, mode="w", encoding="utf-8", dir=cwd
        )
        tmp_file.write(code)
        tmp_file.close()
        tmp_path = tmp_file.name
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]

    elif code_type in ["powershell", "bash"]:
        if os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
        else:
            cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}

    # Execute
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    full_stdout = []

    def stream_reader(proc, logs):
        for line_bytes in iter(proc.stdout.readline, b""):
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                line = line_bytes.decode("gbk", errors="ignore")
            logs.append(line)
            logger.debug(line.rstrip())

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=cwd,
            startupinfo=startupinfo,
        )

        start_t = asyncio.get_event_loop().time()

        import threading

        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            elapsed = asyncio.get_event_loop().time() - start_t
            is_timeout = elapsed > timeout

            if is_timeout or len(stop_signal) > 0:
                process.kill()
                logger.warning(f"Process killed: timeout={is_timeout}")
                if is_timeout:
                    full_stdout.append("\n[Timeout Error] 超时强制终止")
                else:
                    full_stdout.append("\n[Stopped] 用户强制终止")
                break

            await asyncio.sleep(1)

        t.join(timeout=1)
        exit_code = process.poll() or -1

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"

        if len(stdout_str) > 10000:
            stdout_str = stdout_str[:5000] + "\n\n[omitted long output]\n\n" + stdout_str[-5000:]

        return {
            "status": status,
            "stdout": stdout_str,
            "exit_code": exit_code,
            "warnings": warnings,
        }

    except Exception as e:
        if "process" in locals():
            process.kill()
        logger.error(f"code_run error: {e}")
        return {"status": "error", "msg": str(e), "warnings": warnings}

    finally:
        if code_type == "python" and "tmp_path" in locals() and os.path.exists(tmp_path):
            os.remove(tmp_path)
