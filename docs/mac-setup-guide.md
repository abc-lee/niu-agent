# Mac 端设置指南

## 1. Clone 仓库

```bash
# 创建工作目录
cd ~/projects  # 或你想放置项目的位置

# Clone 私有仓库（使用 HTTPS）
git clone https://github.com/abc-lee/niu-agent.git
cd niu-agent

# 切换到 Mac 测试分支
git checkout mac-testing
```

## 2. 身份认证

**方式 A：使用 Personal Access Token**

```bash
# 第一次 pull/push 时会要求输入凭据
# Username: abc-lee
# Password: YOUR_TOKEN (不是 GitHub 密码)

# 或者直接在 URL 中包含 token
git remote set-url origin https://YOUR_TOKEN@github.com/abc-lee/niu-agent.git
```

**方式 B：使用 GitHub CLI（推荐）**

```bash
# 安装 GitHub CLI
brew install gh

# 登录
gh auth login
# 选择 GitHub.com
# 选择 HTTPS
# 使用浏览器登录或粘贴 token

# Clone（会自动配置认证）
gh repo clone abc-lee/niu-agent
```

## 3. 下载大模型文件

由于大模型文件超过 GitHub 限制，需要单独下载：

### 方案 A：从 Windows 复制

```bash
# 在 Windows 上打包模型文件
cd E:\tools\ai-bot\models
tar -czf models.tar.gz models/

# 使用 U 盘、网盘或 AirDrop 传输到 Mac

# 在 Mac 上解压
cd ~/projects/niu-agent
tar -xzf models.tar.gz
```

### 方案 B：重新下载模型

```bash
# 安装依赖
pip install sentence-transformers insightface

# Python 会自动下载模型到以下位置：
# - all-MiniLM-L6-v2: ~/.cache/torch/sentence_transformers/
# - buffalo_l: ~/.insightface/models/

# 然后复制到项目目录
cp -r ~/.cache/torch/sentence_transformers/all-MiniLM-L6-v2 models/
cp -r ~/.insightface/models/buffalo_l models/models/
```

## 4. 安装依赖

```bash
# Python 依赖
pip install -e agent
pip install -e niu_api
pip install -e mcp-servers/photo-server
pip install -e mcp-servers/kg-server
pip install -e mcp-servers/vector-store
pip install -e mcp-servers/file-parser
pip install -e mcp-servers/config-manager
pip install -e mcp-servers/memory-server
pip install -e mcp-servers/session-manager
pip install -e mcp-servers/scheduler-server

# Node.js 依赖（前端）
cd ui/main
npm install
cd ../..
```

## 5. 跨平台测试

### 测试路径兼容性

```bash
# 运行测试
python -c "
import os
from pathlib import Path

# 测试路径处理
test_path = Path.home() / 'test'
print(f'Home: {Path.home()}')
print(f'Test path: {test_path}')
print(f'Exists: {test_path.exists()}')
"
```

### 测试启动

```bash
# 启动服务
python -m niu_api

# 在另一个终端启动前端
cd ui/main
npm start
```

## 6. 跨平台修改示例

### 修复路径问题

```python
# ❌ 错误（Windows 硬编码）
path = "E:\\tools\\ai-bot\\data"

# ✅ 正确（跨平台）
from pathlib import Path
path = Path(__file__).parent / "data"
# 或
path = Path.home() / "tools" / "ai-bot" / "data"
```

### 修复权限问题

```python
import os
import stat

# macOS/Linux 需要设置执行权限
if platform.system() != "Windows":
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
```

## 7. 提交跨平台修改

```bash
# 测试通过后，提交修改
git status
git add agent/runner.py  # 只添加跨平台修复的文件
git commit -m "fix(mac): 修复路径兼容性问题

- 使用 pathlib.Path 替代硬编码路径
- 修复文件权限问题
- 测试通过：macOS 14, Python 3.11"

git push origin mac-testing

# 在 GitHub 创建 Pull Request
gh pr create --title "fix(mac): 跨平台兼容性修复" --body "
## 修改内容
- 使用 pathlib.Path 替代硬编码路径
- 添加平台检测逻辑
- 修复文件权限问题

## 测试结果
- ✅ macOS 14 测试通过
- ✅ Python 3.11 兼容
- ✅ 不影响 Windows 功能
"
```

## 8. 日常同步流程

```bash
# 每天 Mac 开始工作时
git checkout mac-testing
git pull origin main  # 同步 Windows 最新代码
git pull origin mac-testing  # 同步 Mac 分支

# 测试和修改...

# 推送修改
git add -A
git commit -m "fix(mac): ..."
git push origin mac-testing

# 如果确定可以合并，创建 PR
gh pr create
```

## 9. 查看差异

```bash
# 查看 Mac 和 main 的差异
git diff main...mac-testing

# 查看所有提交
git log main..mac-testing --oneline
```

## 10. 常见问题

### Q1: Python 版本不同怎么办？

```bash
# 检查 Python 版本
python --version  # 确保 >= 3.11

# 使用 pyenv 管理多个版本
brew install pyenv
pyenv install 3.11.0
pyenv local 3.11.0
```

### Q2: 依赖安装失败？

```bash
# 某些依赖需要编译工具
xcode-select --install

# InsightFace 可能需要 OpenCV
brew install opencv
```

### Q3: 文件权限问题？

```bash
# macOS/Linux 文件权限
chmod +x scripts/*.py
chmod 644 config/*.json
```

### Q4: 如何撤销本地修改？

```bash
# 撤销所有未提交的修改
git checkout .
git clean -fd

# 重置到远程状态
git fetch origin
git reset --hard origin/mac-testing
```

## 11. 安全注意事项

⚠️ **重要**：不要在公开场合分享你的 GitHub Token！

如果 token 已暴露：
1. 访问 https://github.com/settings/tokens
2. 点击 "Delete" 删除暴露的 token
3. 点击 "Generate new token" 生成新 token
4. 选择权限：repo（私有仓库访问）
5. 保存到安全的地方（如密码管理器）

---

## Mac 端开发环境检查清单

- [ ] Clone 仓库成功
- [ ] 切换到 mac-testing 分支
- [ ] 下载大模型文件
- [ ] 安装 Python 依赖
- [ ] 安装 Node.js 依赖
- [ ] 测试路径兼容性
- [ ] 测试服务启动
- [ ] 创建第一个跨平台 PR

---

**Happy Coding on Mac! 🍎**
