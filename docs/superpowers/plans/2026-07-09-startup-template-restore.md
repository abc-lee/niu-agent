# 启动时模板恢复 skills 目录 Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补回 niu-agent 启动时自动从项目 `memory/` 目录复制模板到用户 `~/.niu/` 目录的功能，重点是恢复 `memory/skills/` 目录的复制（当前 `init_niu_dir` 只复制 `memory.json` / `preferences.json` 两个文件，不复制 `skills/` 目录）。用户清空 `~/.niu/skills/` 后重启程序，skills 不再自动恢复，导致 skills 功能失效。

**Architecture:** 在 Rust 启动器 `launcher/src/main.rs` 的 `init_niu_dir` 函数里追加 skills 目录复制逻辑（不动现有 `memory.json` / `preferences.json` 的复制逻辑）。复制粒度到单个 `.md` 文件级，触发条件覆盖三种场景：(1) `~/.niu/skills/` 目录不存在；(2) 目录存在但为空；(3) 目录里某个 `.md` 文件不存在。已存在的 `.md` 文件不覆盖（保护用户修改）。复制逻辑放 Rust 而不是 Python，理由：(1) Rust 启动器先跑，能更早保证 `~/.niu/skills/` 就绪，Python 启动时 SkillSync 扫描 `~/.niu/skills/` 不会扑空；(2) 与现有 `memory.json` / `preferences.json` 复制逻辑同函数同语言，一致性好；(3) Rust 的 `fs::read_dir` / `fs::create_dir_all` / `fs::write` API 简洁够用，不需要 Python 的便利性。

**Tech Stack:** Rust (launcher)，cargo build via `launcher/build.sh`，Bash 手动验证

---

## Context

### 当前 bug

用户清空 `~/.niu/skills/` 后重启 `./niu`，启动器 `init_niu_dir` 只复制 `memory.json` 和 `preferences.json`，不复制 `skills/` 目录，导致 `~/.niu/skills/` 保持空，SkillSync 扫描时 `if not self.skills_dir.exists():` 分支不会触发（目录存在但空），后续 `self.skills_dir.glob("*.md")` 返回空迭代器，skills 功能完全失效。

### 根因

commit `6a271e8f fix: 删掉启动时备份逻辑，关键用户数据作为项目文件纳入仓库` 删掉了 `niu_api/__main__.py` 的 `_backup_critical_files()`（反向备份，`~/.niu/` → `~/.niu/backup/`），但用户记得当时配套还有"启动时从项目 `memory/` 复制模板到 `~/.niu/`"的功能，这个功能在某个时间点（可能是 worktree 没合并）丢失了。当前 `init_niu_dir` 只复制两个文件，不复制 `skills/` 目录。

### 已查清事实

**项目 `memory/` 目录**（模板源）：
- `memory/memory.json`
- `memory/preferences.json`
- `memory/skills/`（含 6 个 .md：`brain-region-management.md` / `browser-automation.md` / `note-management.md` / `office-docs.md` / `photo-face-display.md` / `report-skill.md`）
- 另含 `.DS_Store`（macOS Finder 文件，复制时需跳过）

**用户 `~/.niu/` 目录**（运行时读取位置）：
- `~/.niu/memory.json`
- `~/.niu/preferences.json`
- `~/.niu/skills/`（用户清空后为空，但目录本身存在）

**当前启动逻辑**（`launcher/src/main.rs:655-701` 的 `init_niu_dir` 函数）：
- 只复制 `["memory.json", "preferences.json"]` 两个文件（仅当 `~/.niu/<file>` 不存在时）
- **不复制 skills 目录**
- 调用点：`launcher/src/main.rs:1077` 的 `init_niu_dir(&project_root)`
- `project_root` 解析：优先 exe 目录，回退 cwd（L1044-1064 处理）

**SkillSync 读取点**（`agent/injector/sync.py:175-177`）：
- `_default_skills_dir()` 返回 `Path.home() / ".niu" / "skills"`
- `_scan_loop` L272-273：`if not self.skills_dir.exists(): logger.warning(...)` — 目录不存在只 warn 不退出
- L282：`for skill_file in self.skills_dir.glob("*.md"):` — 目录空时返回空迭代器，skills 列表为空，但不会报错
- **关键**：Python API 启动时 SkillSync 拿到的目录如果是空的，会静默失败（skills 功能失效但不崩），用户看不到错误信息

### 三个关键代码点

| 文件 | 行号 | 内容 | 改动 |
|------|------|------|------|
| `launcher/src/main.rs` | L655-701 | `init_niu_dir` 函数，复制 `memory.json` / `preferences.json` | **在 L700 后追加 skills 目录复制逻辑** |
| `launcher/src/main.rs` | L1077 | `init_niu_dir(&project_root)` 调用点 | **不动**（函数内部扩展，调用点不变） |
| `launcher/build.sh` | 全文 | `cargo build --release && cp target/release/niu-launcher ../niu` | **不动**（铁律 #8 要求用这个编译） |

### 复制逻辑设计选择

**选用 Rust 启动器 `init_niu_dir` 内追加 skills 复制**，理由：

1. **时序优势**：Rust 启动器先跑，能保证 Python API 启动时 `~/.niu/skills/` 已经被填充，SkillSync L272-282 扫描时不会扑空。如果放 Python `niu_api/__main__.py` lifespan，SkillSync 在 lifespan 的第 8 步启动（L236-242），但 skills 复制得在 SkillSync 之前——可以放在 lifespan 第 1 步前，但那样跟 Rust 的 `init_niu_dir` 重复了，不如一处维护
2. **一致性**：与现有 `memory.json` / `preferences.json` 复制逻辑同函数同语言，未来要改复制策略（如改触发条件）只改一处
3. **API 够用**：Rust 的 `fs::read_dir` / `fs::create_dir_all` / `fs::write` 完全够用，不需要 Python 的 `shutil` 或 `pathlib` 的便利性
4. **避免重复执行**：放 Python 的话，Rust 启动器已经复制过 `memory.json` / `preferences.json`，Python 再复制 skills 会让逻辑分裂在两个语言两个文件里

### 触发条件设计

**三种场景全覆盖**：

| 场景 | 当前 `init_niu_dir` 行为 | 修复后行为 |
|------|------------------------|-----------|
| `~/.niu/skills/` 目录不存在 | 不复制（根本没处理 skills） | `fs::create_dir_all` 创建目录，复制所有 `.md` |
| `~/.niu/skills/` 存在但为空 | 不复制（根本没处理 skills） | 复制所有 `.md`（dst 不存在才复制） |
| `~/.niu/skills/` 存在且某 `.md` 已存在 | 不复制（根本没处理 skills） | 跳过已存在的 `.md`（保护用户修改），只复制缺失的 |

**复制粒度**：单个 `.md` 文件级。`fs::read_dir(template_skills_dir)` 遍历每个 entry，过滤 `*.md` 后缀，对每个 `.md` 检查 `~/.niu/skills/<name>.md` 是否存在，不存在才复制。

**跳过非 `.md` 文件**：`memory/skills/` 里有 `.DS_Store`（macOS Finder 元数据），复制时按后缀过滤，只复制 `.md`，避免把 `.DS_Store` 拷过去污染用户目录。

### 关键约束（用户铁律）

- **铁律 #3 修改前必须先做临时提交备份** — Task 0 做备份
- **铁律 #7 git 操作后必须修复文件权限** — Task 4 编译后跑权限修复
- **铁律 #8 Rust 启动器编译必须用 `launcher/build.sh`** — Task 3 编译用 `./launcher/build.sh`，禁止直接 `cargo build`
- **铁律 #5 测试必须用真实数据 + 真实 LLM** — Rust 启动器没有单元测试框架约束，Task 2 用真实场景手动验证（删 `~/.niu/skills/` 启动后确认被填充）
- **禁止 `git reset --hard` / force push**
- **禁止 pkill 强杀进程** — Task 4 用 `kill -TERM` 优雅退出

### 关键代码位置（HEAD = 27b287f4）

**当前 `init_niu_dir`** `launcher/src/main.rs:655-701`：
```rust
fn init_niu_dir(project_root: &str) {
    let home_dir = match dirs::home_dir() { ... };
    let niu_dir = home_dir.join(".niu");
    if let Err(e) = fs::create_dir_all(&niu_dir) { ... }

    // Template files to copy if they don't exist in ~/.niu/
    let template_files = ["memory.json", "preferences.json"];
    let template_dir = PathBuf::from(project_root).join("memory");

    for filename in &template_files {
        let dst_path = niu_dir.join(filename);
        if dst_path.exists() { continue; }
        let src_path = template_dir.join(filename);
        let src_data = match fs::read(&src_path) { ... };
        if let Err(e) = fs::write(&dst_path, &src_data) { ... }
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }
    // ← L700 后追加 skills 目录复制逻辑
}
```

**调用点** `launcher/src/main.rs:1077`：
```rust
init_niu_dir(&project_root);  // 不动，函数内部扩展
```

**SkillSync 扫描点** `agent/injector/sync.py:272-282`（不动，验证修复后扫描能拿到文件）：
```python
if not self.skills_dir.exists():
    logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
    return  # 修复后这里不会触发，因为 init_niu_dir 已创建目录
for skill_file in self.skills_dir.glob("*.md"):
    # 修复后这里能拿到 6 个 .md 文件
```

---

## File Structure

```
ai-bot/                              # 项目根
├── launcher/
│   ├── src/
│   │   └── main.rs                  # 改 init_niu_dir 函数 L655-701，追加 skills 复制
│   ├── build.sh                     # 不改，Task 3 用它编译
│   └── target/                      # 编译产物
├── memory/
│   ├── memory.json                  # 不改（已有复制逻辑）
│   ├── preferences.json             # 不改（已有复制逻辑）
│   └── skills/                      # 模板源，6 个 .md，不改
│       ├── brain-region-management.md
│       ├── browser-automation.md
│       ├── note-management.md
│       ├── office-docs.md
│       ├── photo-face-display.md
│       └── report-skill.md
├── agent/injector/sync.py           # 不改，验证修复后 SkillSync 扫描能拿到文件
├── niu_api/__main__.py              # 不改，Python 启动流程不动
└── docs/superpowers/plans/
    └── 2026-07-09-startup-template-restore.md  # 本计划文件
```

---

## Tasks

### Task 0: 修改前临时备份提交

- [ ] **Step 0.1**：检查工作区干净（除本次新计划文件外）
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
```
**预期**：只有 `docs/superpowers/plans/2026-07-09-startup-template-restore.md` 是新文件（或工作区干净）。

- [ ] **Step 0.2**：临时备份提交（标注问题名 + 节点类型 + 基线 hash）
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A
git commit -m "backup: 启动模板恢复 skills 目录改造前临时备份 (baseline 27b287f4)

问题：init_niu_dir 只复制 memory.json/preferences.json，不复制 skills/ 目录。
用户清空 ~/.niu/skills/ 后重启程序，skills 不自动恢复，功能失效。

准备改 launcher/src/main.rs 的 init_niu_dir 函数：
- 在 L700 后追加 skills 目录复制逻辑
- 复制粒度到单个 .md 文件级，已存在不覆盖
- 触发条件覆盖：目录不存在 / 存在但为空 / 文件缺失

铁律 #8：改 Rust 代码后用 ./launcher/build.sh 编译，禁止 cargo build。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1: 读当前 init_niu_dir 实现 + 设计新增代码块

**目标**：确认 `init_niu_dir` 当前实现细节，设计要追加的代码块（不写文件，只读+设计）。

- [ ] **Step 1.1**：读 `launcher/src/main.rs` L650-705 当前实现
```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 用 Read 工具读 launcher/src/main.rs offset=649 limit=60
```
**确认点**：
- L655 `fn init_niu_dir(project_root: &str)` 函数签名
- L673 `let template_files = ["memory.json", "preferences.json"];` 当前两个文件
- L674 `let template_dir = PathBuf::from(project_root).join("memory");` 模板源目录
- L676-700 for 循环复制两个文件
- L700 `}` 函数结束

- [ ] **Step 1.2**：设计追加的 skills 复制代码块

**追加位置**：L699-700 之间（for 循环结束后、函数 `}` 之前）。

**新增代码块设计**（伪代码 → 实际代码见 Task 2）：
```
1. 拼接源目录：template_dir.join("skills")
2. 拼接目标目录：niu_dir.join("skills")
3. 如果源目录不存在 → warn 日志，return（不报错，模板缺失不阻塞启动）
4. fs::create_dir_all(目标目录)（不存在则创建，已存在幂等）
5. fs::read_dir(源目录) 遍历每个 entry
6. 对每个 entry：
   a. 取文件名，过滤只处理 .md 后缀（跳过 .DS_Store / 隐藏文件）
   b. 拼接 dst_path = 目标目录.join(文件名)
   c. 如果 dst_path.exists() → 跳过（保护用户修改）
   d. 否则 fs::read(src_path) → fs::write(dst_path, data)
   e. info! 日志记录复制
7. 统计复制了几个文件，info! 日志汇总
```

---

### Task 2: 改 launcher/src/main.rs 追加 skills 复制逻辑

**目标**：在 `init_niu_dir` 函数 L700 前追加 skills 目录复制代码块。

- [ ] **Step 2.1**：用 Read 工具读 `launcher/src/main.rs` L655-705 确认当前精确文本

**读完后确认当前 L676-700 的 for 循环结束位置精确文本**（用于 Edit 锚点）：
```rust
        if let Err(e) = fs::write(&dst_path, &src_data) {
            error!(
                "Failed to copy template file: src={}, dst={}, error={}",
                src_path.display(),
                dst_path.display(),
                e
            );
            continue;
        }
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }
}
```

- [ ] **Step 2.2**：用 Edit 工具在 for 循环 `}` 之后、函数 `}` 之前插入 skills 复制逻辑

**Edit old_string**（精确匹配 for 循环结束 + 函数结束）：
```rust
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }
}
```

**Edit new_string**（追加 skills 复制块）：
```rust
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }

    // Copy skills/ directory (individual .md files, don't overwrite existing)
    // Triggered when: dir missing / dir exists but empty / specific .md missing
    // Protects user modifications by skipping existing files
    let src_skills_dir = template_dir.join("skills");
    let dst_skills_dir = niu_dir.join("skills");

    if !src_skills_dir.exists() {
        warn!(
            "Template skills directory not found, skipping: {}",
            src_skills_dir.display()
        );
        return;
    }

    if let Err(e) = fs::create_dir_all(&dst_skills_dir) {
        error!(
            "Failed to create skills directory: {}, error={}",
            dst_skills_dir.display(),
            e
        );
        return;
    }

    let entries = match fs::read_dir(&src_skills_dir) {
        Ok(e) => e,
        Err(e) => {
            warn!(
                "Failed to read template skills directory: {}, error={}",
                src_skills_dir.display(),
                e
            );
            return;
        }
    };

    let mut copied_count = 0u32;
    let mut skipped_count = 0u32;
    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                warn!("Failed to read directory entry: {}", e);
                continue;
            }
        };

        let file_name = entry.file_name();
        let file_name_str = match file_name.to_str() {
            Some(s) => s,
            None => continue,
        };

        // Only copy .md files, skip .DS_Store / hidden files
        if !file_name_str.ends_with(".md") {
            continue;
        }

        let dst_path = dst_skills_dir.join(file_name_str);
        if dst_path.exists() {
            debug!(
                "Skill file already exists, skipping: {}",
                dst_path.display()
            );
            skipped_count += 1;
            continue;
        }

        let src_path = entry.path();
        let src_data = match fs::read(&src_path) {
            Ok(d) => d,
            Err(e) => {
                warn!(
                    "Failed to read template skill file: {}, error={}",
                    src_path.display(),
                    e
                );
                continue;
            }
        };

        if let Err(e) = fs::write(&dst_path, &src_data) {
            error!(
                "Failed to copy skill file: src={}, dst={}, error={}",
                src_path.display(),
                dst_path.display(),
                e
            );
            continue;
        }

        info!(
            "Copied skill file: {} -> {}",
            file_name_str,
            dst_path.display()
        );
        copied_count += 1;
    }

    info!(
        "Skills directory sync: copied={}, skipped(existing)={}, dst={}",
        copied_count,
        skipped_count,
        dst_skills_dir.display()
    );
}
```

**关键设计点说明**：
1. **源目录不存在只 warn 不报错**：模板缺失不阻塞启动，跟现有 `template_files` 复制时 `warn!("Template file not found, skipping: ...")` 的策略一致
2. **目标目录用 `create_dir_all`**：幂等，不存在则创建（含父目录），已存在不报错
3. **`fs::read_dir` 遍历**：返回迭代器，逐个 entry 处理
4. **`file_name.to_str()` 失败跳过**：文件名含非 UTF-8 字符时跳过（macOS 罕见但可能）
5. **`ends_with(".md")` 过滤**：跳过 `.DS_Store` / 隐藏文件 / 其他非 markdown 文件
6. **`dst_path.exists()` 跳过**：保护用户修改的核心逻辑
7. **`debug!` 级别记录跳过**：避免 info 级别刷屏（用户修改的文件每次启动都跳过，info 太吵）
8. **汇总日志 info 级别**：复制多少 / 跳过多少 / 目标路径，方便排查

- [ ] **Step 2.3**：Rust 语法检查（cargo check）
```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher
cargo check 2>&1 | tail -20
```
**预期**：无错误无警告（warning 可能因为 unused import 等，但不应有跟新代码相关的错误）。
**注意**：`cargo check` 只做语法检查不产出二进制，跟铁律 #8 不冲突——铁律 #8 禁止用 `cargo build` 替代 `./launcher/build.sh` 做编译产出，但 `cargo check` 不产出二进制，可以用。

如果 `cargo check` 报错，立即用 Edit 工具修正，不能继续。

---

### Task 3: 用 launcher/build.sh 编译

**目标**：用铁律 #8 要求的 `./launcher/build.sh` 编译产出新的 `niu` 二进制。

- [ ] **Step 3.1**：跑 build.sh
```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh 2>&1 | tail -20
```
**预期输出**：
```
   Compiling niu-launcher v...
    Finished release [optimized] target(s) in ...
Built and copied to ../niu
```
**关键检查**：
- `Built and copied to ../niu` 这行必须出现（说明 `cp target/release/niu-launcher ../niu` 执行了）
- 项目根目录的 `niu` 二进制时间戳更新（用 `ls -la REDACTED_USER_PATH/tools/ai-bot/niu` 确认）

- [ ] **Step 3.2**：确认新二进制时间戳
```bash
ls -la REDACTED_USER_PATH/tools/ai-bot/niu
```
**预期**：时间戳是刚才编译的时间。

- [ ] **Step 3.3**：git 操作后修复文件权限（铁律 #7）
```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```
**说明**：build.sh 内部已经跑了 node_modules 权限修复（见 `launcher/build.sh` 末尾），但 python/bin/ 的修复要额外跑。如果 git 状态干净（Task 0 已提交），这步是幂等的。

---

### Task 4: 真实端到端验证（真实场景 + 真实程序）

**目标**：用真实场景验证修复——清空 `~/.niu/skills/` 启动程序，确认 6 个 .md 被自动恢复。

**铁律 #5 要求**：测试必须用真实数据 + 真实 LLM，不 mock。本任务是启动器层面，没有 LLM 介入，但要用真实程序 + 真实文件系统，不 mock。

- [ ] **Step 4.1**：杀掉所有 niu 进程（铁律：禁止 pkill -f niu，必须 kill -TERM 优雅退出）
```bash
ps aux | grep -E "niu|launcher" | grep -v grep
# 用 kill -TERM <pid> 逐个优雅退出
ps aux | grep -E "niu|launcher" | grep -v grep | awk '{print $2}' | xargs -I {} kill -TERM {} 2>/dev/null
sleep 3
ps aux | grep -E "niu|launcher" | grep -v grep  # 应为空
```

- [ ] **Step 4.2**：备份当前 `~/.niu/skills/` 状态（如果有用户修改的 .md 要保护）
```bash
ls -la ~/.niu/skills/ 2>/dev/null
# 如果有用户修改的 .md，先备份到临时位置
if [ -d ~/.niu/skills ] && [ "$(ls -A ~/.niu/skills/ 2>/dev/null)" ]; then
    rm -rf ~/.niu/skills.bak
    cp -r ~/.niu/skills ~/.niu/skills.bak
    echo "Backed up existing ~/.niu/skills/ to ~/.niu/skills.bak"
fi
```

- [ ] **Step 4.3**：清空 `~/.niu/skills/` 模拟用户删除场景
```bash
# 删除整个 skills 目录（模拟用户场景）
rm -rf ~/.niu/skills
ls -la ~/.niu/skills 2>/dev/null  # 应提示 No such file or directory
```
**说明**：用户报告的场景是"删了 `~/.niu/skills/` 后重启"，所以这里 `rm -rf` 整个目录，模拟"目录不存在"场景。修复后 `init_niu_dir` 应该 `create_dir_all` 重建目录并复制 6 个 .md。

- [ ] **Step 4.4**：启动程序，观察启动日志
```bash
cd REDACTED_USER_PATH/tools/ai-bot
./niu > /tmp/niu_startup.log 2>&1 &
NIU_PID=$!
echo "Niu PID: $NIU_PID"
# 等待 5 秒让启动器跑完 init_niu_dir
sleep 5
```

- [ ] **Step 4.5**：检查启动日志中的 skills 复制记录
```bash
grep -E "Copied skill|Skills directory sync|skills directory" /tmp/niu_startup.log
```
**预期输出**（6 条 Copied + 1 条汇总）：
```
... Copied skill file: brain-region-management.md -> REDACTED_USER_PATH/.niu/skills/brain-region-management.md
... Copied skill file: browser-automation.md -> REDACTED_USER_PATH/.niu/skills/browser-automation.md
... Copied skill file: note-management.md -> REDACTED_USER_PATH/.niu/skills/note-management.md
... Copied skill file: office-docs.md -> REDACTED_USER_PATH/.niu/skills/office-docs.md
... Copied skill file: photo-face-display.md -> REDACTED_USER_PATH/.niu/skills/photo-face-display.md
... Copied skill file: report-skill.md -> REDACTED_USER_PATH/.niu/skills/report-skill.md
... Skills directory sync: copied=6, skipped(existing)=0, dst=REDACTED_USER_PATH/.niu/skills
```

- [ ] **Step 4.6**：确认 `~/.niu/skills/` 被填充
```bash
ls -la ~/.niu/skills/
```
**预期**：6 个 .md 文件 + `.DS_Store`（不应被复制，因为代码过滤了 `.md` 后缀）。
**关键检查**：
- 6 个 .md 文件都存在
- `.DS_Store` **不存在**（验证后缀过滤生效）
- 文件大小跟 `memory/skills/` 里的一致（用 `diff` 验证内容一致）

```bash
# 验证内容一致
for f in brain-region-management browser-automation note-management office-docs photo-face-display report-skill; do
    diff ~/.niu/skills/$f.md REDACTED_USER_PATH/tools/ai-bot/memory/skills/$f.md > /dev/null
    if [ $? -eq 0 ]; then
        echo "OK: $f.md content matches"
    else
        echo "MISMATCH: $f.md"
    fi
done
```
**预期**：6 个文件全部 `OK: ... content matches`。

- [ ] **Step 4.7**：验证"已存在不覆盖"逻辑（保护用户修改）
```bash
# 修改一个文件模拟用户修改
echo "<!-- 用户自定义修改 -->" >> ~/.niu/skills/report-skill.md
# 记录修改后的内容指纹
md5 ~/.niu/skills/report-skill.md

# 杀掉 niu 进程
kill -TERM $NIU_PID 2>/dev/null
sleep 3

# 重新启动
cd REDACTED_USER_PATH/tools/ai-bot
./niu > /tmp/niu_startup2.log 2>&1 &
NIU_PID2=$!
sleep 5

# 验证 report-skill.md 没被覆盖（内容指纹不变）
md5 ~/.niu/skills/report-skill.md
# 两个 md5 应该一致
```
**预期日志**（`/tmp/niu_startup2.log`）：
```
... Skill file already exists, skipping: REDACTED_USER_PATH/.niu/skills/report-skill.md  (debug 级别，可能不输出)
... Skills directory sync: copied=0, skipped(existing)=6, dst=REDACTED_USER_PATH/.niu/skills
```
**关键检查**：
- `copied=0, skipped(existing)=6`（全部 6 个都已存在，全部跳过）
- `report-skill.md` 的 md5 在第二次启动前后一致（用户修改被保护）

- [ ] **Step 4.8**：验证"目录存在但为空"场景
```bash
# 杀进程
kill -TERM $NIU_PID2 2>/dev/null
sleep 3

# 清空 skills 目录但保留目录本身（模拟"目录存在但为空"）
rm -rf ~/.niu/skills/*
ls -la ~/.niu/skills/  # 应为空目录

# 启动
cd REDACTED_USER_PATH/tools/ai-bot
./niu > /tmp/niu_startup3.log 2>&1 &
NIU_PID3=$!
sleep 5

# 验证 6 个 .md 被复制
ls ~/.niu/skills/*.md | wc -l  # 应为 6
grep "Skills directory sync" /tmp/niu_startup3.log
# 应输出 copied=6, skipped(existing)=0
```

- [ ] **Step 4.9**：恢复用户修改（如果 Step 4.2 有备份）
```bash
# 杀进程
kill -TERM $NIU_PID3 2>/dev/null
sleep 3

# 如果 Step 4.2 备份了用户原始 skills，恢复回去
if [ -d ~/.niu/skills.bak ]; then
    rm -rf ~/.niu/skills
    mv ~/.niu/skills.bak ~/.niu/skills
    echo "Restored user's original skills"
fi
```
**说明**：测试用例修改了 `report-skill.md` 加了一行注释，测试完应该恢复。如果用户原始 skills 是空的（用户报告的场景），跳过这步。

- [ ] **Step 4.10**：测试完彻底杀进程（铁律 #7）
```bash
ps aux | grep -E "niu|launcher" | grep -v grep | awk '{print $2}' | xargs -I {} kill -TERM {} 2>/dev/null
sleep 3
ps aux | grep -E "niu|launcher" | grep -v grep  # 应为空
```

---

### Task 5: 提交修复

- [ ] **Step 5.1**：检查改动范围
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
git diff launcher/src/main.rs
```
**预期**：只有 `launcher/src/main.rs` 改动 + 计划文件（`docs/superpowers/plans/2026-07-09-startup-template-restore.md`）。

- [ ] **Step 5.2**：提交修复
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add launcher/src/main.rs docs/superpowers/plans/2026-07-09-startup-template-restore.md
git commit -m "$(cat <<'EOF'
fix(launcher): init_niu_dir 补回 skills 目录模板复制

启动时 init_niu_dir 只复制 memory.json/preferences.json，不复制
skills/ 目录。用户清空 ~/.niu/skills/ 后重启程序，skills 不自动
恢复，SkillSync 扫描拿到空目录，skills 功能失效。

修复（在 init_niu_dir 函数末尾追加 skills 复制逻辑）：
1. 拼接源目录 memory/skills 和目标目录 ~/.niu/skills
2. create_dir_all 目标目录（不存在则创建，已存在幂等）
3. read_dir 遍历源目录，过滤 *.md 后缀（跳过 .DS_Store）
4. 每个文件检查 dst.exists()，已存在跳过保护用户修改
5. 不存在才 fs::read+fs::write 复制
6. 汇总日志记录 copied / skipped(existing) 数量

触发条件全覆盖：
- 目录不存在 → create_dir_all + 复制全部
- 目录存在但为空 → 复制全部
- 某个 .md 缺失 → 只复制缺失的
- 全部已存在 → 全部跳过（copied=0, skipped=6）

铁律 #8：编译用 ./launcher/build.sh，禁止 cargo build。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5.3**：git 操作后修复文件权限（铁律 #7）
```bash
cd REDACTED_USER_PATH/tools/ai-bot
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

- [ ] **Step 5.4**：验证提交成功
```bash
cd REDACTED_USER_PATH/tools/ai-bot
git log --oneline -3
git status
```

---

## Self-Review

### 改动最小化检查

- [x] **只改一个代码文件**：`launcher/src/main.rs` 的 `init_niu_dir` 函数追加 skills 复制块
- [x] **不动现有 memory.json / preferences.json 复制逻辑**：L676-700 的 for 循环完全保留
- [x] **不动调用点**：L1077 `init_niu_dir(&project_root)` 不变，函数内部扩展
- [x] **不动 Python 启动流程**：`niu_api/__main__.py` lifespan 不动
- [x] **不动 SkillSync 扫描逻辑**：`agent/injector/sync.py` 不动，验证修复后扫描能拿到文件
- [x] **不动 build.sh**：用现有脚本编译，不改

### 复制逻辑放 Rust 还是 Python 的选择

- [x] **选 Rust**：时序优势（启动器先跑，Python 启动时 skills 目录已就绪）+ 一致性（与现有两个文件复制同函数同语言）+ API 够用（`fs::read_dir` / `create_dir_all` / `write` 完全够）
- [x] **不选 Python**：放 Python lifespan 会跟 Rust 的 `init_niu_dir` 重复逻辑分裂在两个语言两个文件；放 Python 的话需要在 SkillSync 启动前（lifespan 第 8 步前）插入，但那样不如一处维护

### 触发条件覆盖检查

- [x] **目录不存在**：`fs::create_dir_all(&dst_skills_dir)` 创建目录 + `fs::read_dir` 遍历复制全部
- [x] **目录存在但为空**：`create_dir_all` 幂等返回 Ok，`read_dir` 遍历复制全部（dst 文件不存在）
- [x] **某个 .md 缺失**：`dst_path.exists()` 跳过已存在的，复制缺失的
- [x] **全部已存在**：全部 `dst_path.exists()` 跳过，`copied=0, skipped=6`
- [x] **源目录不存在**：`!src_skills_dir.exists()` warn 后 return，不报错不阻塞启动

### 已存在文件不覆盖的保护

- [x] **核心保护**：`if dst_path.exists() { skipped_count += 1; continue; }`
- [x] **debug 级别日志**：跳过时 `debug!` 不刷屏（用户修改的文件每次启动都跳过）
- [x] **汇总日志**：`info!` 级别记录 `skipped(existing)=N`，方便排查

### 编译用 launcher/build.sh 是否明确

- [x] **Task 3 明确要求用 `./launcher/build.sh`**：铁律 #8
- [x] **Task 2.3 的 cargo check 不冲突**：`cargo check` 只做语法检查不产出二进制，跟铁律 #8 不冲突（铁律禁止的是 `cargo build` 替代 `build.sh` 做编译产出）
- [x] **Task 3.2 验证 `niu` 二进制时间戳**：确认 `cp target/release/niu-launcher ../niu` 执行了

### 引入新 bug 的风险

- [x] **风险一：复制时权限丢失**
  - 评估：`fs::read` + `fs::write` 会用默认权限（umask），但 `.md` 文件不需要可执行权限，rw-r--r-- 即可
  - 结论：低风险；Task 5.3 跑权限修复脚本兜底（虽然 .md 不需要可执行，但 python/bin/ 和 node_modules/.bin/ 需要）
- [x] **风险二：并发问题（多个 niu 实例同时启动）**
  - 评估：用户场景下不会同时启动两个 niu 实例（启动器有 splash window 单实例逻辑）；即便发生，`fs::write` 是原子性的（POSIX rename），最坏情况是后启动的实例覆盖前一个的写入，但内容一致所以无影响
  - 结论：低风险，不需要额外处理
- [x] **风险三：源目录有意外文件类型（如子目录）**
  - 评估：`fs::read_dir` 遍历 entry，`entry.path()` 拿到路径，`fs::read` 读子目录会失败返回 Err，warn 后 continue
  - 结论：低风险，已用 `match fs::read` 兜底
- [x] **风险四：`.DS_Store` 被复制**
  - 评估：`!file_name_str.ends_with(".md")` 过滤会跳过 `.DS_Store`
  - 结论：无风险，Task 4.6 验证 `.DS_Store` 不存在
- [x] **风险五：文件名含非 UTF-8 字符**
  - 评估：`file_name.to_str()` 返回 None 时 continue 跳过
  - 结论：低风险，已兜底
- [x] **风险六：磁盘满或权限不足**
  - 评估：`fs::write` 失败时 error 日志 + continue，不阻塞后续启动
  - 结论：低风险，跟现有 `template_files` 复制策略一致

### 测试覆盖检查

- [x] **目录不存在场景**：Task 4.3 `rm -rf ~/.niu/skills` + 启动验证 6 个 .md 被复制
- [x] **目录存在但为空场景**：Task 4.8 `rm -rf ~/.niu/skills/*` 保留目录 + 启动验证 6 个 .md 被复制
- [x] **已存在不覆盖场景**：Task 4.7 修改一个 .md + 重启验证内容指纹不变
- [x] **内容一致性**：Task 4.6 用 `diff` 验证 6 个文件内容跟模板一致
- [x] **后缀过滤生效**：Task 4.6 验证 `.DS_Store` 不被复制

---

## Execution Handoff

执行顺序（**严格按 Task 0 → 5 顺序**）：

1. **Task 0**：临时备份提交（铁律 #3）
2. **Task 1**：读当前实现 + 设计新增代码块（不写文件，只读+设计）
3. **Task 2**：用 Edit 工具改 `launcher/src/main.rs` 追加 skills 复制逻辑 + `cargo check` 语法检查
4. **Task 3**：用 `./launcher/build.sh` 编译（铁律 #8）+ 修复文件权限（铁律 #7）
5. **Task 4**：真实端到端验证（清空 `~/.niu/skills/` 启动确认被填充，铁律 #5）
6. **Task 5**：提交修复 + 修复文件权限

**关键约束**：
- 每个 Step 都要打勾 `- [ ]` → `- [x]`
- 任何 Step 失败立即停下，不要继续
- 调试无效立即撤销改动恢复原状（铁律 #5 调试无效马上撤销）
- 派出去的子 Agent 必须遵守所有铁律（特别是 #3 备份、#5 真实测试、#7 修权限、#8 用 build.sh 不用 cargo build）
- 杀进程用 `kill -TERM` 优雅退出，禁止 `pkill -f niu` 强杀（曾导致 LightRAG vdb 文件损坏）
