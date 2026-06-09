"""
LightRAG 直连入库基准测试

对比直连LLM和通过代理的入库时间差异，定位入库慢的瓶颈。

用法:
  python scripts/benchmark_lightrag_ingest.py [--direct] [--proxy]

  --direct: 直连LLM API（绕过niu_api代理）
  --proxy:  通过niu_api代理（需要程序已启动）

默认运行两种模式，输出对比报告。
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试文档：使用用户实际入库的同一文件
TEST_DOC = ROOT / "docs" / "SYSTEM_MANUAL.md"

# 直连LLM配置：从 user-config.json 读取
CONFIG_PATH = ROOT / "config" / "user-config.json"


def load_llm_config():
    """从 user-config.json 读取 LLM API 配置。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    llm = config.get("llm", {})
    return {
        "api_key": llm.get("apiKey", ""),
        "base_url": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
    }


def get_test_storage_dir(mode: str) -> Path:
    """返回测试用的独立存储目录。"""
    return Path.home() / ".niu" / f"lightrag_benchmark_{mode}"


def cleanup_storage(storage_dir: Path):
    """清空测试存储目录。"""
    if storage_dir.exists():
        shutil.rmtree(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)


async def run_ingest_test(mode: str, storage_dir: Path) -> dict:
    """运行一次入库测试，返回耗时统计。

    Args:
        mode: "direct" 或 "proxy"
        storage_dir: LightRAG 存储目录
    """
    from lightrag.lightrag import LightRAG, EmbeddingFunc
    from lightrag.llm.openai import openai_complete_if_cache

    # Embedding: 使用本地模型（与生产环境一致）
    from niu_api.internal.embedding import get_model, get_embedding_max_seq_length
    model = get_model()
    max_seq = get_embedding_max_seq_length()

    async def embed_func(texts: list[str]):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: model.encode(texts, convert_to_numpy=True, show_progress_bar=False),
        )

    embedding_func = EmbeddingFunc(
        embedding_dim=768,
        max_token_size=max_seq,
        func=embed_func,
    )

    # LLM: 根据模式选择直连或代理
    llm_config = load_llm_config()

    if mode == "direct":
        # 直连：直接调用LLM API，绕过niu_api代理
        base_url = llm_config["base_url"]
        api_key = llm_config["api_key"]
        model_name = llm_config["model"]
        print(f"  直连模式: model={model_name}, base_url={base_url}")
    else:
        # 代理：通过localhost:9876转发
        base_url = "http://localhost:9876/llm/v1"
        api_key = "not-needed"
        model_name = "proxy-model"
        print(f"  代理模式: model={model_name}, base_url={base_url}")

    async def llm_func(prompt, system_prompt=None, history_messages=None,
                       keyword_extraction=False, **kwargs):
        return await openai_complete_if_cache(
            model_name, prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            base_url=base_url,
            api_key=api_key,
            keyword_extraction=keyword_extraction,
            **kwargs,
        )

    # 创建 LightRAG 实例
    CUSTOM_ENTITY_TYPES = [
        "person", "organization", "technology", "concept",
        "location", "event", "document", "photo", "video",
        "note", "chat", "skill", "tool", "knowledge",
        "interactionhabit", "episodicevent", "brainregion", "other",
    ]

    rag = LightRAG(
        working_dir=str(storage_dir),
        llm_model_func=llm_func,
        llm_model_name=model_name,
        embedding_func=embedding_func,
        chunk_overlap_token_size=50,
        chunk_token_size=1200,
        addon_params={
            "entity_types": CUSTOM_ENTITY_TYPES,
            "language": "Chinese",
        },
    )

    # 初始化存储
    await rag.initialize_storages()

    # 读取测试文档
    doc_content = TEST_DOC.read_text(encoding="utf-8")
    doc_size_kb = len(doc_content) / 1024

    print(f"  文档: {TEST_DOC.name} ({doc_size_kb:.1f} KB)")

    # 开始入库计时
    start_time = time.time()
    print(f"  开始入库...")

    await rag.ainsert(doc_content)

    elapsed = time.time() - start_time

    # 统计入库结果
    G = rag.chunk_entity_relation_graph._graph
    node_count = G.number_of_nodes()
    edge_count = G.number_of_edges()

    result = {
        "mode": mode,
        "doc_file": TEST_DOC.name,
        "doc_size_kb": round(doc_size_kb, 1),
        "elapsed_seconds": round(elapsed, 2),
        "nodes": node_count,
        "edges": edge_count,
    }

    print(f"  完成: {elapsed:.2f}s, {node_count}节点, {edge_count}边")

    return result


async def main():
    parser = argparse.ArgumentParser(description="LightRAG 入库基准测试")
    parser.add_argument("--direct", action="store_true", help="只运行直连模式")
    parser.add_argument("--proxy", action="store_true", help="只运行代理模式")
    args = parser.parse_args()

    modes = []
    if args.direct:
        modes.append("direct")
    if args.proxy:
        modes.append("proxy")
    if not modes:
        modes = ["direct"]  # 默认只跑直连，代理需要程序已启动

    results = []

    for mode in modes:
        print(f"\n{'='*60}")
        print(f"  {mode.upper()} 模式入库测试")
        print(f"{'='*60}")

        storage_dir = get_test_storage_dir(mode)
        cleanup_storage(storage_dir)

        try:
            result = await run_ingest_test(mode, storage_dir)
            results.append(result)
        except Exception as e:
            print(f"  错误: {e}")
            results.append({"mode": mode, "error": str(e)})
        finally:
            # 清理测试数据
            cleanup_storage(storage_dir)

    # 输出对比报告
    print(f"\n{'='*60}")
    print(f"  对比报告")
    print(f"{'='*60}")

    for r in results:
        if "error" in r:
            print(f"  {r['mode']}: 失败 - {r['error']}")
        else:
            print(f"  {r['mode']}: {r['elapsed_seconds']}s, {r['nodes']}节点, {r['edges']}边")

    if len(results) == 2 and "error" not in results[0] and "error" not in results[1]:
        direct_time = results[0]["elapsed_seconds"]
        proxy_time = results[1]["elapsed_seconds"]
        overhead = proxy_time - direct_time
        overhead_pct = (overhead / direct_time * 100) if direct_time > 0 else 0

        print(f"\n  代理额外开销: {overhead:.2f}s ({overhead_pct:.1f}%)")
        if overhead_pct > 20:
            print(f"  ⚠ 代理层显著影响入库速度，需排查代理处理逻辑")
        elif overhead_pct > 10:
            print(f"  ⚠ 代理层有可感知的开销，建议优化")
        else:
            print(f"  ✓ 代理层开销可接受")


if __name__ == "__main__":
    asyncio.run(main())