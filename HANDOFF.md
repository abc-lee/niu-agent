# Handoff: Windows → Mac 迁移

## 背景

项目从 Windows 主机迁移到 Mac。Windows 版（`REDACTED_USER_PATH/tools/ai-bot`）是最新代码，Mac 版（`REDACTED_USER_PATH/tools/niu-agent`）代码已过时。

**目标**：在 `REDACTED_USER_PATH/tools/ai-bot` 目录下完成 Mac 适配，使其成为新的工作目录。

## 已确认的事实

1. **两个 main.go 完全一致**——Windows 版已包含 Mac/Linux 的 `runtime.GOOS` 分支，无需修改
2. **Mac 版 go.mod 被精简过**（删除了 `tool` 指令和 `golang.org/x/term` 依赖），但能正常编译
3. **Mac 版代码架构已过时**——使用旧的 vector-store/kg-server，Windows 版已改用 lightrag-server 图检索架构
4. **Mac 版独有的文件都是废弃的**（vector-store、kg-server、tool_lifecycle.py、vector_search.py 等），不需要保留

## 需要执行的操作

### 步骤 1：修复 go.mod

Mac 版 go.mod 被精简过，缺少 `tool` 指令。需要从 Windows 版恢复完整 go.mod，然后运行 `go mod tidy` 适配 Mac。

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# Windows 版的 go.mod 是完整的，先确认能否直接编译
go build -o /dev/null 2>&1
# 如果报错，运行 go mod tidy
```

### 步骤 2：修改 config/user-config.json 路径

Windows 版路径需要全部改为 Mac 路径。关键映射：

| Windows 路径 | Mac 路径 |
|-------------|---------|
| `D:\lilei\Documents\Niu` | `REDACTED_USER_PATH/Documents/Niu` |
| `D:\lilei\tools\ai-bot\models` | `REDACTED_USER_PATH/tools/ai-bot/models` |
| `D:\lilei\tools\ai-bot\python` | `REDACTED_USER_PATH/tools/ai-bot/python` |
| Windows 用户目录下的 `.niu` | `REDACTED_USER_PATH/.niu` |

需要修改的字段：
- `storage.documentRoot` → `REDACTED_USER_PATH/Documents/Niu`
- `storage.databasePath` → `REDACTED_USER_PATH/Documents/Niu`
- 其他包含 Windows 路径的字段

### 步骤 3：修改 config/mcp-servers.yaml 路径

Windows 版的 `workdir` 使用反斜杠路径，需要改为 Mac 路径：
- `workdir: ..\mcp-servers\xxx\src` → `workdir: ../mcp-servers/xxx/src`

### 步骤 4：修改 config/agents/ 下的路径

检查 `niu.md`、`context-manager.md`、`dream-evolver.md` 等文件中是否有 Windows 路径。

### 步骤 5：修改 ~/.niu/ 用户数据路径

如果从 Windows 拷贝了 `~/.niu/` 目录，需要修改：
- `~/.niu/memory.json` 中的 `workspace.path` → Mac 路径
- `~/.niu/preferences.json` 中的路径配置

### 步骤 6：重建 Python 虚拟环境

Windows 的 Python 虚拟环境（`.pyd`/`.dll`）在 Mac 上无法使用，必须重建：

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# main.go 启动时会自动创建 python/ 虚拟环境
# 但需要先删除 Windows 的虚拟环境
rm -rf python/
# 或者手动创建：
python3 -m venv python
source python/bin/activate
pip install -e agent/
# 安装各 MCP 服务器依赖
for dir in mcp-servers/*/; do
    pip install -e "$dir" 2>/dev/null || pip install -e "${dir}src/" 2>/dev/null
done
```

### 步骤 7：重建 npm 依赖

```bash
cd REDACTED_USER_PATH/tools/ai-bot/ui/assistant
rm -rf node_modules
npm install
```

### 步骤 8：模型文件

Windows 版的模型文件（`.onnx`/`.safetensors`）在 Mac 上需要重新下载或拷贝：
- `models/all-MiniLM-L6-v2/model.safetensors` (~90 MB)
- `models/models/buffalo_l/*.onnx` (~326 MB) — InsightFace 人脸识别
- `models/bge-base-zh-v1.5/` — 中文向量模型（Windows 版新增）
- `models/paraphrase-multilingual-MiniLM-L12-v2/` — 多语言向量模型

模型文件优先从本地加载，本地没有才下载。可以：
1. 从 Windows 拷贝模型文件（需要网络传输）
2. 首次运行时自动下载（需要网络）

### 步骤 9：重建 GitNexus 索引

```bash
cd REDACTED_USER_PATH/tools/ai-bot
npx gitnexus analyze
```

### 步骤 10：编译并测试

```bash
cd REDACTED_USER_PATH/tools/ai-bot
go build -o niu && ./niu
```

## 注意事项

1. **不要拷贝 Mac 版（niu-agent）的代码到 Windows 版**——Mac 版的代码架构已过时
2. **不要拷贝 Windows 的 Python 虚拟环境**——平台不兼容
3. **不要拷贝 Windows 的 node_modules**——平台不兼容
4. **不要拷贝 `niu.exe`**——Mac 上需要重新 `go build`
5. **lightrag-server 是新架构核心**——Mac 版旧的 vector-store/kg-server 已废弃
6. **Mac 版 go.mod 被精简过**——如果 Windows 版 go.mod 在 Mac 上编译失败，参考 Mac 版的精简方式（删除 `tool` 指令和 `golang.org/x/term`）
7. **MCP 配置中 gitnexus 的启动方式**——已改为 `/usr/local/bin/gitnexus mcp`（不用 npx），需要确认 `.claude.json` 中项目路径更新

## 完成后的目录切换

迁移完成后，工作目录从 `REDACTED_USER_PATH/tools/niu-agent` 切换到 `REDACTED_USER_PATH/tools/ai-bot`。需要更新：
- Claude Code 的项目配置（`.claude.json` 中的项目路径）
- GitNexus 索引路径
- 任何硬编码了 `REDACTED_USER_PATH/tools/niu-agent` 的配置
