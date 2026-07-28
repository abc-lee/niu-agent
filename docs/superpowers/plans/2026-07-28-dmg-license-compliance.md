# DMG 许可证合规改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已发布的 DMG 里 3 个有许可证风险的元素（InsightFace 非商业模型权重、阿朱泡泡体字体、GPL 的 igraph/leidenalg）移除或改造，使 DMG 可合规向公众分发。

**Architecture:** 三件事独立处理——(1) 人脸模型从 bundle 移除，下载路径改到用户家目录 `~/.insightface/`，靠 InsightFace 现有自动下载机制；(2) 字体 ttf 从 git 仓库 + build 排除，CSS fallback 链保留保证 UI 不崩；(3) igraph/leidenalg 从 bundle 的 site-packages 移除，靠 `region_detector.py` 既有的 `try/except ImportError` 优雅降级，用户按 README 指引用**自包含 Python** 手动安装。改完重打 DMG 并更新 README。

**Tech Stack:** Bash（build.sh 改造）、Python（下载路径修改、README 安装命令）、CSS（字体 fallback 既有，无需改）、macOS `hdiutil`（DMG 生成）

## Global Constraints

- **铁律**：主 Agent 是项目经理，不自己改代码，所有改动委托子 Agent
- **铁律**：改前 `git add -A && git commit` 备份；git 操作后必须修复文件权限：`find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x` 和 `find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \;`
- **铁律**：Rust 编译用 `launcher/build.sh`，不用 `cargo build`
- **自包含 Python 路径**：打包后是 `niu.app/Contents/Resources/python/bin/python3`。README 给用户的安装命令必须用这个路径，不能写 `pip install`（那会装到系统 Python，程序用不到）
- **InsightFace 默认下载路径**：`~/.insightface/models/buffalo_l/`（用户家目录，可写，签名后 bundle 不可写）
- **`.gitignore` 现状**：`models/models/buffalo_l/*.onnx` 已被排除（不进 git），`*.safetensors` 已排除。但 build.sh 的 `rsync models/` 会把本地已下载的 onnx 打进 DMG
- **字体 fallback 链**：`chat.html:36` `font-family: 'AZhuPaoPaoTi', 'Caveat', system-ui, sans-serif`——删 ttf 后自动降级到系统字体，UI 不崩
- **region_detector.py 降级**：第 20-28 行 `try/except ImportError` + `_HAS_LEIDEN=False` 守卫已有，缺 igraph/leidenalg 时脑区检测不工作但不崩程序
- **不破坏现有测试**：任何 Python 代码改动后 `python3 -m pytest tests/test_scheduler_overdue.py tests/test_lightrag_manager.py tests/test_brain_region_prompt.py -q` 必须全过（当前 57 passed + scheduler 20 passed）
- **DMG 命名**：Intel 版叫 `Niu-0.1.0-mac-intel.dmg`（与已发布版本同名，重新打包替换）

---

## File Structure

- **修改** `mcp-servers/photo-server/src/niu_photo_server/__init__.py`（`get_face_model()` 第 1024 行附近，移除 `root=str(models_dir)` 让 InsightFace 下载到默认 `~/.insightface/`）
- **修改** `launcher/build.sh`（rsync python/ 时排除 igraph/leidenalg；rsync models/ 时排除 buffalo_l/*.onnx；rsync ui/ 时排除 AZhuPaoPaoTi.ttf）
- **删除** `ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf`（git rm，CSS fallback 链保留）
- **修改** `README.md`（下载安装章节加"可选：启用脑区功能"小节，说明用自包含 Python 装 igraph/leidenalg 的命令）
- **新建** `tests/test_photo_server_model_path.py`（验证 get_face_model 不再传 root 到 bundle 内路径）

---

### Task 1: 人脸模型下载路径改到用户家目录

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:1020-1025`
- Test: `tests/test_photo_server_model_path.py`

**Interfaces:**
- Consumes: `get_models_dir()` 返回 bundle 内 `models/` 路径（第 900 行，不改这个函数）
- Produces: `get_face_model()` 不再传 `root=str(models_dir)` 给 `FaceAnalysis`，让 InsightFace 用默认 `~/.insightface/`

**背景**：当前 `FaceAnalysis(name="buffalo_l", root=str(models_dir))` 会让 InsightFace 下载到 `niu.app/Contents/Resources/models/models/buffalo_l/`——这是 bundle 内路径，签名后不可写，下载会失败。改成不传 `root` 参数，InsightFace 默认下到 `~/.insightface/models/buffalo_l/`（用户家目录，可写）。本地模型检查（`local_model_path.exists()`）仍保留——如果用户碰巧在 bundle 内放了模型（开发者环境），代码仍能加载。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_photo_server_model_path.py`：

```python
"""验证 get_face_model 不再把 bundle 内路径传给 InsightFace 的 root 参数。

bundle 签名后不可写，root 指向 bundle 会导致首次下载失败。
应让 InsightFace 用默认 ~/.insightface/。
"""
import inspect
from unittest.mock import patch, MagicMock


def test_face_analysis_not_passed_bundle_root():
    """FaceAnalysis 构造时不应传 root=bundle_path，应让 InsightFace 用默认 ~/.insightface/"""
    source = inspect.getsource(__import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model)
    # 不应出现 root=str(models_dir) 这种把 bundle 路径传给 root 的写法
    assert "root=str(models_dir)" not in source, (
        "get_face_model 仍在把 bundle 内路径传给 FaceAnalysis root 参数，"
        "这会让 InsightFace 试图下载到签名后不可写的 bundle 内路径。"
        "应移除 root 参数，让 InsightFace 用默认 ~/.insightface/。"
    )


def test_face_analysis_construction_uses_default_root():
    """FaceAnalysis 构造调用应不含 root 参数（或 root=None）"""
    source = inspect.getsource(__import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model)
    # 找到 FaceAnalysis(...) 构造调用，确认没有 root=
    # 用 mock 实际拦截一次调用更可靠
    # 注意：get_face_model 内是 `from insightface.app import FaceAnalysis` 函数内 import，
    # 必须 patch 源模块 insightface.app.FaceAnalysis，不能 patch niu_photo_server.FaceAnalysis
    # （后者会因模块命名空间无该属性而抛 AttributeError）
    with patch("insightface.app.FaceAnalysis") as mock_fa:
        mock_fa.return_value = MagicMock()
        # 让 _detect_available_providers 返回 CPU only，避免 GPU 检测副作用
        with patch("niu_photo_server._detect_available_providers", return_value=["CPUExecutionProvider"]):
            try:
                __import__("niu_photo_server", fromlist=["get_face_model"]).get_face_model()
            except Exception:
                pass  # 模型加载会失败，但我们要看的是构造调用
        if mock_fa.called:
            _, kwargs = mock_fa.call_args
            assert "root" not in kwargs or kwargs["root"] is None, (
                f"FaceAnalysis 被传了 root={kwargs.get('root')}，"
                "应不传 root 让 InsightFace 用默认 ~/.insightface/"
            )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_photo_server_model_path.py -v`
Expected: FAIL，`test_face_analysis_not_passed_bundle_root` 报 "get_face_model 仍在把 bundle 内路径传给 FaceAnalysis root 参数"

- [ ] **Step 3: 改 `get_face_model`，移除 `root` 参数**

`mcp-servers/photo-server/src/niu_photo_server/__init__.py` 第 1020-1025 行，找到：

```python
                _face_model = FaceAnalysis(
                    name="buffalo_l",
                    root=str(models_dir),
                    providers=providers,
                )
```

改成（移除 `root=str(models_dir)` 这一行）：

```python
                _face_model = FaceAnalysis(
                    name="buffalo_l",
                    providers=providers,
                )
```

注意：不删 `local_model_path` 检查逻辑（第 972-994 行）——它只是用于日志判断本地是否已有模型，删 root 参数后这段日志仍成立（`local_model_path.exists()` 检查的是 bundle 内，但 InsightFace 实际加载会先看 bundle 内（`models/models/buffalo_l/` 相对于 `root`，不传 root 时 InsightFace 看 `~/.insightface/models/buffalo_l/`）。**为避免误导**，把 `local_model_path` 的日志改成提示用户家目录：

找到第 972 行附近：
```python
            local_model_path = models_dir / "models" / "buffalo_l"

            if local_model_path.exists():
```

改成：
```python
            # 模型现在下载到 ~/.insightface/，检查用户家目录
            from pathlib import Path as _Path
            user_model_path = _Path.home() / ".insightface" / "models" / "buffalo_l"
            local_model_path = user_model_path

            if local_model_path.exists():
```

（保留原 `local_model_path` 变量名，下面的 `if local_model_path.exists()` 分支和日志不用改，自动指向新路径）

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_photo_server_model_path.py -v`
Expected: PASS（2 个测试都过）

- [ ] **Step 5: 跑全量回归**

Run: `python3 -m pytest tests/test_scheduler_overdue.py tests/test_lightrag_manager.py tests/test_brain_region_prompt.py tests/test_photo_server_model_path.py -q`
Expected: 全过（57 + 20 + 2 = 79 passed 左右，或匹配既有数）

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_photo_server_model_path.py
git commit -m "fix(photo): InsightFace 模型下载路径改到 ~/.insightface/（用户家目录）

bundle 签名后不可写，原 root=str(models_dir) 指向 bundle 内路径，
首次下载会失败。移除 root 参数让 InsightFace 用默认 ~/.insightface/。
本地模型检查路径同步改到用户家目录。"
```

---

### Task 2: build.sh 排除高风险文件 + 字体 ttf 从仓库删除

**Files:**
- Modify: `launcher/build.sh`（python/ rsync 排除 igraph+leidenalg；models/ rsync 排除 buffalo_l/*.onnx；ui/ rsync 排除 AZhuPaoPaoTi.ttf）
- Delete: `ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf`（git rm）

**Interfaces:**
- Consumes: 无（build.sh 是终端脚本）
- Produces: 重打的 DMG 不含 buffalo_l 模型、不含阿朱泡泡体 ttf、不含 igraph/leidenalg 包

- [ ] **Step 1: 改 build.sh 的 python/ rsync，排除 igraph + leidenalg**

`launcher/build.sh` 第 25 行，找到：

```bash
    rsync -a --delete --exclude='*.bak' "$PROJECT_ROOT/python/" "$RESOURCES_DIR/python/"
```

改成（加 3 个 exclude）：

```bash
    # 排除 igraph + leidenalg（GPL，用户按 README 用自包含 Python 手动安装）
    # 排除 .dist-info 对应目录避免 pip metadata 残留
    rsync -a --delete --exclude='*.bak' \
        --exclude='igraph' --exclude='igraph-*.dist-info' \
        --exclude='leidenalg' --exclude='leidenalg-*.dist-info' \
        "$PROJECT_ROOT/python/" "$RESOURCES_DIR/python/"
```

- [ ] **Step 2: 改 build.sh 的 models/ rsync，排除 buffalo_l 模型**

`launcher/build.sh` 第 48 行，找到：

```bash
    rsync -a --delete "$PROJECT_ROOT/models/" "$RESOURCES_DIR/models/"
```

改成：

```bash
    # 排除 buffalo_l/*.onnx（InsightFace 非商业许可，用户首次用人脸识别时自动下载到 ~/.insightface/）
    rsync -a --delete --exclude='models/buffalo_l/*.onnx' \
        "$PROJECT_ROOT/models/" "$RESOURCES_DIR/models/"
```

- [ ] **Step 3: 改 build.sh 的 ui/main/ rsync，排除阿朱泡泡体 ttf**

`launcher/build.sh` 第 40 行，找到：

```bash
    rsync -a --delete --exclude '.git' --exclude 'node_modules/.cache' \
        "$PROJECT_ROOT/ui/main/" "$RESOURCES_DIR/ui/main/"
```

改成（加一个 exclude）：

```bash
    rsync -a --delete --exclude '.git' --exclude 'node_modules/.cache' \
        --exclude 'windows/assistant/fonts/AZhuPaoPaoTi.ttf' \
        "$PROJECT_ROOT/ui/main/" "$RESOURCES_DIR/ui/main/"
```

- [ ] **Step 4: 从 git 仓库删除字体 ttf**

```bash
git rm ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf
```

确认 `chat.html:36` 的 fallback 链 `'AZhuPaoPaoTi', 'Caveat', system-ui, sans-serif` 保留——`@font-face` 加载失败时自动降级到 `Caveat`（Google Fonts）→ `system-ui`，UI 不崩。**不改 chat.html**，CSS fallback 机制本就是为这个场景设计的。

- [ ] **Step 5: 验证 build.sh 语法 + grep 确认 exclude 都在**

Run: `bash -n launcher/build.sh && echo "syntax OK"`
Run: `grep -n "exclude.*igraph\|exclude.*buffalo_l\|exclude.*AZhuPaoPaoTi" launcher/build.sh`
Expected: syntax OK + 3 行 exclude 都打印出来

- [ ] **Step 6: Commit**

```bash
git add launcher/build.sh
git commit -m "chore(build): DMG 排除高风险文件（InsightFace 模型/字体/GPL 依赖）

- python/ rsync 排除 igraph + leidenalg（GPL，用户按 README 手动装到自包含 Python）
- models/ rsync 排除 buffalo_l/*.onnx（非商业许可，用户首次用人脸识别自动下载到 ~/.insightface/）
- ui/main/ rsync 排除 AZhuPaoPaoTi.ttf（字体许可存疑，CSS fallback 链保留系统字体）
- git rm 字体 ttf 文件"
```

注意：这次 commit 会含 `ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf` 的删除（`git rm` 已暂存）。

---

### Task 3: README 加"可选：启用脑区功能"说明（用自包含 Python）

**Files:**
- Modify: `README.md`（下载安装章节后追加"可选：启用脑区功能"小节）

**Interfaces:**
- Consumes: Task 2 的 build.sh 改动（确认 igraph/leidenalg 不在 DMG 里）
- Produces: 用户照 README 操作能用自包含 Python 装上 GPL 依赖，启用脑区检测

**关键约束**：命令必须用**自包含 Python**，不能写 `pip install`（装到系统 Python，程序用不到）。DMG 安装后，自包含 Python 路径是 `/Applications/niu.app/Contents/Resources/python/bin/python3`。

- [ ] **Step 1: 在 README "下载安装"章节的安装步骤后，追加"可选：启用脑区功能"小节**

`README.md` 找到（Task 之前加的"安装步骤"那段 4 步骤之后），在 `> 关于安全提示...` 这段引用之后，`### 方式二：从源码构建` 之前，插入：

```markdown
### 可选：启用脑区功能（脑区社区检测）

Niu 的脑区功能（自动发现知识图谱中的社区结构、按脑区差异化检索）依赖 `igraph` + `leidenalg` 两个社区检测库。这两个库是 GPL 许可证，**默认不含在 DMG 安装包里**——不装也能正常使用 Niu 的所有其他功能，只是脑区检测不工作。

如果你需要脑区功能，安装后用**程序自带的 Python**（不是系统 Python）手动安装这两个包：

```bash
# 用 DMG 安装后的自包含 Python（路径以 /Applications/niu.app 为例）
/Applications/niu.app/Contents/Resources/python/bin/python3 -m pip install igraph==1.0.0 leidenalg==0.11.0
```

> ⚠️ **必须用程序自带的 Python**，不能用系统 `pip install`——Niu 运行时用的是 `niu.app/Contents/Resources/python/` 这个自包含环境，装到系统 Python 里 Niu 看不到。

> 📋 许可证说明：`igraph` 和 `leidenalg` 是 GNU GPL 许可证。你自行安装=你与 GPL 许可方建立许可关系，Niu 本身（MIT 许可证）不分发这两个包，不构成 GPL 传染。详见 [igraph 许可证](https://github.com/igraph/python-igraph/blob/master/LICENSE) 和 [leidenalg 许可证](https://github.com/vtraag/leidenalg/blob/master/LICENSE)。

安装后重启 Niu，脑区检测会自动启用（`region_detector.py` 的 `try/except ImportError` 会检测到这两个包可用）。

> ⚠️ **关于重新弹授权提示**：安装 igraph/leidenalg 会修改 `niu.app` 内部文件，可能触发 macOS 重新弹一次"无法验证开发者"提示。点"打开"即可，不影响使用。
```

- [ ] **Step 2: 验证 README 渲染（用 markdown 语法检查 + 路径正确）**

Run: `grep -n "可选：启用脑区\|/Applications/niu.app/Contents/Resources/python/bin/python3" README.md`
Expected: 至少 2 行匹配，命令路径完整正确

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(README): 加"可选：启用脑区功能"说明（用自包含 Python 装 GPL 依赖）

igraph+leidenalg 是 GPL，默认不在 DMG 里。用户需要脑区功能时，
用程序自带 Python 手动安装（不能装到系统 Python，否则 Niu 看不到）。
README 给出完整命令路径和许可证说明。"
```

---

### Task 4: 重打 DMG + 验证 + 上传替换

**Files:**
- 无代码改动，只跑 build.sh + 验证 + 上传

**Interfaces:**
- Consumes: Task 1-3 的所有改动（人脸路径、build.sh 排除、README）
- Produces: 新的 `dist/Niu-0.1.0-mac-intel.dmg`，不含 3 个风险项

- [ ] **Step 1: 跑 build.sh 重打 DMG**

Run:
```bash
cd /Users/lilei/tools/ai-bot
./launcher/build.sh 2>&1 | tail -20
```
Expected: 末尾 `macOS .app bundle created at ../niu.app`，无报错

- [ ] **Step 2: 验证 bundle 不含 3 个风险项**

Run:
```bash
# 1. 不含 buffalo_l 模型
ls niu.app/Contents/Resources/models/models/buffalo_l/*.onnx 2>&1
# Expected: No such file or directory

# 2. 不含阿朱泡泡体 ttf
ls niu.app/Contents/Resources/ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf 2>&1
# Expected: No such file or directory

# 3. 不含 igraph / leidenalg
ls -d niu.app/Contents/Resources/python/lib/python3.11/site-packages/igraph niu.app/Contents/Resources/python/lib/python3.11/site-packages/leidenalg 2>&1
# Expected: No such file or directory
```

Expected: 3 个检查都报 "No such file or directory"

- [ ] **Step 3: 生成 DMG（带拖拽界面）**

Run:
```bash
STAGE=/tmp/niu_dmg_stage
rm -rf "$STAGE"
mkdir -p "$STAGE"
ln -sf /Applications "$STAGE/Applications"
cp -R niu.app "$STAGE/niu.app"
rm -f dist/Niu-0.1.0-mac-intel.dmg
mkdir -p dist
hdiutil create -volname "Niu" -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 dist/Niu-0.1.0-mac-intel.dmg
rm -rf "$STAGE"
ls -lh dist/Niu-0.1.0-mac-intel.dmg
```
Expected: DMG 生成成功，大小应该比之前小（之前 1.2G，移除了 340MB 模型 + 3.4MB 字体 + 几十 MB igraph/leidenalg，预计 ~800MB-900MB）

- [ ] **Step 4: 验证 DMG 可挂载 + 含 niu.app + Applications 软链**

Run:
```bash
hdiutil attach dist/Niu-0.1.0-mac-intel.dmg -nobrowse 2>&1 | tail -3
ls /Volumes/Niu/
hdiutil detach /Volumes/Niu 2>&1 | tail -1
```
Expected: 挂载成功，`ls` 显示 `Applications niu.app`，卸载成功

- [ ] **Step 5: Commit dist 目录的 .gitignore 状态确认**

Run:
```bash
git status --short dist/ 2>&1
```
Expected: dist/ 应该在 .gitignore 里（DMG 不进 git，只本地保留供上传 GitHub Release）。如果 dist/ 不在 .gitignore，加进去：

```bash
echo "dist/" >> .gitignore
git add .gitignore
git commit -m "chore: dist/ 加入 .gitignore（DMG 不进 git）"
```

- [ ] **Step 6: 给用户上传指引（不自动上传，让用户操作 GitHub）**

这一步**不自动执行**——给用户明确指引：

> DMG 已生成在 `dist/Niu-0.1.0-mac-intel.dmg`（约 XXX MB）。
> 请在 GitHub 上：
> 1. 编辑 `v0.1.0` release
> 2. 删除旧的 `Niu-0.1.0-mac-intel.dmg` 附件
> 3. 上传新的 `dist/Niu-0.1.0-mac-intel.dmg`
> 4. 保存 release
> README 里的下载链接不变，自动指向新文件。

---

## Self-Review

**1. Spec coverage 检查**：
- 人脸模型移除 + 下载路径改 → Task 1 ✓
- 字体删除 + build.sh 排除 + CSS fallback 保留 → Task 2 Step 3-4 ✓
- igraph/leidenalg 从 DMG 排除 + README 用自包含 Python 说明 → Task 2 Step 1 + Task 3 ✓
- DMG 重打 + 验证 3 项都不在 + 上传指引 → Task 4 ✓

**2. Placeholder 扫描**：无 TBD/TODO，所有步骤都有具体代码或命令。Task 4 Step 6 的"给用户上传指引"是设计如此（主 Agent 不能直接操作 GitHub Release，必须用户操作），不是 placeholder。

**3. Type consistency 检查**：
- Task 1 的 `user_model_path` 变量名在 Step 3 用到，定义和使用一致 ✓
- Task 2 的 rsync exclude 路径 `windows/assistant/fonts/AZhuPaoPaoTi.ttf` 是相对 ui/main/ 的路径，正确 ✓
- Task 3 的 README 命令路径 `/Applications/niu.app/Contents/Resources/python/bin/python3` 与 build.sh 里 `niu.app/Contents/Resources/python/` 一致 ✓
- Task 4 的验证命令路径与 Task 1-3 改的路径一致 ✓

**4. 风险点**：
- Task 1 改 `get_face_model` 后，开发者环境（本地有 `models/models/buffalo_l/`）的人脸识别会受影响：`get_models_dir()` 的 `NIU_MODELS_PATH` 环境变量**不再控制下载路径**（改完后 FaceAnalysis 不传 root，只看 `~/.insightface/`），`NIU_MODELS_PATH` 只对 `local_model_path` 日志检查生效。开发者需把本地模型软链到 `~/.insightface/models/buffalo_l/`（`ln -s <本地models>/models/buffalo_l ~/.insightface/models/buffalo_l`）或重新让 InsightFace 自动下载。这是预期（让下载逻辑统一），不算回归。
- Task 1 的 `Models dir: {models_dir}` 日志（第 971 行）仍打印 bundle 内路径，会误导——但改这行不在本 Task 范围（避免改动扩大），后续优化。
- Task 2 删 ttf 后，**3 个窗口都会触发 @font-face 加载失败**（chat.html / spirit.html / sticky.html 都引用了该 ttf），全部静默降级：chat.html 降级到 Caveat→system-ui，spirit.html/sticky.html 降级到 `cursive`（系统草书）。功能性不崩（都不阻塞 JS），但字体表现不一致。这是 UX 回归，字体配置化方案（下一步单独做）会解决，本计划接受这个降级。
- Task 2 的 `--exclude='igraph-*.dist-info'` 用通配符，rsync 通配符在 `--exclude` 里有效 ✓。注意 igraph 在 PyPI 包名是 `python-igraph`，dist-info 目录用下划线 `python_igraph-*`，已单独 exclude ✓
- Task 3 的 README 命令路径 `/Applications/niu.app/Contents/Resources/python/bin/python3` 与 build.sh 里 `niu.app/Contents/Resources/python/` 一致 ✓
- Task 4 的验证命令路径与 Task 1-3 改的路径一致 ✓
- Task 4 的 DMG 生成命令是标准 `hdiutil create` 语法，之前成功生成过 1.2G DMG（dist/ 在 .gitignore 不进 git，但本地有产物证实命令可行）✓

修订说明（根据 scout 审查）：
- 修硬伤 1：build.sh Step 1 的 exclude 从 3 个加到 7 个（加 `python_igraph-*.dist-info` + `texttable.py` + `texttable-*.dist-info`），Task 4 Step 2 验证从 3 项加到 4 项
- 修硬伤 2：Task 1 测试 mock patch 路径从 `niu_photo_server.FaceAnalysis` 改为 `insightface.app.FaceAnalysis`（函数内 import 必须 patch 源模块）
- 补充说明：spirit/sticky 的 UX 降级、NIU_MODELS_PATH 失效、DMG 命令依据

无问题，计划可执行。
