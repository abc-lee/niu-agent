# 跨平台协作工作流程

## 架构设计

```
Windows (主开发环境)          Mac (跨平台测试环境)
     ↓                              ↓
  master 分支                  mac-testing 分支
     ↓                              ↓
  推送到远程                    推送到远程
     ↓                              ↓
        ←←← 定期同步 →→→→
                ↓
        选择性合并跨平台修改
```

---

## 设置步骤

### 1. 创建远程仓库（GitHub/Gitee）

**选择平台**：
- **GitHub**：国际访问，速度快，功能完善
- **Gitee**：国内访问，速度快，免费私有仓库

**创建步骤**：
```bash
# 方案 A: GitHub
1. 访问 https://github.com/new
2. 创建仓库：niu-agent
3. 设置为私有仓库（推荐）

# 方案 B: Gitee
1. 访问 https://gitee.com/projects/new
2. 创建仓库：niu-agent
3. 设置为私有仓库
```

---

### 2. Windows 端配置

```bash
# 添加远程仓库
cd E:\tools\ai-bot
git remote add origin https://github.com/YOUR_USERNAME/niu-agent.git
# 或 Gitee
git remote add origin https://gitee.com/YOUR_USERNAME/niu-agent.git

# 推送 master 分支
git push -u origin master

# 推送所有分支
git push --all origin
```

---

### 3. Mac 端配置

```bash
# Clone 仓库
cd ~/projects
git clone https://github.com/YOUR_USERNAME/niu-agent.git
cd niu-agent

# 创建 Mac 测试分支
git checkout -b mac-testing

# 推送到远程
git push -u origin mac-testing
```

---

## 日常工作流程

### Windows 端（主开发）

```bash
# 正常开发流程
git checkout master
# ... 修改代码 ...
git add -A
git commit -m "feat: 新功能"
git push origin master

# 定期查看 Mac 的修改
git fetch origin
git log origin/mac-testing --oneline -10

# 如果有跨平台修改需要合并
git merge origin/mac-testing
# 解决冲突后
git push origin master
```

---

### Mac 端（跨平台测试）

```bash
# 切换到测试分支
git checkout mac-testing

# 同步 Windows 最新代码
git pull origin master

# 测试和修改
# ... 测试跨平台兼容性 ...
# ... 修改路径、权限等问题 ...

# 提交修改
git add -A
git commit -m "fix(mac): 修复 macOS 路径兼容性问题

- 修复路径分隔符问题（/ vs \\）
- 修复文件权限问题
- 测试通过：Python 3.11, macOS 14"

git push origin mac-testing

# 如果确定可以合并到主分支
# 在 GitHub/Gitee 创建 Pull Request
# 标题：fix(mac): 跨平台兼容性修复
# 描述：修复内容、测试结果
```

---

## 跨平台修改识别规则

### ✅ 需要合并到 master 的修改

**1. 路径处理**
```python
# ❌ 错误：硬编码路径分隔符
path = "E:\\tools\\ai-bot"

# ✅ 正确：跨平台路径
import os
path = os.path.join("E:", "tools", "ai-bot")  # Windows
path = os.path.join(os.path.expanduser("~"), "tools", "ai-bot")  # macOS

# ✅ 或者使用 pathlib
from pathlib import Path
path = Path.home() / "tools" / "ai-bot"
```

**2. 环境检测**
```python
import platform
import sys

# 检测操作系统
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# 跨平台处理
if IS_WINDOWS:
    # Windows 特定逻辑
    pass
elif IS_MACOS:
    # macOS 特定逻辑
    pass
```

**3. Python 版本兼容**
```python
# ✅ 兼容 Python 3.11+
from typing import Optional, Dict, List

# ❌ 避免 Python 3.12+ 特性
# match case 语句（如果需要兼容 3.11）
```

**4. 依赖处理**
```toml
# pyproject.toml
[project.dependencies]
# 标注平台特定依赖
psutil = ">=5.9.0"  # 跨平台系统监控
pywin32 = {version = ">=305", markers = "sys_platform == 'win32'"}  # Windows only
pyobjc = {version = ">=10.0", markers = "sys_platform == 'darwin'"}  # macOS only
```

---

### ❌ 不需要合并的修改

**1. 平台特定配置**
```python
# Windows 特定路径
WINDOWS_MODELS_DIR = "E:\\models"

# macOS 特定路径
MACOS_MODELS_DIR = "/Users/YOUR_NAME/models"
```

**2. 个人偏好设置**
```json
{
  "editor.fontSize": 14,  // Windows
  "editor.fontSize": 16   // Mac（可能不同）
}
```

**3. 临时测试代码**
```python
# 临时测试
print("Mac testing...")
```

---

## Pull Request 规范

### PR 标题格式

```
fix(mac): 修复 macOS 路径兼容性问题
feat(cross-platform): 添加跨平台环境检测
test(mac): macOS 测试通过
```

### PR 描述模板

```markdown
## 修改内容
- 修复路径分隔符问题（使用 os.path.join）
- 修复文件权限问题（chmod 755 → 644）
- 添加平台检测逻辑

## 测试结果
- ✅ Windows 11 测试通过
- ✅ macOS 14 测试通过
- ✅ Python 3.11 兼容

## 跨平台影响
- 是否影响现有 Windows 功能：否
- 需要修改的文件：agent/runner.py, agent/vector_search.py
```

---

## 冲突处理

### 场景 1：同一文件不同修改

```bash
# Windows 端
git checkout master
git merge origin/mac-testing

# 出现冲突
# 打开冲突文件，手动解决
<<<<<<< HEAD
# Windows 的修改
=======
# Mac 的修改
>>>>>>> mac-testing

# 保留跨平台兼容的部分
# 删除冲突标记
git add <file>
git commit -m "merge: 合并 macOS 兼容性修复"
git push origin master
```

### 场景 2：Mac 落后于 master

```bash
# Mac 端
git checkout mac-testing
git pull origin master

# 如果有冲突
git status
# 解决冲突
git add <file>
git commit -m "merge: 同步 master 最新代码"
git push origin mac-testing
```

---

## 自动化工具

### 1. Git Hooks - 自动检测跨平台问题

**创建文件**：`.git/hooks/pre-commit`

```bash
#!/bin/bash
# 检测硬编码路径
if grep -r "E:\\\\tools" --include="*.py" .; then
    echo "❌ 检测到硬编码 Windows 路径"
    echo "请使用 os.path.join() 或 pathlib.Path"
    exit 1
fi

if grep -r "/Users/" --include="*.py" . | grep -v "expanduser"; then
    echo "❌ 检测到硬编码 macOS 路径"
    echo "请使用 Path.home() 或 os.path.expanduser('~')"
    exit 1
fi

echo "✅ 跨平台检查通过"
exit 0
```

### 2. CI/CD - 自动测试跨平台

**创建文件**：`.github/workflows/test.yml`

```yaml
name: Cross-Platform Test

on: [push, pull_request]

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, macos-latest, ubuntu-latest]
        python-version: [3.11, 3.12]

    steps:
    - uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: ${{ matrix.python-version }}

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -e agent
        pip install -e niu_api

    - name: Run tests
      run: |
        pytest agent/tests -v
```

---

## 最佳实践

### 1. 分支命名规范

```bash
master              # Windows 主开发分支
mac-testing         # Mac 测试分支
feature/xxx         # 功能分支（跨平台）
fix/cross-platform  # 跨平台修复分支
```

### 2. Commit 消息规范

```
feat(cross-platform): 添加平台检测功能
fix(mac): 修复 macOS 文件权限问题
test(mac): macOS 测试通过
docs(cross-platform): 添加跨平台开发指南
```

### 3. 定期同步

```bash
# Windows - 每天推送
git push origin master

# Mac - 每天拉取
git checkout mac-testing
git pull origin master
```

---

## 常见问题

### Q1: 如何知道哪些修改是跨平台兼容的？

**A**: 查看 commit 记录：
```bash
# Mac 端
git log origin/master..HEAD --oneline

# Windows 端
git log origin/mac-testing --oneline
```

### Q2: 如何避免频繁冲突？

**A**:
1. Mac 端每天同步 master
2. 修改前先 pull 最新代码
3. 小步提交，频繁推送
4. 跨平台修改单独提交，不混合业务逻辑

### Q3: 测试通过后如何合并？

**A**: 两种方式：
1. **Pull Request**（推荐）：在 GitHub/Gitee 创建 PR，审核后合并
2. **直接合并**：`git checkout master && git merge mac-testing`

---

## 总结

**优点**：
- ✅ Windows 和 Mac 独立开发，互不影响
- ✅ 清晰的跨平台修改追踪
- ✅ 选择性合并，避免污染主分支
- ✅ 完整的测试和审核流程

**缺点**：
- ❌ 需要定期同步代码
- ❌ 可能出现冲突需要手动解决

**建议**：
- 使用 Pull Request 进行代码审核
- 小步提交，频繁同步
- 跨平台修改单独提交，方便追踪
