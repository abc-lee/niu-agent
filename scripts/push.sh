#!/bin/bash
# push.sh —— 推送到远端，自动排除本地私有文档（docs/superpowers/plans/）
#
# 原理：plans 文档在主仓库正常提交（Agent 多轮审查需要主仓库 git 历史），
# 但推送时用 commit-tree 创建"树级排除 plans"的快照提交对象推送到远端，
# 本地 HEAD/工作区/索引完全不动。
#
# 用法：
#   ./scripts/push.sh [remote] [branch]
#   默认 remote=origin, branch=当前分支
#
# 注意：
# - 远端分支当前树不含 plans（GitHub 页面/文件列表不可见）
# - git 历史中仍可考古到 plans（祖先提交含）——完全无痕需一次性 filter-repo 重写历史（见 AGENTS.md）
# - 本脚本不改变本地提交历史，Agent 可继续正常 git add/commit plans
set -e

cd "$(git rev-parse --show-toplevel)"

REMOTE="${1:-origin}"
BRANCH="${2:-$(git branch --show-current)}"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
    echo "error: cannot determine current branch (detached HEAD?)" >&2
    exit 1
fi

# 确认 plans 目录存在且含文件（若已不存在则无需排除，直接普通 push 语义）
EXCLUDE="docs/superpowers/plans"
HAS_EXCLUDE=0
if git ls-files --error-unmatch "$EXCLUDE" >/dev/null 2>&1; then
    HAS_EXCLUDE=1
fi

if [ "$HAS_EXCLUDE" = "1" ]; then
    echo "[push.sh] creating clean snapshot excluding $EXCLUDE ..."
    # 重置索引到 HEAD（不碰工作区）
    git read-tree HEAD
    # 从索引移除 plans（工作区文件不动）
    git rm -r --cached --quiet "$EXCLUDE" 2>/dev/null || true
    # 写树 + 创建快照提交对象（不移动 HEAD）
    TREE=$(git write-tree)
    COMMIT=$(git commit-tree "$TREE" -p HEAD -m "push snapshot (local private docs excluded)")
    # 恢复索引到 HEAD
    git read-tree HEAD
    echo "[push.sh] pushing snapshot ${COMMIT:0:8} -> $REMOTE/$BRANCH (local HEAD $(git rev-parse --short HEAD) unchanged)"
    git push "$REMOTE" "$COMMIT:refs/heads/$BRANCH"
else
    echo "[push.sh] no excluded paths tracked, plain push"
    git push "$REMOTE" "$BRANCH"
fi
