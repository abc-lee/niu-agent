"""
照片 KG 结构化注入方案测试
========================

验证照片知识图谱的结构化注入（inject_custom_kg）方案，对比旧方案（insert）。

架构：双 Agent（Ingestion + Review）
- Ingestion Agent：模拟 sync_photo_to_kg / name_person / merge_persons
- Review Agent：验证图谱结构和一致性

执行方式：
    python scripts/test_photo_kg_structured.py              # 完整测试
    python scripts/test_photo_kg_structured.py --new-only   # 仅新方案
    python scripts/test_photo_kg_structured.py --compare    # 对比测试
    python scripts/test_photo_kg_structured.py --cleanup    # 清理测试数据
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error

# ── sys.path 设置 ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "agent"))

# ── 日志配置 ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("photo_kg_test")

# ── 常量 ───────────────────────────────────────────────────────
TEST_WORKSPACE = Path.home() / ".niu" / "lightrag_test"
ROOT_ENTITY = "brain:Niu"
API_PORT = 9876
API_BASE_URL = f"http://localhost:{API_PORT}"
API_HEALTH_URL = f"{API_BASE_URL}/health"
LLM_HEALTH_URL = f"{API_BASE_URL}/llm/v1/health"


# ══════════════════════════════════════════════════════════════
#  API 服务器管理
# ══════════════════════════════════════════════════════════════

class APIServerManager:
    """管理 Niu API 服务器的启动和关闭。

    测试需要 LLM 代理（/llm/v1/chat/completions），
    因为代理过程中会注入脑区提示词（brain_region_prompt 静态+动态提示词），
    不走代理就测不出真实效果。
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._was_already_running = False

    def is_running(self) -> bool:
        """检查 API 服务器是否已在运行。"""
        try:
            resp = urllib.request.urlopen(API_HEALTH_URL, timeout=3)
            return resp.status == 200
        except Exception:
            return False

    def is_llm_ready(self) -> bool:
        """检查 LLM 代理是否就绪。"""
        try:
            resp = urllib.request.urlopen(LLM_HEALTH_URL, timeout=3)
            data = json.loads(resp.read())
            return data.get("status") == "ok"
        except Exception:
            return False

    def start(self, timeout: int = 120) -> bool:
        """启动 API 服务器。如果已在运行则跳过。

        Returns:
            True 如果服务器就绪，False 如果启动失败。
        """
        if self.is_running():
            self._was_already_running = True
            logger.info("API 服务器已在运行 (port %d)", API_PORT)
            if self.is_llm_ready():
                logger.info("LLM 代理就绪")
                return True
            else:
                logger.error("API 服务器运行中但 LLM 代理不可用 — 检查 config/user-config.json")
                return False

        logger.info("启动 API 服务器 (python -m niu_api)...")
        env = {**os.environ, "NIU_API_PORT": str(API_PORT)}
        # 输出重定向到文件，避免 PIPE 缓冲区满导致进程阻塞
        log_path = Path.home() / ".niu" / "test_api_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w", encoding="utf-8")
        self._process = subprocess.Popen(
            [sys.executable, "-m", "niu_api"],
            cwd=str(_PROJECT_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
        logger.info("API 服务器 PID: %d, 日志: %s", self._process.pid, log_path)

        # 等待服务器就绪
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                # 进程已退出
                rc = self._process.returncode
                log_path = Path.home() / ".niu" / "test_api_server.log"
                output = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""
                logger.error("API 服务器意外退出 (rc=%d):\n%s", rc, output)
                return False
            if self.is_running():
                logger.info("API 服务器已启动")
                # 再等 LLM 代理就绪
                llm_deadline = time.monotonic() + 30
                while time.monotonic() < llm_deadline:
                    if self.is_llm_ready():
                        logger.info("LLM 代理就绪")
                        return True
                    time.sleep(2)
                logger.warning("API 服务器已启动但 LLM 代理未就绪（可能 API Key 未配置）")
                return True
            time.sleep(2)

        logger.error("API 服务器启动超时 (%ds)", timeout)
        self.stop()
        return False

    def stop(self) -> None:
        """关闭 API 服务器（仅当我们启动的）。"""
        if self._was_already_running:
            logger.info("API 服务器是之前已运行的，不关闭")
            return
        if self._process is not None and self._process.poll() is None:
            logger.info("关闭 API 服务器 (PID %d)...", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("API 服务器未响应 terminate，强制 kill")
                self._process.kill()
                self._process.wait(timeout=5)
            logger.info("API 服务器已关闭")
            self._process = None


_api_server = APIServerManager()


# ── 模拟数据 ───────────────────────────────────────────────────
MOCK_PHOTOS = [
    {
        "file_path": "/photos/2024/beach/sunset_01.jpg",
        "abstract": "海滩日落，三人在海边合影",
        "persons": [
            {"person_id": "p001", "person_name": "小明"},
            {"person_id": "p002", "person_name": "小红"},
            {"person_id": "p003", "person_name": "未命名人物"},
        ],
    },
    {
        "file_path": "/photos/2024/city/cafe_02.jpg",
        "abstract": "咖啡馆里的单人照",
        "persons": [{"person_id": "p001", "person_name": "小明"}],
    },
    {
        "file_path": "/photos/2024/park/walk_03.jpg",
        "abstract": "公园散步的两人",
        "persons": [
            {"person_id": "p004", "person_name": "老王"},
            {"person_id": "p005", "person_name": "未命名人物"},
        ],
    },
]

MOCK_CHAT_MESSAGES = [
    {"role": "user", "content": "我今天去了海滩，和小明、小红还有一个人一起看了日落"},
    {"role": "assistant", "content": "听起来很开心！海滩日落一定很美。你说的另一个人是谁呢？"},
    {"role": "user", "content": "那是小刚，我们大学同学"},
    {"role": "assistant", "content": "好的，我记住了小刚是你的大学同学"},
]

MERGE_SCENARIO = {
    "source_entities": ["person:p006"],
    "target_entity": "person:p003",
    "target_name": "小刚",
    "description_after_merge": "小刚，大学同学",
}


# ══════════════════════════════════════════════════════════════
#  适配器单例：将类方法暴露为顶级函数
# ══════════════════════════════════════════════════════════════

class _AdapterProxy:
    """延迟初始化的适配器代理，将 LightRAGAdapter/LightRAGIngester
    类方法暴露为顶级函数式接口。

    注意：LightRAGAdapter/LightRAGIngester 无构造函数参数，
    内部通过 _get_rag() -> get_lightrag() 获取单例。
    """

    def __init__(self) -> None:
        self._adapter = None
        self._ingester = None

    def _ensure(self) -> None:
        if self._adapter is not None:
            return
        from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
        self._adapter = LightRAGAdapter()
        self._ingester = LightRAGIngester()

    # ── Ingester 方法 ──

    def inject_custom_kg(self, **kwargs: Any) -> dict:
        self._ensure()
        return self._ingester.inject_custom_kg(**kwargs)

    def insert(self, content: str, source_id: str = "") -> dict:
        """旧方案入口：调用 lightrag_insert (LLM 自动提取)。"""
        self._ensure()
        kwargs: dict[str, Any] = {}
        if source_id:
            kwargs["file_paths"] = source_id
        return self._ingester.lightrag_insert(content=content, **kwargs)

    def insert_entity(self, entity_name: str, entity_type: str,
                      description: str) -> dict:
        """插入单个实体，通过 inject_custom_kg 实现。"""
        return self.inject_custom_kg(
            entities=[{"entity_name": entity_name,
                       "entity_type": entity_type,
                       "description": description}],
            relationships=[], chunks=[],
            source_id=entity_name,
        )

    def insert_relation(self, src_id: str, tgt_id: str,
                        keywords: str, description: str) -> dict:
        """插入单个关系，通过 inject_custom_kg 实现。"""
        return self.inject_custom_kg(
            entities=[], relationships=[{"src_id": src_id, "tgt_id": tgt_id,
                                         "keywords": keywords,
                                         "description": description}],
            chunks=[], source_id=f"{src_id}-{tgt_id}",
        )

    # ── Adapter 方法 ──

    def query(self, query_str: str, mode: str = "hybrid") -> str | None:
        self._ensure()
        return self._adapter.query(query=query_str, mode=mode)

    def explore_node(self, entity_name: str, depth: int = 1) -> dict:
        self._ensure()
        return self._adapter.explore_node(entity_name=entity_name, depth=depth)

    def list_entities(self, entity_type: str | None = None,
                      limit: int = 100) -> list[dict]:
        """list_entities 返回格式: {"status": "ok", "data": [...]}。
        此方法提取 data 列表并统一实体键名。"""
        self._ensure()
        if entity_type:
            result = self._adapter.list_entities(
                list_type="entities", entity_type=entity_type, limit=limit)
        else:
            result = self._adapter.list_entities(
                list_type="entities", limit=limit)
        if not result or result.get("status") != "ok":
            return []
        data = result.get("data", [])
        # 统一键名：LightRAG 返回 "id"，规范为 "entity_name"
        for item in data:
            if "entity_name" not in item and "id" in item:
                item["entity_name"] = item["id"]
        return data

    def merge_entities(self, source_entities: list[str],
                       target_entity: str) -> dict:
        self._ensure()
        return self._adapter.merge_entities(
            source_entities=source_entities, target_entity=target_entity,
        )


_proxy = _AdapterProxy()


# ══════════════════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════════════════

def _banner(stage: int, title: str) -> None:
    logger.info("=" * 60)
    logger.info("  阶段 %d: %s", stage, title)
    logger.info("=" * 60)


def _stage_done(stage: int) -> None:
    logger.info("-" * 60)
    logger.info("  阶段 %d 完成", stage)
    logger.info("-" * 60)


def _pk(pid: str) -> str:
    return f"person:{pid}"


def _phk(fp: str) -> str:
    return f"photo:{fp}"


def _build_photo_injection(photo: dict, include_co_occurs: bool = False) -> dict:
    """构建单张照片的 inject_custom_kg 参数。"""
    fp = photo["file_path"]
    abstract = photo["abstract"]
    photo_key = _phk(fp)
    entities: list[dict] = [
        {"entity_name": photo_key, "entity_type": "Photo",
         "description": abstract, "file_path": fp},
    ]
    relationships: list[dict] = [
        {"src_id": ROOT_ENTITY, "tgt_id": photo_key,
         "keywords": "remembers", "description": "拥有这张照片"},
    ]
    chunks: list[dict] = [
        {"content": f"照片 {Path(fp).stem}: {abstract}",
         "source_id": photo_key, "file_path": fp},
    ]
    person_keys: list[str] = []
    for p in photo["persons"]:
        pk = _pk(p["person_id"])
        person_keys.append(pk)
        entities.append({"entity_name": pk, "entity_type": "Person",
                         "description": p["person_name"]})
        relationships.append({"src_id": photo_key, "tgt_id": pk,
                              "keywords": "features",
                              "description": f"照片中出现了{p['person_name']}"})
        relationships.append({"src_id": ROOT_ENTITY, "tgt_id": pk,
                              "keywords": "remembers",
                              "description": f"认识{p['person_name']}"})
    if include_co_occurs:
        for i in range(len(person_keys)):
            for j in range(i + 1, len(person_keys)):
                na = photo["persons"][i]["person_name"]
                nb = photo["persons"][j]["person_name"]
                for src, tgt, desc in [
                    (person_keys[i], person_keys[j], f"{na}和{nb}同框出现"),
                    (person_keys[j], person_keys[i], f"{nb}和{na}同框出现"),
                ]:
                    relationships.append({"src_id": src, "tgt_id": tgt,
                                          "keywords": "co_occurs_with",
                                          "description": desc})
    return {"entities": entities, "relationships": relationships,
            "chunks": chunks, "source_id": photo_key,
            "person_keys": person_keys}


def _inject_params(params: dict) -> dict:
    """从 _build_photo_injection 返回值中提取 inject_custom_kg 可接受的参数。"""
    return {k: v for k, v in params.items()
            if k in ("entities", "relationships", "chunks", "source_id")}


# ══════════════════════════════════════════════════════════════
#  阶段 1：环境初始化
# ══════════════════════════════════════════════════════════════

async def stage_1_init() -> bool:
    _banner(1, "环境初始化")
    try:
        import niu_api.internal.lightrag_manager as _lm
        from niu_api.internal.lightrag_manager import ensure_lightrag, get_lightrag
    except ImportError as exc:
        logger.error("导入失败: %s — 请确认 niu_api/internal/ 模块存在", exc)
        return False

    ws_path = Path(TEST_WORKSPACE)
    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)
    ws_path.mkdir(parents=True, exist_ok=True)
    logger.info("测试工作目录: %s", ws_path)

    # 修补 STORAGE_DIR 让 _create_lightrag_instance 使用测试目录
    original_dir = getattr(_lm, "STORAGE_DIR", None)
    _lm.STORAGE_DIR = TEST_WORKSPACE
    logger.info("修补 STORAGE_DIR: %s -> %s", original_dir, TEST_WORKSPACE)

    # 重置单例，确保用新的 STORAGE_DIR 重新创建
    _lm._rag_instance = None
    logger.info("已重置 LightRAG 单例")

    logger.info("初始化 LightRAG...")
    await ensure_lightrag()
    lightrag = get_lightrag()
    if lightrag is None:
        logger.error("LightRAG 初始化失败")
        return False
    logger.info("LightRAG 初始化成功: %s", type(lightrag).__name__)

    result = _proxy.inject_custom_kg(
        entities=[{"entity_name": ROOT_ENTITY, "entity_type": "Brain",
                   "description": "Niu 知识图谱根节点"}],
        relationships=[], chunks=[], source_id="system:init",
    )
    logger.info("根节点注入: %s", result)
    _stage_done(1)
    return True


# ══════════════════════════════════════════════════════════════
#  阶段 2：照片入库（单照单人）
# ══════════════════════════════════════════════════════════════

async def stage_2_photo_ingest() -> dict[str, Any]:
    _banner(2, "照片入库（Ingestion Agent）")
    photo = MOCK_PHOTOS[1]  # 咖啡馆单人照
    logger.info("入库单照: %s", photo["file_path"])
    params = _build_photo_injection(photo)
    result = _proxy.inject_custom_kg(**_inject_params(params))
    logger.info("结果: %s", result)
    _stage_done(2)
    return {"photo": photo["file_path"], "result": result}


# ══════════════════════════════════════════════════════════════
#  阶段 3：多人同框
# ══════════════════════════════════════════════════════════════

async def stage_3_multi_person() -> dict[str, Any]:
    _banner(3, "多人同框")
    photo = MOCK_PHOTOS[0]  # 海滩三人照
    logger.info("入库多人照: %s (%d人)", photo["file_path"], len(photo["persons"]))
    params = _build_photo_injection(photo, include_co_occurs=True)
    result = _proxy.inject_custom_kg(**_inject_params(params))
    logger.info("结果: %s", result)
    _stage_done(3)
    return {"person_keys": params["person_keys"], "result": result}


# ══════════════════════════════════════════════════════════════
#  阶段 4：人物命名
# ══════════════════════════════════════════════════════════════

async def stage_4_name_person() -> dict[str, Any]:
    _banner(4, "人物命名")
    person_key = _pk("p003")
    real_name = "小刚"
    logger.info("命名: %s -> %s", person_key, real_name)
    result = _proxy.inject_custom_kg(
        entities=[{"entity_name": person_key, "entity_type": "Person",
                   "description": real_name}],
        relationships=[{"src_id": ROOT_ENTITY, "tgt_id": person_key,
                        "keywords": "remembers",
                        "description": f"认识{real_name}"}],
        chunks=[], source_id=person_key,
    )
    logger.info("结果: %s", result)
    _stage_done(4)
    return {"person_key": person_key, "real_name": real_name, "result": result}


# ══════════════════════════════════════════════════════════════
#  阶段 5：人物合并
# ══════════════════════════════════════════════════════════════

async def stage_5_merge_person() -> dict[str, Any]:
    _banner(5, "人物合并")
    scenario = MERGE_SCENARIO

    # 预注入待合并实体 person:p006 和第三张照片
    logger.info("预注入: person:p006 + 第三张照片")
    _proxy.inject_custom_kg(
        entities=[{"entity_name": "person:p006", "entity_type": "Person",
                   "description": "小李"}],
        relationships=[
            {"src_id": ROOT_ENTITY, "tgt_id": "person:p006",
             "keywords": "remembers", "description": "认识小李"},
            {"src_id": _phk(MOCK_PHOTOS[2]["file_path"]),
             "tgt_id": "person:p006", "keywords": "features",
             "description": "照片中出现了小李"},
        ],
        chunks=[], source_id="person:p006",
    )
    park_params = _build_photo_injection(MOCK_PHOTOS[2])
    _proxy.inject_custom_kg(**_inject_params(park_params))

    # 步骤1：更新目标实体描述
    logger.info("步骤1: 更新描述 -> '%s'", scenario["description_after_merge"])
    update_result = _proxy.inject_custom_kg(
        entities=[{"entity_name": scenario["target_entity"],
                   "entity_type": "Person",
                   "description": scenario["description_after_merge"]}],
        relationships=[], chunks=[], source_id=scenario["target_entity"],
    )

    # 步骤2：合并实体（迁移边 + 删除旧实体）
    logger.info("步骤2: 合并 %s -> %s",
                scenario["source_entities"], scenario["target_entity"])
    merge_result = _proxy.merge_entities(
        source_entities=scenario["source_entities"],
        target_entity=scenario["target_entity"],
    )
    logger.info("更新: %s, 合并: %s", update_result, merge_result)
    _stage_done(5)
    return {"update_result": update_result, "merge_result": merge_result}


# ══════════════════════════════════════════════════════════════
#  阶段 6：真实聊天记录内容提取入库
# ══════════════════════════════════════════════════════════════

async def stage_6_chat_extract() -> dict[str, Any]:
    _banner(6, "真实聊天记录内容提取入库")
    messages: list[dict] = []

    # 尝试获取真实聊天记录
    try:
        # 尝试从 session_manager MCP 服务器获取
        from niu_api.internal.session_manager import get_recent_messages
        raw = get_recent_messages(session_id="latest", limit=10)
        messages = [{"role": m.get("role", ""), "content": m.get("content", "")}
                    for m in raw if m.get("content")]
        logger.info("获取到 %d 条真实聊天记录", len(messages))
    except ImportError:
        try:
            # 尝试从 session 模块获取
            from niu_api.session import get_recent_messages
            raw = get_recent_messages(session_id="latest", limit=10)
            messages = [{"role": m.get("role", ""), "content": m.get("content", "")}
                        for m in raw if m.get("content")]
            logger.info("获取到 %d 条真实聊天记录", len(messages))
        except Exception:
            messages = []
    except Exception as exc:
        logger.warning("session 读取失败 (%s)，使用模拟数据", exc)
        messages = []

    if not messages:
        messages = MOCK_CHAT_MESSAGES
        logger.info("使用 %d 条模拟聊天记录", len(messages))

    # entity-extractor：格式化精炼文档
    chat_doc = "聊天记录摘要:\n"
    for msg in messages:
        label = "用户" if msg["role"] == "user" else "助手"
        chat_doc += f"- {label}: {msg['content']}\n"
    chat_doc += "\n关键词: 海滩, 日落, 小明, 小红, 小刚, 大学同学"

    logger.info("调用 insert() 触发 LLM 自动提取...")
    insert_result = _proxy.insert(content=chat_doc, source_id="chat:session_test_001")
    logger.info("insert: %s", insert_result)

    # dream-evolver：精加工
    try:
        _proxy.insert_entity("event:beach_sunset", "Event", "海滩日落聚会")
        _proxy.insert_relation("event:beach_sunset", _pk("p001"),
                               "participated", "小明参加了海滩日落聚会")
        _proxy.insert_relation(_pk("p003"), _pk("p001"),
                               "classmate", "小刚和小明是大学同学")
        logger.info("dream-evolver 精加工完成")
    except Exception as exc:
        logger.warning("精加工失败: %s", exc)

    _stage_done(6)
    return {"message_count": len(messages), "insert_result": insert_result}


# ══════════════════════════════════════════════════════════════
#  阶段 7：图谱审查（Review Agent）
# ══════════════════════════════════════════════════════════════

async def stage_7_review() -> dict[str, Any]:
    _banner(7, "图谱审查（Review Agent）")
    checks: list[dict[str, Any]] = []

    def _check(name: str, passed: bool, detail: str = "") -> None:
        st = "PASS" if passed else "FAIL"
        checks.append({"name": name, "status": st, "detail": detail})
        (logger.info if passed else logger.error)(
            "  [%s] %s %s", st, name, f"— {detail}" if detail else "")

    # ── 照片实体 ──
    photo_list = _proxy.list_entities(entity_type="Photo", limit=100) or []
    _check("照片实体数量", len(photo_list) >= len(MOCK_PHOTOS),
           f"期望>={len(MOCK_PHOTOS)}, 实际={len(photo_list)}")
    for p in MOCK_PHOTOS:
        _check(f"照片存在: {_phk(p['file_path'])}",
               any(e.get("entity_name") == _phk(p["file_path"]) for e in photo_list),
               f"file_path={p['file_path']}")

    # ── 人物实体 ──
    person_list = _proxy.list_entities(entity_type="Person", limit=100) or []
    _check("人物实体数量", len(person_list) >= 4,
           f"期望>=4, 实际={len(person_list)}")

    # ── 照片→人物 features 关系 ──
    beach_explore = _proxy.explore_node(_phk(MOCK_PHOTOS[0]["file_path"]),
                                         depth=1) or {}
    beach_edges = beach_explore.get("edges", [])
    feat_edges = [e for e in beach_edges if e.get("relation") == "features"]
    _check("海滩照片 features 关系", len(feat_edges) >= 3,
           f"期望>=3, 实际={len(feat_edges)}")

    # ── 根节点→人物 remembers 关系 ──
    root_edges = (_proxy.explore_node(ROOT_ENTITY, depth=1) or {}).get("edges", [])
    rem_edges = [e for e in root_edges if e.get("relation") == "remembers"]
    _check("根节点 remembers 关系", len(rem_edges) >= 3,
           f"期望>=3, 实际={len(rem_edges)}")

    # ── 同框关系 ──
    co_edges = [e for e in beach_edges if e.get("relation") == "co_occurs_with"]
    _check("同框关系存在", len(co_edges) >= 3,
           f"期望>=3, 实际={len(co_edges)}")

    # ── 合并后旧实体不存在 ──
    all_entities = _proxy.list_entities(limit=200) or []
    old_found = any(e.get("entity_name") == "person:p006" for e in all_entities)
    _check("合并后 person:p006 不存在", not old_found,
           f"{'仍存在' if old_found else '已删除'}")

    # ── 合并后关系已迁移 ──
    target_edges = (_proxy.explore_node(
        MERGE_SCENARIO["target_entity"], depth=1) or {}).get("edges", [])
    _check("合并目标有关联边", len(target_edges) >= 1,
           f"边数={len(target_edges)}")

    # ── 无重复实体 ──
    names = [e.get("entity_name", "") for e in all_entities]
    dupes = list(set(n for n in names if names.count(n) > 1))
    _check("无重复实体", len(dupes) == 0,
           f"重复: {dupes[:5]}" if dupes else "")

    # ── 查询功能 ──
    try:
        qr = _proxy.query("谁出现在海滩照片里？", mode="hybrid")
        _check("查询功能可用", bool(qr),
               f"返回长度={len(qr) if qr else 0}")
    except Exception as exc:
        _check("查询功能可用", False, str(exc))

    # ── 审查报告 ──
    pass_count = sum(1 for c in checks if c["status"] == "PASS")
    fail_count = len(checks) - pass_count
    report: dict[str, Any] = {
        "total": len(checks), "passed": pass_count,
        "failed": fail_count, "checks": checks,
    }
    logger.info("审查: %d/%d 通过, %d 失败", pass_count, len(checks), fail_count)
    for c in checks:
        logger.info("  [%s] %s %s",
                     "OK" if c["status"] == "PASS" else "XX",
                     c["name"], c["detail"])
    _stage_done(7)
    return report


# ══════════════════════════════════════════════════════════════
#  阶段 8：对比测试
# ══════════════════════════════════════════════════════════════

async def stage_8_compare() -> dict[str, Any]:
    _banner(8, "对比测试：旧方案 vs 新方案")

    # 旧方案
    t0 = time.monotonic()
    old_result = _proxy.insert(
        content="照片 park_03: 公园散步的两人，老王和未命名人物",
        source_id="compare:old",
    )
    old_time = time.monotonic() - t0
    logger.info("旧方案 (insert): %.3fs — %s", old_time, old_result)

    # 新方案
    t1 = time.monotonic()
    new_result = _proxy.inject_custom_kg(
        entities=[
            {"entity_name": "photo:compare_new", "entity_type": "Photo",
             "description": "对比测试：公园照片", "file_path": "/compare/new.jpg"},
            {"entity_name": "person:compare_new_p1", "entity_type": "Person",
             "description": "对比测试人物A"},
        ],
        relationships=[{"src_id": "photo:compare_new",
                        "tgt_id": "person:compare_new_p1",
                        "keywords": "features",
                        "description": "照片中出现了人物A"}],
        chunks=[{"content": "照片 new: 对比测试，公园照片",
                 "source_id": "photo:compare_new"}],
        source_id="compare:new",
    )
    new_time = time.monotonic() - t1
    logger.info("新方案 (inject_custom_kg): %.3fs — %s", new_time, new_result)

    logger.info("对比: 旧=%.3fs, 新=%.3fs", old_time, new_time)
    logger.info("  新方案优势: 结构化实体/关系，无需 LLM 二次提取")
    logger.info("  旧方案优势: 自动提取，适合非结构化内容")
    _stage_done(8)
    return {"old_time": old_time, "new_time": new_time}


# ══════════════════════════════════════════════════════════════
#  清理 + 主流程
# ══════════════════════════════════════════════════════════════

async def cleanup() -> None:
    _banner(0, "清理测试数据")
    for p in [Path(TEST_WORKSPACE), Path.home() / ".niu" / "lightrag_compare"]:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            logger.info("已清理: %s", p)
    _stage_done(0)


async def _run_stages(stages: list) -> dict[str, Any]:
    """按顺序运行阶段列表，捕获异常。"""
    results: dict[str, Any] = {}
    for stage_fn in stages:
        name = stage_fn.__name__
        try:
            results[name] = await stage_fn()
        except Exception as exc:
            logger.error("%s 异常: %s", name, exc, exc_info=True)
            results[name] = {"error": str(exc)}
    return results


async def run_full() -> dict[str, Any]:
    if not await stage_1_init():
        return {}
    return await _run_stages([
        stage_2_photo_ingest, stage_3_multi_person,
        stage_4_name_person, stage_5_merge_person,
        stage_6_chat_extract, stage_7_review,
    ])


async def run_new_only() -> dict[str, Any]:
    if not await stage_1_init():
        return {}
    return await _run_stages([
        stage_2_photo_ingest, stage_3_multi_person,
        stage_4_name_person, stage_5_merge_person, stage_7_review,
    ])


async def run_compare() -> dict[str, Any]:
    if not await stage_1_init():
        return {}
    return {"stage_8": await stage_8_compare()}


def main() -> None:
    parser = argparse.ArgumentParser(description="照片 KG 结构化注入方案测试")
    parser.add_argument("--new-only", action="store_true", help="仅新方案测试")
    parser.add_argument("--compare", action="store_true", help="对比测试")
    parser.add_argument("--cleanup", action="store_true", help="清理测试数据")
    parser.add_argument("--no-api", action="store_true",
                        help="不启动 API 服务器（需手动确保 LLM 代理可用）")
    args = parser.parse_args()

    if args.cleanup:
        asyncio.run(cleanup())
        return

    # ── 启动 API 服务器（提供 LLM 代理） ──
    if not args.no_api:
        logger.info("检查 API 服务器...")
        if not _api_server.start():
            logger.error("API 服务器启动失败或 LLM 代理不可用，测试终止")
            logger.error("请检查：1) config/user-config.json 中 API Key 配置")
            logger.error("       2) 端口 %d 未被占用", API_PORT)
            logger.error("或使用 --no-api 跳过自动启动（需手动确保 LLM 代理可用）")
            _api_server.stop()
            return
    else:
        logger.info("跳过 API 服务器启动 (--no-api)")
        if _api_server.is_running():
            if _api_server.is_llm_ready():
                logger.info("API 服务器已在运行且 LLM 代理就绪")
            else:
                logger.warning("API 服务器运行中但 LLM 代理不可用")
        else:
            logger.warning("API 服务器未运行 — LLM 调用将失败")

    try:
        if args.compare:
            results = asyncio.run(run_compare())
        elif args.new_only:
            results = asyncio.run(run_new_only())
        else:
            results = asyncio.run(run_full())
    finally:
        # ── 关闭 API 服务器 ──
        _api_server.stop()

    # ── 汇总 ──
    logger.info("=" * 60)
    logger.info("  测试完成汇总")
    logger.info("=" * 60)

    review = results.get("stage_7_review", {})
    if review and "checks" in review:
        logger.info("  审查: %d/%d 通过, %d 失败",
                     review["passed"], review["total"], review["failed"])
        for c in review["checks"]:
            if c["status"] == "FAIL":
                logger.info("    - %s %s", c["name"], c["detail"])

    cmp = results.get("stage_8", {})
    if cmp:
        logger.info("  旧=%.3fs, 新=%.3fs",
                     cmp.get("old_time", 0), cmp.get("new_time", 0))

    logger.info("  测试目录: %s — 用 --cleanup 清理", TEST_WORKSPACE)


if __name__ == "__main__":
    main()