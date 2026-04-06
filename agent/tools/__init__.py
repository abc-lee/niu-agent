"""
Tools - Atomic tools for Niu Agent

Based on GenericAgent's tool philosophy:
- code_run: Execute arbitrary code (dynamic capability creation)
- file_read/write/patch: File operations
- web_fetch/search: Simple web operations
- ask_user: Human-in-the-loop
"""

from .code_run import code_run
from .file_ops import file_read, file_write, file_patch
from .web_ops import web_fetch, web_search
from .ask_user import ask_user

__all__ = [
    "code_run",
    "file_read",
    "file_write",
    "file_patch",
    "web_fetch",
    "web_search",
    "ask_user",
]
