"""
HTTP 日志拦截器 -- SDK 层面的原始 HTTP 请求/响应记录

拦截两条路径：
1. OpenAI SDK 路径：通过设置 litellm.client_session，让 OpenAI SDK 使用带日志的 httpx.Client
2. HTTPHandler 路径：通过 patch HTTPHandler.post()，拦截 DeepSeek/MiniMax 等其他 provider

日志目录：logs/raw_http/{YYYYMMDD}/{seq:06d}.json
"""

import json as _json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx

# ---------------------------------------------------------------------------
# 线程安全的状态
# ---------------------------------------------------------------------------
_write_lock = threading.Lock()
_seq_counter = 0


def _get_log_dir() -> Path:
    """返回日志目录 logs/raw_http/{YYYYMMDD}/，自动创建。"""
    date_str = datetime.now().strftime("%Y%m%d")
    log_dir = Path("logs") / "raw_http" / date_str
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _next_seq() -> int:
    """线程安全递增序号。"""
    global _seq_counter
    with _write_lock:
        _seq_counter += 1
        return _seq_counter


def _mask_api_key(value: str) -> str:
    """脱敏 API Key：sk-abc...xyz -> sk-ab...yz"""
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-3:]


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """脱敏敏感 header（authorization, api-key 等）。"""
    sensitive_keys = {"authorization", "api-key", "x-api-key", "cookie"}
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in sensitive_keys:
            sanitized[k] = _mask_api_key(str(v))
        else:
            sanitized[k] = v
    return sanitized


def _decode_body(body_bytes: bytes) -> Any:
    """尝试 JSON decode，失败返回原始字符串。"""
    if not body_bytes:
        return None
    try:
        return _json.loads(body_bytes)
    except Exception:
        try:
            return body_bytes.decode("utf-8", errors="replace")
        except Exception:
            return f"<binary {len(body_bytes)} bytes>"


def _is_streaming_response(response: httpx.Response) -> bool:
    """检测 SSE 流式响应。"""
    content_type = response.headers.get("content-type", "")
    return "text/event-stream" in content_type


def _read_streaming_body(response: httpx.Response) -> Any:
    """读取 SSE 流式响应内容（仅读取前 N 字节作为采样）。"""
    max_bytes = 4096
    try:
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="replace")
        if total >= max_bytes:
            text += f"\n... <truncated, total >= {max_bytes} bytes>"
        return text
    except Exception as exc:
        return f"<streaming read error: {exc}>"


def _write_log_entry(seq: int, entry: dict) -> None:
    """写入 JSON 日志文件。"""
    log_dir = _get_log_dir()
    filepath = log_dir / f"{seq:06d}.json"
    with _write_lock:
        filepath.write_text(
            _json.dumps(entry, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# LoggingTransport -- 组合模式包装 httpx.HTTPTransport
# ---------------------------------------------------------------------------


class LoggingTransport(httpx.BaseTransport):
    """组合模式：包装 httpx.HTTPTransport，在 handle_request 中记录日志。"""

    def __init__(self, real_transport: Optional[httpx.HTTPTransport] = None, **kwargs: Any) -> None:
        if real_transport is not None:
            self._transport = real_transport
        else:
            self._transport = httpx.HTTPTransport(**kwargs)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        start_time = datetime.now(timezone.utc)
        seq = _next_seq()

        # 调用底层 transport
        response = self._transport.handle_request(request)

        # 记录日志（不修改任何数据）
        try:
            elapsed_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

            # 读取 request body
            req_body = _decode_body(request.content) if request.content else None

            # 读取 response body
            if _is_streaming_response(response):
                resp_body = _read_streaming_body(response)
            else:
                resp_body = _decode_body(response.content)

            log_entry = {
                "seq": seq,
                "timestamp": start_time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start_time.microsecond // 1000:03d}",
                "elapsed_ms": elapsed_ms,
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _sanitize_headers(dict(request.headers)),
                    "body": req_body,
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": resp_body,
                },
            }
            _write_log_entry(seq, log_entry)
        except Exception as exc:
            print(f"[http_logger] failed to write log: {exc}", file=sys.stderr)

        return response

    def __getattr__(self, name: str) -> Any:
        """代理其他属性到底层 transport。"""
        return getattr(self._transport, name)

    def close(self) -> None:
        """关闭底层 transport。"""
        self._transport.close()


# ---------------------------------------------------------------------------
# install_http_logger() -- 安装拦截器
# ---------------------------------------------------------------------------


def install_http_logger() -> None:
    """
    安装 HTTP 日志拦截器，拦截两条路径：

    1. OpenAI SDK 路径：设置 litellm.client_session
    2. HTTPHandler 路径：patch HTTPHandler.post()
    """
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler, get_ssl_configuration

    # ---- 步骤 1：设置 litellm.client_session ----
    ssl_config = get_ssl_configuration()
    transport = LoggingTransport()
    litellm.client_session = httpx.Client(
        transport=transport,
        verify=ssl_config,
        follow_redirects=True,
    )

    # ---- 步骤 2：patch HTTPHandler.post() ----
    original_post = HTTPHandler.post

    def patched_post(
        self,
        url: str,
        data: Union[dict, str, bytes, None] = None,
        json: Union[dict, str, List, None] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        stream: bool = False,
        timeout: Union[float, Any, None] = None,
        files: Any = None,
        content: Any = None,
        logging_obj: Optional[Any] = None,
    ) -> httpx.Response:
        start_time = datetime.now(timezone.utc)
        seq = _next_seq()

        # 调用原始 post（可能抛异常）
        try:
            response = original_post(
                self,
                url,
                data=data,
                json=json,
                params=params,
                headers=headers,
                stream=stream,
                timeout=timeout,
                files=files,
                content=content,
                logging_obj=logging_obj,
            )
        except Exception as exc:
            # 异常时也记录日志
            try:
                elapsed_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
                # 从传入参数推断 request body
                req_body = json if json is not None else data
                if isinstance(req_body, str):
                    try:
                        req_body = _json.loads(req_body)
                    except Exception:
                        pass
                elif isinstance(req_body, bytes):
                    req_body = _decode_body(req_body)

                log_entry = {
                    "seq": seq,
                    "timestamp": start_time.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{start_time.microsecond // 1000:03d}",
                    "elapsed_ms": elapsed_ms,
                    "request": {
                        "method": "POST",
                        "url": url,
                        "headers": _sanitize_headers(headers or {}),
                        "body": req_body,
                    },
                    "response": {
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                }
                _write_log_entry(seq, log_entry)
            except Exception as log_exc:
                print(f"[http_logger] failed to write error log: {log_exc}", file=sys.stderr)
            # 重新抛出原始异常，不吞掉
            raise

        # 正常响应，记录日志
        try:
            elapsed_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)

            # 从传入参数推断 request body
            req_body = json if json is not None else data
            if isinstance(req_body, str):
                try:
                    req_body = _json.loads(req_body)
                except Exception:
                    pass
            elif isinstance(req_body, bytes):
                req_body = _decode_body(req_body)

            # 读取 response body
            if _is_streaming_response(response):
                resp_body = _read_streaming_body(response)
            else:
                resp_body = _decode_body(response.content)

            log_entry = {
                "seq": seq,
                "timestamp": start_time.strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{start_time.microsecond // 1000:03d}",
                "elapsed_ms": elapsed_ms,
                "request": {
                    "method": "POST",
                    "url": url,
                    "headers": _sanitize_headers(headers or {}),
                    "body": req_body,
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": resp_body,
                },
            }
            _write_log_entry(seq, log_entry)
        except Exception as exc:
            print(f"[http_logger] failed to write log: {exc}", file=sys.stderr)

        return response

    HTTPHandler.post = patched_post  # type: ignore[assignment]
