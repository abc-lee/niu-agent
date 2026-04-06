"""
Web Operations - Simple web helpers with SSRF protection

For complex web interactions, use code_run with selenium/playwright.
"""

import re
import ipaddress
from urllib.parse import urlparse
from typing import Dict, Any, List
import httpx
from loguru import logger

# 内网 IP 段
PRIVATE_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # Class A private
    ipaddress.ip_network("172.16.0.0/12"),  # Class B private
    ipaddress.ip_network("192.168.0.0/16"),  # Class C private
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local
    ipaddress.ip_network("0.0.0.0/8"),  # Current network
]

# 允许的协议
ALLOWED_SCHEMES = ["http", "https"]


def is_private_ip(ip_str: str) -> bool:
    """检查是否为内网 IP"""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in PRIVATE_IP_RANGES:
            if ip in network:
                return True
        return False
    except ValueError:
        return False


def validate_url_for_ssrf(url: str) -> tuple[bool, str]:
    """
    验证 URL 是否安全（SSRF 防护）

    Returns:
        (is_safe, error_message)
    """
    if not url:
        return False, "URL 不能为空"

    try:
        parsed = urlparse(url)

        # 检查协议
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return False, f"不允许的协议: {parsed.scheme}。只允许 http/https"

        # 检查主机名
        hostname = parsed.hostname
        if not hostname:
            return False, "URL 缺少主机名"

        # 检查是否为 IP 地址
        try:
            ip = ipaddress.ip_address(hostname)
            if is_private_ip(str(ip)):
                return False, f"禁止访问内网 IP: {hostname}"
        except ValueError:
            # 不是 IP，是域名
            pass

        # 检查危险域名
        dangerous_hostnames = ["localhost", "localhost.localdomain", "ip6-localhost"]
        if hostname.lower() in dangerous_hostnames:
            return False, f"禁止访问: {hostname}"

        return True, ""

    except Exception as e:
        return False, f"URL 解析错误: {str(e)}"


async def web_fetch(
    url: str,
    method: str = "GET",
    headers: Dict[str, str] = None,
    body: str = None,
    timeout: int = 30,
    skip_ssrf_check: bool = False,
) -> Dict[str, Any]:
    """
    Fetch URL content with SSRF protection

    Args:
        url: URL to fetch
        method: HTTP method
        headers: Request headers
        body: Request body
        timeout: Timeout in seconds
        skip_ssrf_check: Skip SSRF check (dangerous!)

    Returns:
        {'status': 'success'|'error'|'blocked', 'content': str, 'status_code': int}
    """
    # SSRF 检查
    if not skip_ssrf_check:
        is_safe, error_msg = validate_url_for_ssrf(url)
        if not is_safe:
            logger.warning(f"SSRF blocked: {url} - {error_msg}")
            return {"status": "blocked", "msg": f"SSRF 防护: {error_msg}"}

    logger.info(f"web_fetch: {method} {url}")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method=method, url=url, headers=headers or {}, content=body
            )

            # 截断过长的响应
            content = response.text
            if len(content) > 50000:
                content = content[:25000] + "\n\n[...truncated...]\n\n" + content[-25000:]

            return {
                "status": "success",
                "content": content,
                "status_code": response.status_code,
                "headers": dict(response.headers),
            }

    except httpx.TimeoutException:
        logger.error(f"web_fetch timeout: {url}")
        return {"status": "error", "msg": "请求超时"}
    except Exception as e:
        logger.error(f"web_fetch error: {e}")
        return {"status": "error", "msg": str(e)}


async def web_search(query: str, num_results: int = 5) -> Dict[str, Any]:
    """
    Simple web search using DuckDuckGo (no API key needed)

    Args:
        query: Search query
        num_results: Number of results

    Returns:
        {'status': 'success'|'error', 'results': list}
    """
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)

            results = []
            pattern = r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(pattern, response.text)

            for i, (link, title) in enumerate(matches[:num_results]):
                results.append({"title": title.strip(), "url": link})

            return {"status": "success", "results": results}

    except Exception as e:
        logger.error(f"web_search error: {e}")
        return {"status": "error", "msg": str(e)}
