# Mac 兼容性问题修复报告

## 已完成的修复（mac-compat-dev 分支）

### 1. Python 路径检测修复
**文件**: `agent/mcp_client.py`, `main.go`

**问题**: Mac/Linux 下 Python 路径应该是 `bin/python3` 而不是 `Scripts/python.exe`

**修复**:
```go
// main.go detectPython()
if runtime.GOOS == "darwin" || runtime.GOOS == "linux" {
    candidates = []string{
        filepath.Join(homeDir, ".niu-venv", "bin", "python3"),
        filepath.Join(homeDir, ".venv", "bin", "python3"),
        // ...
    }
}
```

### 2. 虚拟环境优先级
**文件**: `main.go`

**问题**: 应该优先检测用户虚拟环境 `~/.niu-venv`

**修复**: 将 `~/.niu-venv` 放在候选列表首位

### 3. embedding 模型路径修复
**文件**: `niu_api/internal/embedding.py`

**问题**: 路径计算错误，模型目录定位到 4 层上级而非 3 层

**修复**: `Path(__file__).parent.parent.parent / "models"` （原来是 4 层）

### 4. HOME 目录环境变量优先级
**文件**: `mcp-servers/config-manager/__init__.py`

**修复**: 改为 `["HOME", "USERPROFILE", "HOMEPATH"]`

### 5. 跨平台路径处理
**文件**: `mcp-servers/photo-server/__init__.py`

**修复**: 将硬编码的 `E:/tools/ai-bot/logs` 改为 `Path.home() / ".niu" / "logs"`

### 6. 日志编码修复
**文件**: `agent/handler.py`, `agent/generic/handler.py`, `agent/tools/code_run.py`

**问题**: Windows GBK 编码在 Mac 上不存在

**修复**: 改为 `latin-1` 作为 fallback 编码

### 7. PYTHONPATH 分隔符
**文件**: `agent/mcp_client.py`

**修复**: Windows 用 `;`，Unix 用 `:`

---

## 发现但未修复的问题

### memory-server 未读取 memory.json 配置

**文件**: `mcp-servers/memory-server/src/niu_memory_server/storage.py`

**问题**: `get_db_path()` 只读取 `WORKSPACE_PATH` 环境变量，没有读取 `~/.niu/memory.json` 中的 `workspace.path`

**当前代码**:
```python
def get_db_path() -> str:
    workspace = os.environ.get("WORKSPACE_PATH", ".")
    return os.path.join(workspace, "vectors.db")
```

**应该参考**: `agent/vector_search.py` 第 46-62 行的实现：
```python
@staticmethod
def _default_db_path() -> str:
    # 1. 尝试从 memory.json 读取工作目录
    memory_path = os.path.join(os.path.expanduser("~"), ".niu", "memory.json")
    if os.path.exists(memory_path):
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                memory = json.load(f)
                workspace_path = memory.get("workspace", {}).get("path")
                if workspace_path and os.path.exists(workspace_path):
                    return os.path.join(workspace_path, "vectors.db")
        except Exception:
            pass

    # 2. 降级到 home 目录
    home = os.path.expanduser("~")
    return os.path.join(home, ".niu", "vectors.db")
```

---

## 测试结果

运行 `./niu_test` 成功：
- Python 路径: `/Users/lilei/.niu-venv/bin/python3` ✅
- Embedding 模型: 从本地加载 ✅
- MCP 工具: 64 个加载成功 ✅
- Electron UI: 启动成功 ✅
