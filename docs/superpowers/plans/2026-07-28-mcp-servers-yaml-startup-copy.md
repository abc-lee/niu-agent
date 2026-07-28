# mcp-servers.yaml 启动复制修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 mcp-servers.yaml 纳入 `init_niu_dir` 的启动复制体系（与 skills/ 同范式），保证 MCP 配置在启动时即就绪，不再依赖 Python 侧惰性触发。Python 侧 `_get_mcp_servers_path()` 保留作为兜底。

**Architecture:**
- 现状：`init_niu_dir`（launcher/src/main.rs:910-1061）只复制 memory.json / preferences.json / skills/*.md，**不碰 config/ 目录**。Python 侧 `_get_mcp_servers_path()` 在首次 import `niu_api.config` 时惰性复制 mcp-servers.yaml 到 `~/.niu/config/`。
- 问题：mcp-servers.yaml 的就绪没有"启动即保证"。bundle 路径解析失败时 Python 侧静默不复制 → MCP 全部加载失败且无提示。
- 目标：`init_niu_dir` 增加对 `config/mcp-servers.yaml` 的复制（与 skills/ 同范式：文件级 `dst.exists()` 跳过、容错、计数日志）。Python 侧保留作为兜底 + 加注释说明主路径是 Rust 启动器。

**Tech Stack:** Rust (launcher/src/main.rs)、Python (`niu_api/config.py`)、`./launcher/build.sh` 编译验证、gitnexus 影响分析。

---

## File Structure

| 文件 | 责任 | 改动 |
|------|------|------|
| `launcher/src/main.rs` | Rust 启动器（init_niu_dir） | **修改**：在 template_files 循环结束后、skills/ 复制块**之前**插入 mcp-servers.yaml 文件级复制逻辑 |
| `niu_api/config.py` | Python 配置加载 | **修改**：`_get_mcp_servers_path()` 加注释说明主路径是 Rust 启动器 |
| `launcher/build.sh` | 编译脚本 | 不改，仅用于编译验证 |

---

## Task 1: 修改 launcher/src/main.rs 增加 mcp-servers.yaml 复制

**Files:**
- Modify: `launcher/src/main.rs:954-957`（在 template_files 循环结束后、skills/ 复制块之前插入 mcp-servers.yaml 复制逻辑）

- [ ] **Step 1: 备份当前代码**

```bash
cd /Users/lilei/tools/ai-bot
git status --short
# 如果工作区干净，跳过空 commit（遵守 No Empty Backup Commits 铁律）
# 如果有未提交改动：
git add -A && git commit -m "backup: before mcp-servers.yaml startup copy fix"
```

- [ ] **Step 2: Read 确认 template_files 循环结束位置和 skills 块开始位置**

```bash
sed -n '950,962p' /Users/lilei/tools/ai-bot/launcher/src/main.rs
```

预期看到：

```rust
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }

    // Copy skills/ directory (individual .md files, don't overwrite existing)
    // Triggered when: dir missing / dir exists but empty / specific .md missing
    // Protects user modifications by skipping existing files
    let src_skills_dir = template_dir.join("skills");
```

- L954 `info!("Copied template file: ...")` 是 template_files 循环最后一行
- L955 `}` 是循环结束
- L957 `// Copy skills/ directory ...` 注释开始 skills 块

**为什么必须插在 skills 块之前（而不是之后）**：skills/ 块内部有三处提前 `return`（src_skills_dir 不存在 / create_dir_all 失败 / read_dir 失败），任一触发都会跳过函数后续所有代码。若把 mcp-servers.yaml 复制块放在 skills 块之后，就会被这些 return 跳过 — 这正是本 plan 要修复的"启动不保证"失败模式。所以必须插在 skills 块**之前**。

- [ ] **Step 3: Edit 在 template_files 循环结束后、skills 块之前插入 mcp-servers.yaml 复制**

old_string（template_files 循环收尾 + skills 注释开头，在真实代码中唯一）：

```rust
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }

    // Copy skills/ directory (individual .md files, don't overwrite existing)
```

new_string（在 skills 注释之前插入 mcp-servers.yaml 复制块）：

```rust
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }

    // Copy config/mcp-servers.yaml (file-level, don't overwrite existing)
    // 与 skills/ 同范式：dst.exists() 跳过，保护用户修改
    // 源是 project_root/config/mcp-servers.yaml，目标是 ~/.niu/config/mcp-servers.yaml
    // 必须放在 skills 块之前：skills 块内部有多处提前 return，
    // 放它之后会被跳过，违背"启动即保证"目标
    let src_mcp_yaml = PathBuf::from(project_root).join("config").join("mcp-servers.yaml");
    let dst_config_dir = niu_dir.join("config");
    let dst_mcp_yaml = dst_config_dir.join("mcp-servers.yaml");

    if dst_mcp_yaml.exists() {
        info!(
            "mcp-servers.yaml already exists, skipping: {}",
            dst_mcp_yaml.display()
        );
    } else if !src_mcp_yaml.exists() {
        warn!(
            "Template mcp-servers.yaml not found, skipping: {}",
            src_mcp_yaml.display()
        );
    } else if let Err(e) = fs::create_dir_all(&dst_config_dir) {
        error!(
            "Failed to create config directory: {}, error={}",
            dst_config_dir.display(),
            e
        );
    } else {
        match fs::read(&src_mcp_yaml) {
            Ok(data) => {
                if let Err(e) = fs::write(&dst_mcp_yaml, &data) {
                    error!(
                        "Failed to copy mcp-servers.yaml: src={}, dst={}, error={}",
                        src_mcp_yaml.display(),
                        dst_mcp_yaml.display(),
                        e
                    );
                } else {
                    info!(
                        "Copied mcp-servers.yaml: {} -> {}",
                        src_mcp_yaml.display(),
                        dst_mcp_yaml.display()
                    );
                }
            }
            Err(e) => {
                warn!(
                    "Failed to read mcp-servers.yaml: {}, error={}",
                    src_mcp_yaml.display(),
                    e
                );
            }
        }
    }

    // Copy skills/ directory (individual .md files, don't overwrite existing)
```

**注意**：
- 使用 `PathBuf::from(project_root).join("config").join("mcp-servers.yaml")`：config/ 与 memory/ 是 project_root 下的**平级目录**，从 project_root 直接 join（而非 `template_dir.join("../config")`）
- 容错风格与 skills/ 一致：单文件失败 `warn!`/`error!` 不阻断启动
- `dst.exists()` 跳过保护用户修改；skip 日志用 `info!`（与 skills 块的 info 级 skipped 计数风格一致），保证默认 INFO 级别下用户可见
- `fs::read` 失败用 `match` 分支，**禁止用 `return`**：本块在 skills 块之前，`return` 会跳过整个 skills 复制

- [ ] **Step 4: 语法检查（cargo check）**

```bash
cd /Users/lilei/tools/ai-bot/launcher && cargo check 2>&1 | tail -20
```

预期：无 error，可能有一些 warning（与本次改动无关的历史 warning 可忽略）。

如果有 error，分析并修复。

- [ ] **Step 5: 用 build.sh 编译并复制到 ../niu**

**铁律**：禁止直接 `cargo build`，必须用 `./launcher/build.sh`（会自动 `cp target/release/niu-launcher ../niu`）。

```bash
cd /Users/lilei/tools/ai-bot && ./launcher/build.sh 2>&1 | tail -30
```

预期：
- 编译成功
- 输出 "Built and copied to ../niu"
- macOS 用户：输出包含 "copying config/..." 等 bundle 资源复制日志

如果编译失败，根据错误信息修复。

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add launcher/src/main.rs
git commit -m "$(cat <<'EOF'
feat(launcher): init_niu_dir 增加 mcp-servers.yaml 启动复制

把 mcp-servers.yaml 纳入启动复制体系（与 skills/ 同范式）：
- 源：project_root/config/mcp-servers.yaml
- 目标：~/.niu/config/mcp-servers.yaml
- dst.exists() 跳过（保护用户修改）
- 容错：单文件失败 warn!/error! 不阻断启动

解决 mcp-servers.yaml 就绪依赖 Python 侧惰性触发的问题。
Python 侧 niu_api/config.py:_get_mcp_servers_path() 保留作为兜底。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: niu_api/config.py 加注释说明主路径

**Files:**
- Modify: `niu_api/config.py:67-77`（`_get_mcp_servers_path()` 函数 docstring）

- [ ] **Step 1: Read 确认当前 docstring**

```bash
sed -n '67,78p' /Users/lilei/tools/ai-bot/niu_api/config.py
```

预期：

```python
def _get_mcp_servers_path() -> str:
    """返回 ~/.niu/config/mcp-servers.yaml。首次启动从 bundle 内复制。"""
    home = os.path.expanduser("~")
    niu_config_dir = Path(home) / ".niu" / "config"
    mcp_yaml = niu_config_dir / "mcp-servers.yaml"
    if not mcp_yaml.exists():
        bundle_yaml = _get_bundle_config_dir() / "mcp-servers.yaml"
        if bundle_yaml.exists():
            niu_config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle_yaml, mcp_yaml)
    return str(mcp_yaml)
```

- [ ] **Step 2: Edit docstring 说明主路径是 Rust 启动器**

old_string：

```python
def _get_mcp_servers_path() -> str:
    """返回 ~/.niu/config/mcp-servers.yaml。首次启动从 bundle 内复制。"""
```

new_string：

```python
def _get_mcp_servers_path() -> str:
    """返回 ~/.niu/config/mcp-servers.yaml。首次启动从 bundle 内复制。

    主路径：Rust 启动器 init_niu_dir 在拉起 Python 之前已复制（启动即保证）。
    本函数的惰性复制是兜底：纯 Python 启动场景（不经 launcher）或 launcher 复制失败时生效。
    """
```

- [ ] **Step 3: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/niu_api/config.py').read()); print('syntax ok')"
```

预期：`syntax ok`

- [ ] **Step 4: Commit**

```bash
git add niu_api/config.py
git commit -m "$(cat <<'EOF'
docs(config): _get_mcp_servers_path docstring 说明主路径是 Rust 启动器

主路径：launcher init_niu_dir 启动即复制（启动即保证）。
本函数惰性复制是兜底：纯 Python 启动场景或 launcher 失败时生效。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: gitnexus 影响分析 + 真实环境验证

**Files:**
- 不改代码，仅验证

- [ ] **Step 1: gitnexus 影响分析（可选，若索引已过期可跳过）**

```bash
cd /Users/lilei/tools/ai-bot
npx gitnexus detect_changes 2>&1 | tail -30
```

如果输出提示索引过期：
```bash
npx gitnexus analyze 2>&1 | tail -10
```

然后再次 detect_changes 确认只影响 `init_niu_dir` 函数和 `_get_mcp_servers_path` 函数。

如果 gitnexus 不可用或环境受限，跳过此步，直接进入 Step 2 真实环境验证。

- [ ] **Step 2: 真实环境验证（用户配合）— dev 模式**

提示用户：

> 改动完成，请你做一次 dev 模式真实环境验证：
>
> 1. 备份当前 `~/.niu/config/mcp-servers.yaml` 到安全位置
> 2. 删除 `~/.niu/config/mcp-servers.yaml`（模拟首次启动场景）
> 3. 启动程序 `./niu`
> 4. 观察启动日志：应该看到 `Copied mcp-servers.yaml: ... -> /Users/lilei/.niu/config/mcp-servers.yaml` 这条 **info** 日志（默认 INFO 级别可见）
> 5. 验证 `~/.niu/config/mcp-servers.yaml` 已被创建
> 6. 验证 MCP 工具加载成功（日志中应看到 "All N servers loaded"，N 是动态数字取决于加载成功的服务器数，一般是 9-10）
> 7. **再次启动跳过验证（不依赖日志级别的判据）**：
>    - 记录当前文件 mtime：`stat -f "%m %Sm" ~/.niu/config/mcp-servers.yaml`
>    - 再次启动 `./niu`
>    - 再次查看 mtime：`stat -f "%m %Sm" ~/.niu/config/mcp-servers.yaml`
>    - **判据 A（mtime）**：两次 mtime 完全一致 → 文件未被覆盖，跳过逻辑生效
>    - **判据 B（info 日志）**：启动日志中可见 `mcp-servers.yaml already exists, skipping: ...` 这条 **info** 级日志（与 skills 块的 info 级 skipped 计数风格一致）
>    - 两个判据任一通过即可；mtime 判据更可靠（不依赖日志级别 / 过滤配置）
> 8. 验证完成后可以选择恢复 backup（如果你想保留自己改过的版本）
>
> 注意事项：
> - 如果用户的 `~/.niu/config/mcp-servers.yaml` 已存在且自定义过，启动器不会覆盖（这是正确行为）
> - Python 侧兜底逻辑保留，即使 launcher 失败也能在 Python import 时复制

- [ ] **Step 3: 真实环境验证（用户配合）— bundle 模式**

dev 模式 Python 兜底几乎不会失败，dev 验证通过不能证明 bundle 场景修复。本 bug 的主战场是 bundle 模式（niu.app），必须单独验证。

提示用户：

> 请你做一次 bundle 模式真实环境验证：
>
> 1. 重新打包：`cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`
>    - 预期输出 "Built and copied to ../niu"
>    - 预期生成 `niu.app`（macOS）
> 2. 备份当前 `~/.niu/config/mcp-servers.yaml` 到安全位置
> 3. 删除 `~/.niu/config/mcp-servers.yaml`（模拟首次启动场景）
> 4. 启动 bundle：`open niu.app`（或 Finder 双击 niu.app）
> 5. 验证 `~/.niu/config/mcp-servers.yaml` 已被创建
> 6. 验证 MCP 工具加载成功（前端可调用 MCP 工具，或日志中看到 "All N servers loaded"，N 是动态数字）
> 7. 再次 `open niu.app`，用 Step 2 的 mtime 判据验证文件未被覆盖
> 8. 验证完成后可以选择恢复 backup
>
> 注意事项：
> - bundle 模式下 Python 兜底 `_get_bundle_config_dir()` 历史上曾解析失败，本步骤是本次修复的核心场景
> - 如果 bundle 模式失败但 dev 模式成功，说明 launcher 在 bundle 路径下的 `detect_resources_root()` 解析有问题，需要排查 launcher 而非 Python

- [ ] **Step 4: 无 commit**

本 Task 只是验证，不改代码。

---

## Self-Review

**1. Spec coverage**:
- ✅ init_niu_dir 增加 mcp-servers.yaml 复制（与 skills/ 同范式）：Task 1
- ✅ 插入位置在 template_files 循环结束后、skills/ 块**之前**（避免被 skills 块内三处提前 return 跳过）：Task 1 Step 3
- ✅ 文件级 `dst.exists()` 跳过保护用户修改：Task 1 Step 3
- ✅ 容错 warn!/error! 不阻断启动；fs::read 失败用 match 分支而非 return（避免误杀 skills 复制）：Task 1 Step 3
- ✅ skip 日志用 `info!`（默认 INFO 级别可见，与 skills 块 info 级 skipped 计数风格一致）：Task 1 Step 3
- ✅ 计数日志（ Copied mcp-servers.yaml 单条 info）：Task 1 Step 3
- ✅ Python 侧保留作为兜底 + 注释说明：Task 2
- ✅ 用 ./launcher/build.sh 编译：Task 1 Step 5
- ✅ gitnexus 影响分析：Task 3 Step 1
- ✅ 真实环境验证（dev + bundle 双模式）：Task 3 Step 2 + Step 3
- ✅ 验证步骤 7 不依赖日志级别：mtime 判据 + info 日志双判据：Task 3 Step 2

**2. Placeholder scan**:
- ✅ 无 TBD/TODO
- ✅ 所有 Step 含具体命令和预期输出
- ✅ Rust 代码片段完整可直接 Edit

**3. Type consistency**:
- ✅ Rust 路径处理 `PathBuf::from(project_root).join("config").join("mcp-servers.yaml")` 与现有 `template_dir = PathBuf::from(project_root).join("memory")` 模式一致
- ✅ Python docstring 改动只是字符串，无类型变化

**4. 风险点**:
- **路径假设**：plan 假设 `detect_project_root()` 返回的路径在 bundle 模式 = `Contents/Resources`，dev 模式 = 项目根。`build.sh:42-44` 确认 bundle 打包时 `config/` 复制到 `Contents/Resources/config/`，所以 `project_root.join("config").join("mcp-servers.yaml")` 两种模式都对。
- **cargo run 场景**：直接 `cargo run`（不经 build.sh）时，`detect_resources_root()` 会返回 `launcher/target/debug/`，该目录下没有 `config/mcp-servers.yaml`，launcher 会命中 `!src_mcp_yaml.exists()` 分支打印 warn 跳过。此时 Python 兜底 `_get_bundle_config_dir()` 在 dev 项目根下解析成功，仍能完成复制。符合"launcher 主 + Python 兜底"分层设计，不视为 bug。
- **PathBuf import**：`launcher/src/main.rs` 已 `use std::path::PathBuf`（L11），无需新增 import。
- **fs module**：已 `use std::fs`（L9），无需新增 import。
- **log macros**：`info!`/`warn!`/`error!` 已通过 `use tracing::{debug, error, info, warn};`（L24）import。本次改动**不再使用** `debug!`（skip 日志升级为 `info!`）。
- **不用 return**：fs::read 失败用 match 分支而非 `return`。本块前移到 skills 块之前后，任何 `return` 都会跳过整个 skills 复制。
- **tasks order**：Task 1 改 Rust → Task 2 改 Python 注释 → Task 3 验证（dev + bundle），顺序合理。
- **不覆盖用户修改**：`dst.exists()` 跳过保证用户自定义不会被覆盖。

**5. 审查历史**:

本计划基于用户提供的另一 Agent 的审查报告，证据链已逐条验证：
1. ✅ `agent/mcp_loader.py:42-47` 确实从 `~/.niu/config/mcp-servers.yaml` 读
2. ✅ `niu_api/config.py:67-76` `_get_mcp_servers_path()` 是惰性复制
3. ✅ `init_niu_dir` 在 `launcher/src/main.rs:1491` 执行（拉起 Python 之前），只复制 memory/preferences/skills，没碰 config/
4. ✅ 后果合理：bundle 路径解析失败时静默不复制会导致 MCP 加载失败

**6. 不要改的部分**（确认范围）:
- `init_niu_dir` 对 memory.json / preferences.json / skills/ 的现有复制逻辑保持原样
- `agents/` 和 `disk/` 双目录设计（bundle 系统资源 + ~/.niu 用户覆盖）不改
- `llm-presets.json`（前端只从 bundle 读）和 `agent-template.md`（不被加载）不需要复制

---

## 执行交付条件

1. 所有 3 个 Task 完成，Task 1 和 Task 2 各自单独 commit
2. `cargo check` 通过 + `./launcher/build.sh` 编译成功
3. `python3 -c "import ast; ast.parse(...)"` 通过
4. gitnexus detect_changes 显示只影响 `init_niu_dir` 和 `_get_mcp_servers_path`
5. 用户在真实环境验证（**dev 模式 + bundle 模式双验证**）：删除 mcp-servers.yaml 后重启，看到 Copied info 日志 + 文件被创建 + MCP 加载成功；二次启动用 mtime 判据确认未被覆盖
