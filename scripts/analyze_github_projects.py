#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 项目详细信息获取脚本
"""

import requests
import sys
from datetime import datetime

# 设置标准输出编码为 UTF-8
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def get_repo_info(owner: str, repo: str) -> dict:
    """获取仓库信息"""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def get_repo_readme(owner: str, repo: str) -> str:
    """获取 README 内容"""
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        # README 是 base64 编码的
        import base64

        content = base64.b64decode(data["content"]).decode("utf-8")
        return content
    except Exception as e:
        return f"Error: {e}"


def get_issue(owner: str, repo: str, issue_number: int) -> dict:
    """获取 Issue 详情"""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def get_pr(owner: str, repo: str, pr_number: int) -> dict:
    """获取 PR 详情"""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def analyze_page_agent_mcp():
    """分析 alibaba/page-agent 的 MCP 支持"""
    print("\n" + "=" * 80)
    print("📦 分析 alibaba/page-agent 的 MCP 支持")
    print("=" * 80)

    # 1. 仓库信息
    repo_info = get_repo_info("alibaba", "page-agent")
    if "error" not in repo_info:
        print(f"\n⭐ Stars: {repo_info.get('stargazers_count', 0)}")
        print(f"🍴 Forks: {repo_info.get('forks_count', 0)}")
        print(f"📝 描述: {repo_info.get('description', '无')}")

    # 2. Issue #297: MCP (beta) is here
    print("\n" + "-" * 80)
    print("🐛 Issue #297: MCP (beta) is here")
    print("-" * 80)
    issue = get_issue("alibaba", "page-agent", 297)
    if "error" not in issue:
        print(f"标题: {issue.get('title', '')}")
        print(f"状态: {issue.get('state', '')}")
        print(f"创建时间: {issue.get('created_at', '')}")
        print(f"评论数: {issue.get('comments', 0)}")
        print(f"\n描述:\n{issue.get('body', '无')[:1000]}")

    # 3. PR #283: feat: mcp (WIP)
    print("\n" + "-" * 80)
    print("🔧 PR #283: feat: mcp (WIP)")
    print("-" * 80)
    pr = get_pr("alibaba", "page-agent", 283)
    if "error" not in pr:
        print(f"标题: {pr.get('title', '')}")
        print(f"状态: {pr.get('state', '')}")
        print(f"创建时间: {pr.get('created_at', '')}")
        print(f"\n描述:\n{pr.get('body', '无')[:1000]}")


def analyze_playwright_mcp():
    """分析 microsoft/playwright-mcp 的架构"""
    print("\n" + "=" * 80)
    print("📦 分析 microsoft/playwright-mcp 的架构")
    print("=" * 80)

    # 1. 仓库信息
    repo_info = get_repo_info("microsoft", "playwright-mcp")
    if "error" not in repo_info:
        print(f"\n⭐ Stars: {repo_info.get('stargazers_count', 0)}")
        print(f"📝 描述: {repo_info.get('description', '无')}")

    # 2. README
    print("\n" + "-" * 80)
    print("📄 README (前 2000 字符)")
    print("-" * 80)
    readme = get_repo_readme("microsoft", "playwright-mcp")
    if not readme.startswith("Error:"):
        print(readme[:2000])
    else:
        print(readme)


def analyze_browser_use():
    """分析 browser-use/browser-use 的工具调用机制"""
    print("\n" + "=" * 80)
    print("📦 分析 browser-use/browser-use 的工具调用机制")
    print("=" * 80)

    # 1. 仓库信息
    repo_info = get_repo_info("browser-use", "browser-use")
    if "error" not in repo_info:
        print(f"\n⭐ Stars: {repo_info.get('stargazers_count', 0)}")
        print(f"📝 描述: {repo_info.get('description', '无')}")

    # 2. README
    print("\n" + "-" * 80)
    print("📄 README (前 2000 字符)")
    print("-" * 80)
    readme = get_repo_readme("browser-use", "browser-use")
    if not readme.startswith("Error:"):
        print(readme[:2000])
    else:
        print(readme)


def main():
    """主函数"""
    print("🔍 GitHub 项目详细分析")
    print(f"📅 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 分析 page-agent MCP 支持
    analyze_page_agent_mcp()

    # 2. 分析 playwright-mcp
    analyze_playwright_mcp()

    # 3. 分析 browser-use
    analyze_browser_use()

    print("\n" + "=" * 80)
    print("✅ 分析完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
