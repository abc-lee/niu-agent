# LLM 调用延迟诊断计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 找出 LLM 调用从"很快"变"慢"的原因

**Architecture:** TDD 风格诊断 - 先写测试验证假设，再根据结果调整方向

**Tech Stack:** Python, pytest, httpx, LiteLLM, LightRAG

---

## 问题背景

- 用户说 LLM 调用原来很快，现在变慢了
- 测试脚本显示单次 LLM 调用约 17 秒
- 但日志显示 LightRAG 的 ainsert 调用耗时 56.82s + 36.03s = 93 秒
- 需要找出**什么变化导致变慢**

## 已知事实

1. LightRAG 的实体提取 prompt 很长（包含 3 个示例）
2. `entity_extract_max_gleaning=1`（默认值），每个 chunk 需要 2 次 LLM 调用
3. 最近有性能优化提交（设置 `LITELLM_LOCAL_MODEL_COST_MAP` 和 `LITELLM_NO_AIOHTTP_TRANSPORT`）

---

### Task 1: 建立基准测试

**Files:**
- Create: `scripts/test_llm_baseline.py`

- [ ] **Step 1: 写基准测试脚本**

```python
"""
LLM 调用基准测试

目标：建立各层级的性能基准，找出延迟来源
"""

import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_httpx_direct():
    """测试 1: 直接 httpx 调用 API（基准线）"""
    import httpx

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    config = json.loads(config_path.read_text())
    llm = config.get("llm", {})

    url = f"{llm.get('apiBase', '')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm.get('apiKey', '')}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": llm.get("model", ""),
        "messages": [{"role": "user", "content": "说一个数字"}],
        "stream": False,
    }

    t0 = time.time()
    with httpx.Client(timeout=60) as client:
        response = client.post(url, headers=headers, json=payload)
        t1 = time.time()

    print(f"httpx 直接调用: {t1-t0:.2f}s")
    return t1 - t0


def test_lightrag_llm_func():
    """测试 2: LightRAG llm_model_func"""
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("LightRAG 不可用")
        return -1

    async def run_test():
        t0 = time.time()
        result = await rag.llm_model_func("说一个数字")
        t1 = time.time()
        print(f"LightRAG llm_model_func: {t1-t0:.2f}s")
        return t1 - t0

    return call_async(run_test(), timeout=120)


def test_lightrag_entity_extraction():
    """测试 3: LightRAG 实体提取（完整 prompt）"""
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    rag = get_lightrag()
    if rag is None:
        print("LightRAG 不可用")
        return -1

    # 模拟 LightRAG 的实体提取 prompt
    test_content = "用户将照片中的人物命名为安安，系统已完成人物识别和知识图谱关联"

    async def run_test():
        t0 = time.time()
        # 调用 LightRAG 的内部实体提取
        from lightrag.prompt import PROMPTS
        system_prompt = PROMPTS.get('entity_extraction_system_prompt', '')
        # 简化的测试
        result = await rag.llm_model_func(test_content, system_prompt=system_prompt)
        t1 = time.time()
        print(f"LightRAG 实体提取: {t1-t0:.2f}s")
        return t1 - t0

    return call_async(run_test(), timeout=120)


def main():
    print("=" * 60)
    print("LLM 调用基准测试")
    print("=" * 60)

    results = {}
    results["httpx"] = test_httpx_direct()
    results["lightrag_llm"] = test_lightrag_llm_func()
    results["lightrag_entity"] = test_lightrag_entity_extraction()

    print("\n" + "=" * 60)
    print("结果汇总")
    print("=" * 60)
    for name, elapsed in results.items():
        if elapsed > 0:
            print(f"  {name}: {elapsed:.2f}s")

    # 分析
    if results["httpx"] > 0 and results["lightrag_llm"] > 0:
        overhead = results["lightrag_llm"] - results["httpx"]
        print(f"\n  LightRAG 开销: {overhead:.2f}s")

    if results["lightrag_llm"] > 0 and results["lightrag_entity"] > 0:
        overhead = results["lightrag_entity"] - results["lightrag_llm"]
        print(f"  实体提取 prompt 开销: {overhead:.2f}s")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行基准测试**

Run: `cd /Users/lilei/tools/ai-bot && python scripts/test_llm_baseline.py`
Expected: 显示各层级耗时

- [ ] **Step 3: 分析结果，确定下一步方向**

根据测试结果：
- 如果 httpx 很慢（>30s）：问题在网络/API
- 如果 LightRAG llm_model_func 慢：问题在 LightRAG 封装
- 如果实体提取 prompt 开销大：问题在 prompt 长度

---

### Task 2: 对比历史版本

**Files:**
- Modify: `scripts/test_llm_baseline.py`

- [ ] **Step 1: 检查 git 历史，找出"快"的版本**

Run: `git log --oneline -20 -- niu_api/internal/lightrag_manager.py agent/generic/litellm_adapter.py`
Expected: 显示最近 20 个提交

- [ ] **Step 2: 检查 LightRAG 版本是否变化**

Run: `pip show lightrag-hku | grep Version`
Expected: 显示当前版本

- [ ] **Step 3: 检查是否有配置变化**

检查 `preferences.json` 中的 LightRAG 配置是否有变化。

---

### Task 3: 验证假设

根据 Task 1 和 Task 2 的结果，验证以下假设：

- [ ] **假设 1: LightRAG prompt 变长**
  - 检查 LightRAG 版本升级是否导致 prompt 变长

- [ ] **假设 2: 网络延迟增加**
  - 对比不同时间的 API 响应时间

- [ ] **假设 3: 配置变化**
  - 检查 `entity_extract_max_gleaning` 等配置是否变化

---

### Task 4: 提出解决方案

根据验证结果，提出解决方案：

- [ ] **如果是 prompt 问题**：
  - 考虑自定义 prompt（减少示例）
  - 或使用更快的模型做实体提取

- [ ] **如果是网络问题**：
  - 检查 API 配置
  - 考虑使用缓存

- [ ] **如果是配置问题**：
  - 调整 LightRAG 配置
