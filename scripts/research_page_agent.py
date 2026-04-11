#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub API 搜索脚本 - 调研 page-agent 开源生态
"""

import requests
import sys
from datetime import datetime
from typing import Any

# 设置标准输出编码为 UTF-8
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

GITHUB_API = "https://api.github.com"


def github_search(query: str, search_type: str = "repositories") -> dict[str, Any]:
    """
    GitHub API 搜索

    Args:
        query: 搜索关键词
        search_type: 搜索类型 (repositories/issues/code)
    """
    url = f"{GITHUB_API}/search/{search_type}"
    params = {"q": query, "per_page": 30}

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def search_repos(keywords: list[str]) -> None:
    """搜索仓库"""
    print("\n" + "=" * 80)
    print("📦 GitHub 仓库搜索")
    print("=" * 80)

    for keyword in keywords:
        print(f"\n🔍 搜索关键词: {keyword}")
        result = github_search(keyword, "repositories")

        if "error" in result:
            print(f"❌ 搜索失败: {result['error']}")
            continue

        items = result.get("items", [])
        if not items:
            print("  无结果")
            continue

        for item in items[:10]:  # 只显示前 10 个
            full_name = item.get("full_name", "")
            description = item.get("description", "") or "无描述"
            stars = item.get("stargazers_count", 0)
            forks = item.get("fork_count", 0)
            url = item.get("html_url", "")

            print(f"\n  📁 {full_name}")
            print(f"     ⭐ {stars} | 🍴 {forks}")
            print(f"     {description[:100]}")
            print(f"     {url}")


def search_issues(keywords: list[str]) -> None:
    """搜索 Issues 和 PRs"""
    print("\n" + "=" * 80)
    print("🐛 GitHub Issues/PRs 搜索")
    print("=" * 80)

    for keyword in keywords:
        print(f"\n🔍 搜索关键词: {keyword}")
        result = github_search(keyword, "issues")

        if "error" in result:
            print(f"❌ 搜索失败: {result['error']}")
            continue

        items = result.get("items", [])
        if not items:
            print("  无结果")
            continue

        for item in items[:10]:  # 只显示前 10 个
            title = item.get("title", "")
            repo = item.get("repository", {}).get("full_name", "")
            state = item.get("state", "")
            url = item.get("html_url", "")
            created = item.get("created_at", "")

            print(f"\n  🐛 {title[:80]}")
            print(f"     仓库: {repo} | 状态: {state}")
            print(f"     创建时间: {created}")
            print(f"     {url}")


def search_code(keywords: list[str]) -> None:
    """搜索代码"""
    print("\n" + "=" * 80)
    print("💻 GitHub 代码搜索")
    print("=" * 80)

    for keyword in keywords:
        print(f"\n🔍 搜索关键词: {keyword}")
        result = github_search(keyword, "code")

        if "error" in result:
            print(f"❌ 搜索失败: {result['error']}")
            continue

        items = result.get("items", [])
        if not items:
            print("  无结果")
            continue

        for item in items[:10]:  # 只显示前 10 个
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            url = item.get("html_url", "")

            print(f"\n  📄 {path}")
            print(f"     仓库: {repo}")
            print(f"     {url}")


def main():
    """主函数"""
    print("🔍 Page-Agent 开源生态调研")
    print(f"📅 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 搜索仓库
    repo_keywords = [
        "page-agent",
        "page-agent customTools",
        "browser automation MCP",
        "AI browser agent",
        "playwright MCP server",
        "puppeteer MCP",
    ]
    search_repos(repo_keywords)

    # 2. 搜索 Issues/PRs
    issue_keywords = [
        "page-agent customTools",
        "alibaba/page-agent MCP",
        "browser automation tool calling",
        "page-agent fork",
    ]
    search_issues(issue_keywords)

    # 3. 搜索代码
    code_keywords = [
        "customTools filename:package.json",
        "page-agent MCP filename:*.py",
        "browser_tool_call filename:*.py",
    ]
    search_code(code_keywords)

    print("\n" + "=" * 80)
    print("✅ 调研完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
