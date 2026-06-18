# tests/test_tilde_expansion.py
"""测试 expand_path_args() 对 ~/ 路径参数的自动展开"""
import os


def test_expand_tilde_in_path_args():
    from agent.handler import expand_path_args
    args = {"file_path": "~/test.json", "content": "hello"}
    expand_path_args(args)
    assert args["file_path"] == os.path.expanduser("~/test.json")
    assert "~" not in args["file_path"]
    assert args["content"] == "hello"


def test_no_expand_non_tilde_path():
    from agent.handler import expand_path_args
    args = {"file_path": "/Users/xxx/test.json", "content": "hello"}
    expand_path_args(args)
    assert args["file_path"] == "/Users/xxx/test.json"


def test_no_expand_non_path_args():
    from agent.handler import expand_path_args
    args = {"content": "~/some/text", "command": "ls ~/Documents"}
    expand_path_args(args)
    assert args["content"] == "~/some/text"
    assert args["command"] == "ls ~/Documents"


def test_expand_multiple_path_args():
    from agent.handler import expand_path_args
    args = {"source_path": "~/src/file.txt", "dest_path": "~/dst/file.txt"}
    expand_path_args(args)
    assert "~" not in args["source_path"]
    assert "~" not in args["dest_path"]


def test_expand_none_value_skipped():
    from agent.handler import expand_path_args
    args = {"file_path": None, "path": "~/test"}
    expand_path_args(args)
    assert args["file_path"] is None
    assert "~" not in args["path"]


def test_expand_path_args_called_in_dispatch():
    """验证 expand_path_args 被调用后路径参数已展开"""
    from agent.handler import expand_path_args
    args = {"file_path": "~/test_dispatch.json", "content": "hello"}
    expand_path_args(args)
    assert "~" not in args["file_path"]
    assert args["file_path"] == os.path.expanduser("~/test_dispatch.json")
