"""生成合成 fixture（不含真实人名），用于端到端测试。"""
import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent


def generate():
    # 3 个文档（虚构内容）
    docs = {
        "doc-syn-1": {
            "content": "测试文档1：虚构人物张三李四王五的介绍，用于测试 LightRAG 重建流程。",
            "file_path": "synthetic1.md",
            "create_time": 1781930610,
            "update_time": 1781930610,
            "_id": "doc-syn-1",
        },
        "doc-syn-2": {
            "content": "测试文档2：虚构组织测试公司的业务介绍，用于测试脑区功能。",
            "file_path": "synthetic2.md",
            "create_time": 1781930611,
            "update_time": 1781930611,
            "_id": "doc-syn-2",
        },
        "doc-syn-3": {
            "content": "测试文档3：系统维护日志，记录删除重复脑区的操作。",
            "file_path": "synthetic3.md",
            "create_time": 1781930612,
            "update_time": 1781930612,
            "_id": "doc-syn-3",
        },
    }

    # 5 个正常 extract cache + 1 个僵尸脑区 cache
    cache = {
        "default:extract:syn-key-1": {
            "return": "entity<|#|>张三<|#|>person<|#|>虚构人物张三的介绍。\nrelation<|#|>人际关系脑区<|#|>张三<|#|>包含<|#|>人际关系脑区包含张三。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-1",
            "original_prompt": "synthetic",
            "create_time": 1781930610,
        },
        "default:extract:syn-key-2": {
            "return": "entity<|#|>李四<|#|>person<|#|>虚构人物李四的介绍。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-2",
            "create_time": 1781930611,
        },
        "default:extract:syn-key-3": {
            "return": "entity<|#|>测试公司<|#|>organization<|#|>虚构测试公司介绍。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-3",
            "create_time": 1781930612,
        },
        "default:extract:syn-key-4": {
            "return": "entity<|#|>王五<|#|>person<|#|>虚构人物王五。",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-1",
            "create_time": 1781930613,
        },
        "default:extract:syn-key-5": {
            "return": "<|COMPLETE|>",
            "cache_type": "extract",
            "chunk_id": "chunk-syn-2",
            "create_time": 1781930614,
        },
        # 僵尸脑区 cache（description 含"被删除"标记）
        "default:extract:zombie-syn": {
            "return": "entity<|#|>智家测试僵尸脑区<|#|>brainregion<|#|>被删除的重复脑区实体之一。",
            "cache_type": "extract",
            "chunk_id": "chunk-zombie-syn",
            "create_time": 1781930615,
        },
    }

    (FIXTURE_DIR / "kv_store_full_docs.json").write_text(
        json.dumps(docs, ensure_ascii=False, indent=2)
    )
    (FIXTURE_DIR / "kv_store_llm_response_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2)
    )
    print(f"生成 fixture 到 {FIXTURE_DIR}")


if __name__ == "__main__":
    generate()
