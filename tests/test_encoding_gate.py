"""AST 文本编码门禁（方案 docs/superpowers/plans/2026-09-14-utf8-encoding-unification.md §3 层 5）。

目的：保证全仓 .py 不再出现"依赖进程 locale 的文本 IO"——即未显式指定
encoding= 的文本模式 open / Path.read_text / subprocess 文本模式等写法。

规格（方案冻结）：
- 遍历面：git ls-files --cached --others --exclude-standard '*.py'
  （自动排除 python/、niu.app/、node_modules/，且包含未提交的新文件）。
- 判定方式：仅按 AST 调用节点判定（禁文本匹配）；解析失败 = 门禁失败并报文件名。
- 命中形态 R1..R8 见 RULE_DESC；豁免表按调用点局部名匹配，展开 from X import Y [as Z] 别名。

本文件同时包含**自测用例**：用内存源码片段驱动判定函数，不依赖真实仓内容，
必须在本机（macOS / python/bin/python 3.11）直接跑绿。
主门禁用例在当前仓库会报出已知违规（T2/T3 之后清零），断言信息里打印完整违规清单。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 内置豁免表（方案写死，非空属正常；变更需改本文件走 diff 评审）
# 字节/句柄 API，本身没有 encoding 参数（加了会 TypeError）。
# 按调用点局部名匹配，且展开 from X import Y [as Z] 别名。
# ---------------------------------------------------------------------------
EXEMPT_EXACT = {
    "os.open",          # fd 字节 API
    "Image.open",       # PIL 图像解码（from PIL import Image）
    "PIL.Image.open",   # 同上的模块路径形态
    "io.BytesIO",       # 内存字节流
}
EXEMPT_PREFIXES = (
    "zipfile.",         # zip 条目均为字节流
    "tarfile.",         # tar 条目均为字节流
)

_TEMPFILE_FNS = {
    "tempfile.NamedTemporaryFile",   # 缺省二进制 w+b → 未显式文本 mode 即豁免
    "tempfile.TemporaryFile",
    "tempfile.SpooledTemporaryFile",
}
_LOG_FILE_HANDLERS = {
    "FileHandler",
    "RotatingFileHandler",
    "WatchedFileHandler",
    "TimedRotatingFileHandler",
}

RULE_DESC = {
    "R1": "builtin open() / 任意 .open() 属性调用：文本模式无 encoding=",
    "R2": "Path.open() 文本模式无 encoding=",
    "R3": "read_text()/write_text()（任意接收者）无 encoding=",
    "R4": "io.open / codecs.open 文本模式无 encoding=",
    "R5": "subprocess 文本模式（text=/universal_newlines=/input=str）无 encoding=",
    "R6": "os.fdopen 文本模式 / tempfile 显式文本 mode，无 encoding=",
    "R7": "logging 文件 handler 无 encoding=",
    "R8": "mode 非字面量且无 encoding=（无法静态判定，需人工确认后登记白名单）",
    "PARSE": "Python 语法解析失败（门禁恒红直至修复）",
}

# 人工登记白名单：给"无法静态判定"（规则 R8）的个案用。
# 形态 = (文件路径, 源码片段, 理由)；目标状态 = 空。
# 与内置豁免表一样：任何条目都要改本文件才生效，杜绝注释/文件内标记绕过。
MANUAL_WHITELIST: list[tuple[str, str, str]] = []


class _Aliases:
    """收集 from X import Y [as Z] / import X 绑定（含函数体内 import），展开别名。"""

    def __init__(self) -> None:
        self.bound: dict[str, str] = {}

    @classmethod
    def scan(cls, tree: ast.AST) -> "_Aliases":
        a = cls()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    # import X.Y：无 asname 时只绑定顶层名 X，resolve() 的 func.id 兜底
                    # 结果同为顶层名 → 不登记（登记反而会把前缀叠加进点分名）。
                    if al.asname:
                        a.bound[al.asname] = al.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for al in node.names:
                    if al.name == "*":
                        continue
                    local = al.asname or al.name
                    a.bound[local] = f"{mod}.{al.name}" if mod else al.name
        return a

    def resolve(self, func: ast.AST) -> str | None:
        """把 Call.func 解析成点分名（展开别名）；无法判定返回 None。"""
        if isinstance(func, ast.Name):
            return self.bound.get(func.id, func.id)
        if isinstance(func, ast.Attribute):
            base = self.resolve(func.value)
            if base is None:
                return None
            return f"{base}.{func.attr}"
        if isinstance(func, ast.Call):
            # Path(p).open / zipfile.ZipFile(p).open：接收者是构造调用 → 递归其 func
            return self.resolve(func.func)
        return None


def _is_exempt(name: str) -> bool:
    return name in EXEMPT_EXACT or any(name.startswith(p) for p in EXEMPT_PREFIXES)


def _mode_of(call: ast.Call) -> tuple[str, str | None]:
    """取 mode：第 2 位置参数或 mode= 关键字。

    返回 ("absent", None) / ("literal", 字符串) / ("nonliteral", None)。
    """
    m = None
    if len(call.args) >= 2:
        m = call.args[1]
    else:
        for kw in call.keywords:
            if kw.arg == "mode":
                m = kw.value
                break
    if m is None:
        return "absent", None
    if isinstance(m, ast.Constant) and isinstance(m.value, str):
        return "literal", m.value
    return "nonliteral", None


def _has_encoding(call: ast.Call) -> bool:
    # 显式 encoding=None = 用 locale 默认（Python 语义）→ 视为缺失，不合规。
    for kw in call.keywords:
        if kw.arg != "encoding":
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            return False
        return True
    return False


def _check_open_like(call: ast.Call, base_rule: str) -> str | None:
    """R1/R2/R4 共用：缺省按默认 'r'（文本）判定；字面量含 'b' 才豁免。"""
    kind, mode = _mode_of(call)
    if kind == "literal" and "b" in mode:
        return None
    if _has_encoding(call):
        return None
    if kind == "nonliteral":
        return "R8"
    return base_rule


def _check_subprocess(call: ast.Call) -> str | None:
    text_mode = False
    for kw in call.keywords:
        if kw.arg in ("text", "universal_newlines"):
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                text_mode = True
        elif kw.arg == "input":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                text_mode = True
    if not text_mode or _has_encoding(call):
        return None
    return "R5"


def _check_fdopen(call: ast.Call) -> str | None:
    kind, mode = _mode_of(call)
    if kind == "literal" and "b" in mode:
        return None
    if _has_encoding(call):
        return None
    if kind == "nonliteral":
        return "R8"
    return "R6"


def _check_tempfile(call: ast.Call) -> str | None:
    """tempfile 系列缺省二进制（w+b）→ 未显式给文本 mode 即豁免。"""
    kind, mode = _mode_of(call)
    if kind == "absent":
        return None
    if kind == "literal" and "b" in mode:
        return None
    if _has_encoding(call):
        return None
    if kind == "nonliteral":
        return "R8"
    return "R6"


def analyze_call(call: ast.Call, aliases: _Aliases) -> str | None:
    """判定单个调用节点，返回规则 ID（R1..R8）或 None（合法/豁免）。"""
    # R3：read_text()/write_text() 按属性名匹配**任意接收者形态**
    # （x.read_text()、(a / b).write_text()、Path(p).read_text() 全部在内），
    # 故不依赖接收者可解析，直接按 AST 属性名判定。
    if isinstance(call.func, ast.Attribute) and call.func.attr in ("read_text", "write_text"):
        return None if _has_encoding(call) else "R3"

    resolved = aliases.resolve(call.func)
    if resolved is None:
        # fail-closed：接收者不可解析（BinOp/Subscript 等）的 .open() → 按 R1 报违规，不静默跳过
        if isinstance(call.func, ast.Attribute) and call.func.attr == "open":
            return _check_open_like(call, "R1")
        return None
    last = resolved.rsplit(".", 1)[-1]

    # R1：builtin open()
    if isinstance(call.func, ast.Name) and call.func.id == "open":
        return _check_open_like(call, "R1")
    # R4：io.open / codecs.open（必须先于通用 .open() 分支判定，否则 last=="open" 会抢走）
    if resolved in ("io.open", "codecs.open"):
        return _check_open_like(call, "R4")
    # R1/R2：任意 .open() 属性调用（内置豁免表按局部名排除字节/句柄 API）
    if last == "open" and not _is_exempt(resolved):
        if resolved == "pathlib.Path.open":
            return _check_open_like(call, "R2")
        return _check_open_like(call, "R1")
    # R5：subprocess 文本模式
    if resolved.startswith("subprocess."):
        return _check_subprocess(call)
    # R6：os.fdopen / tempfile 系列
    if resolved == "os.fdopen":
        return _check_fdopen(call)
    if resolved in _TEMPFILE_FNS:
        return _check_tempfile(call)
    # R7：logging 文件 handler
    if resolved.startswith("logging.") and last in _LOG_FILE_HANDLERS:
        return None if _has_encoding(call) else "R7"
    return None


def _walk_calls(tree: ast.AST, lines: list[str]) -> list[tuple[int, str, str]]:
    aliases = _Aliases.scan(tree)
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rule = analyze_call(node, aliases)
            if rule is None:
                continue
            ln = node.lineno
            text = lines[ln - 1].strip() if ln <= len(lines) else ""
            out.append((ln, rule, text))
    return out


def check_source(source: str) -> list[tuple[int, str, str]]:
    """判定内存源码片段，返回 [(lineno, rule_id, source_line), ...]。

    解析失败 → 单条 PARSE（门禁恒红并报行号）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [(e.lineno or 0, "PARSE", f"SyntaxError: {e.msg} (line {e.lineno})")]
    return _walk_calls(tree, source.splitlines())


def check_file(path: Path) -> list[tuple[int, str, str]]:
    """判定单个文件（按字节解析，兼容 PEP 263 coding 声明）。"""
    data = path.read_bytes()
    try:
        tree = ast.parse(data)
    except SyntaxError as e:
        return [(e.lineno or 0, "PARSE", f"SyntaxError: {e.msg} (line {e.lineno})")]
    text = data.decode("utf-8", errors="replace").splitlines()
    return _walk_calls(tree, text)


def _whitelisted(path_name: str, line: str) -> bool:
    return any(path_name == f and frag in line for f, frag, _reason in MANUAL_WHITELIST)


def _py_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=REPO_ROOT, capture_output=True, encoding="utf-8", errors="replace",
    )
    files: list[str] = []
    for name in proc.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        # 双保险：gitignore 已排除 python/、niu.app/、node_modules/，此处再按前缀过滤
        if name.startswith(("python/", "niu.app/", "node_modules/")):
            continue
        files.append(name)
    return files


def collect_violations() -> list[tuple[str, int, str, str]]:
    """全仓扫描，返回 [(file, lineno, rule_id, source_line), ...]（按文件/行号排序）。"""
    out: list[tuple[str, int, str, str]] = []
    for name in _py_files():
        for ln, rule, line in check_file(REPO_ROOT / name):
            if _whitelisted(name, line):
                continue
            out.append((name, ln, rule, line))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


# ---------------------------------------------------------------------------
# 主门禁：当前仓库已知违规（T2/T3 清零前允许红），断言信息打印完整清单供对账。
# ---------------------------------------------------------------------------

def test_repo_has_no_locale_dependent_text_io() -> None:
    violations = collect_violations()
    if not violations:
        return
    detail = "\n".join(
        f"{f}:{ln} [{r}] {RULE_DESC[r]}\n    | {s}" for f, ln, r, s in violations
    )
    assert False, (
        f"编码门禁：发现 {len(violations)} 处依赖进程 locale 的文本 IO\n{detail}"
    )


# ---------------------------------------------------------------------------
# 自测用例（内存源码片段驱动，不依赖真实仓内容；本机必须全绿）
# ---------------------------------------------------------------------------

def _rules(source: str) -> list[str]:
    return [r for _ln, r, _line in check_source(source)]


def test_alias_import_exempt() -> None:
    """模块级 from PIL import Image as I 后 I.open(p) → 豁免（绿）。"""
    src = 'from PIL import Image as I\np = "x.jpg"\nI.open(p)\n'
    assert _rules(src) == []


def test_function_body_import_exempt() -> None:
    """函数体内 from PIL import Image 后 Image.open(p) → 豁免（绿）。"""
    src = (
        "def f(p):\n"
        "    from PIL import Image\n"
        "    return Image.open(p)\n"
    )
    assert _rules(src) == []


def test_path_read_text_any_receiver_violates() -> None:
    """Path(p).read_text()（任意接收者形态）→ R3 违规（红）。"""
    src = 'from pathlib import Path\np = "a"\nPath(p).read_text()\n'
    assert _rules(src) == ["R3"]


def test_open_variants() -> None:
    """open 各 mode 形态：文本模式无 encoding 违规，二进制豁免。"""
    assert _rules('open("p", "w")') == ["R1"]
    assert _rules('open("p", mode="w")') == ["R1"]
    assert _rules('open("p")') == ["R1"]
    assert _rules('open("p", "rb")') == []


def test_tempfile_default_binary_exempt() -> None:
    """tempfile.NamedTemporaryFile(suffix=".db") 缺省二进制 → 豁免（绿）。"""
    src = 'import tempfile\ntempfile.NamedTemporaryFile(suffix=".db")\n'
    assert _rules(src) == []


def test_tempfile_text_mode_violates() -> None:
    """tempfile.NamedTemporaryFile(mode="w") 显式文本 mode 无 encoding → R6（红）。"""
    src = 'import tempfile\ntempfile.NamedTemporaryFile(mode="w")\n'
    assert _rules(src) == ["R6"]


def test_subprocess_text_mode() -> None:
    """subprocess.run(x, text=True) 无 encoding → R5（红）；带 encoding="utf-8" → 绿。"""
    assert _rules('import subprocess\nsubprocess.run(["x"], text=True)\n') == ["R5"]
    assert _rules(
        'import subprocess\nsubprocess.run(["x"], text=True, encoding="utf-8")\n'
    ) == []


def test_parse_failure_reported() -> None:
    """语法非法源码 → 门禁报 PARSE 失败（含行号）。"""
    violations = check_source("def f(:\n    pass\n")
    assert [r for _ln, r, _line in violations] == ["PARSE"]


def test_open_on_call_receiver_violates() -> None:
    """接收者是构造调用/表达式时 .open() 不得逃逸：
    Path("p").open("w") → R2；(a / b).open("w") 接收者不可解析 → R1 fail-closed。"""
    src = 'from pathlib import Path\nPath("p").open("w")\n'
    assert _rules(src) == ["R2"]
    assert _rules('(a / b).open("w")') == ["R1"]


def test_open_on_zipfile_call_receiver_exempt() -> None:
    """zipfile.ZipFile("p").open("x") → 命中 zipfile. 前缀豁免（绿）。"""
    src = 'import zipfile\nzipfile.ZipFile("p").open("x")\n'
    assert _rules(src) == []


def test_io_open_is_r4() -> None:
    """io.open("p","r") 必须判 R4，不得被通用 .open() 分支抢成 R1。"""
    src = 'import io\nio.open("p", "r")\n'
    assert _rules(src) == ["R4"]


def test_dotted_import_binding() -> None:
    """import PIL.Image 绑定顶层名 PIL：PIL.Image.open(p) 豁免；
    import os.path + os.fdopen(1) → R6（根名不得被绑成 os.path）。"""
    src = 'import PIL.Image\np = "x.jpg"\nPIL.Image.open(p)\n'
    assert _rules(src) == []
    assert _rules('import os.path\nos.fdopen(1)\n') == ["R6"]


def test_dotted_import_asname() -> None:
    """import PIL.Image as I + I.open(p) → 豁免（绿）。"""
    src = 'import PIL.Image as I\np = "x.jpg"\nI.open(p)\n'
    assert _rules(src) == []


def test_encoding_none_counts_as_missing() -> None:
    """encoding=None = 用 locale 默认 → 视为缺失：open R1、subprocess R5。"""
    assert _rules('open("p", "w", encoding=None)') == ["R1"]
    assert _rules('import subprocess\nsubprocess.run(["x"], text=True, encoding=None)\n') == ["R5"]
