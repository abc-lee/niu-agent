#!REDACTED_USER_PATH/tools/ai-bot/python/bin/python3
"""
Reproduce "文档入库后卡死" bug — minimal diagnostic script.

This script walks the real call chain step by step:
  inject_document() (sync)
    -> call_async(rag.ainsert(content), timeout=600)
      -> LightRAG.ainsert() runs in lightrag-loop daemon thread
        -> _process_extract_entities() per-chunk LLM call
          -> _llm_model_func()
            -> openai_complete_if_cache("proxy-model", base_url="http://localhost:9876/llm/v1")
              -> HTTP POST to llm_proxy
                -> chat_completions()
                  -> asyncio.to_thread(inject_brain_region_context)
                  -> asyncio.to_thread(sync_call)

Prerequisites:
  - niu_api must already be running (python -m niu_api on port 9876)
  - LLM configured in config/user-config.json
  - LightRAG installed (lightrag-hku fork version)

Run:
  REDACTED_USER_PATH/tools/ai-bot/python/bin/python3 REDACTED_USER_PATH/tools/ai-bot/scripts/reproduce_insert_hang.py
"""

import sys
import os
import signal
import threading
import traceback
import time

# === Setup path ===
PROJECT_DIR = "REDACTED_USER_PATH/tools/ai-bot"
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

# === Global timeout: 300s, then dump stacks and force exit ===
GLOBAL_TIMEOUT = 300
STACK_DUMP_INTERVAL = 10  # seconds between automatic stack dumps

_deadline = time.monotonic() + GLOBAL_TIMEOUT
_stack_dump_timer = None


def _dump_all_thread_stacks(label=""):
    """Print stack traces for all active threads."""
    print(f"\n{'='*60}")
    print(f"  THREAD STACK DUMP {label}  active_count={threading.active_count()}")
    print(f"{'='*60}")
    for thr in threading.enumerate():
        name = thr.name
        is_daemon = thr.daemon
        ident = thr.ident if thr.ident is not None else 0
        print(f"\n--- Thread: {name} (daemon={is_daemon}, ident={ident}) ---")
        stack = sys._current_frames().get(ident) if ident else None
        if stack:
            for filename, lineno, funcname, text in traceback.extract_stack(stack):
                print(f"  {filename}:{lineno} in {funcname}")
                if text:
                    print(f"    {text.strip()}")
        else:
            print("  (no stack available)")
    print(f"{'='*60}\n")


def _periodic_stack_dump():
    """Periodically dump all thread stacks every STACK_DUMP_INTERVAL seconds."""
    elapsed = time.monotonic()
    remaining = _deadline - elapsed
    if remaining <= 0:
        return  # expired, final dump handled by timeout handler

    label = f"[elapsed={elapsed - _start_time:.0f}s, remaining={remaining:.0f}s]"
    _dump_all_thread_stacks(label)

    # Schedule next dump
    next_interval = min(STACK_DUMP_INTERVAL, remaining)
    if next_interval > 0:
        _stack_dump_timer = threading.Timer(next_interval, _periodic_stack_dump)
        _stack_dump_timer.daemon = True
        _stack_dump_timer.start()


def _timeout_handler(_signum, _frame):
    """SIGALRM handler: dump stacks and force exit."""
    elapsed = time.monotonic() - _start_time
    print(f"\n!!! GLOBAL TIMEOUT ({GLOBAL_TIMEOUT}s) at {elapsed:.0f}s !!!")
    _dump_all_thread_stacks("[TIMEOUT-EXPIRED]")
    os._exit(2)


# Register SIGALRM for global timeout (works on Unix)
_start_time = time.monotonic()
signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(GLOBAL_TIMEOUT)

# Start periodic stack dump
_stack_dump_timer = threading.Timer(STACK_DUMP_INTERVAL, _periodic_stack_dump)
_stack_dump_timer.daemon = True
_stack_dump_timer.start()


def _elapsed():
    """Return elapsed seconds since script start."""
    return time.monotonic() - _start_time


def _check_deadline():
    """Raise if global timeout expired."""
    remaining = _deadline - time.monotonic()
    if remaining <= 0:
        print(f"\n!!! Deadline check: {remaining:.0f}s remaining, TIMEOUT !!!")
        _dump_all_thread_stacks("[DEADLINE-EXCEEDED]")
        os._exit(2)


# ============================================================
# Phase 1: Check llm_proxy health
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 1: Check llm_proxy health  [{_elapsed():.1f}s]")
print(f"{'='*60}")

import urllib.request
import urllib.error
import json

PROXY_URL = "http://localhost:9876/llm/v1"

try:
    t0 = time.monotonic()
    req = urllib.request.Request(f"{PROXY_URL}/health")
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode())
    t1 = time.monotonic()
    print(f"  [OK] Health check: {data}  ({t1-t0:.2f}s)")
    if data.get("status") != "ok":
        print(f"  [WARN] Health status is not 'ok': {data}")
except urllib.error.URLError as e:
    print(f"  [FAIL] llm_proxy not reachable: {e}")
    print(f"  Please start niu_api first: python/bin/python3 -m niu_api")
    os._exit(1)
except Exception as e:
    print(f"  [FAIL] Health check error: {e}")
    os._exit(1)

_check_deadline()

# ============================================================
# Phase 2: LLM baseline test — direct POST to llm_proxy
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 2: LLM baseline test (single chat completion)  [{_elapsed():.1f}s]")
print(f"{'='*60}")

# Read LLM config to get model name
config_path = os.path.join(PROJECT_DIR, "config", "user-config.json")
with open(config_path, "r", encoding="utf-8") as f:
    user_config = json.load(f)
llm_cfg = user_config.get("llm", {})
model_name = llm_cfg.get("model", llm_cfg.get("Model", "unknown"))
print(f"  Config: model={model_name}")

# Simple chat completion request
chat_payload = json.dumps({
    "model": model_name,
    "messages": [{"role": "user", "content": "Say 'hello' in one word."}],
    "temperature": 0.1,
    "stream": False,
}).encode()

try:
    t0 = time.monotonic()
    print(f"  [START] POST {PROXY_URL}/chat/completions  thread_count={threading.active_count()}")
    req = urllib.request.Request(
        f"{PROXY_URL}/chat/completions",
        data=chat_payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=60)
    resp_data = json.loads(resp.read().decode())
    t1 = time.monotonic()
    print(f"  [OK] LLM baseline response in {t1-t0:.2f}s")
    print(f"  Response model: {resp_data.get('model', '?')}")
    content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "?")
    print(f"  Response content (first 100 chars): {content[:100]}")
    usage = resp_data.get("usage", {})
    print(f"  Usage: prompt={usage.get('prompt_tokens',0)} completion={usage.get('completion_tokens',0)} total={usage.get('total_tokens',0)}")
except urllib.error.URLError as e:
    print(f"  [FAIL] LLM baseline request failed: {e}")
    _dump_all_thread_stacks("[LLM-BASELINE-FAIL]")
    os._exit(1)
except Exception as e:
    print(f"  [FAIL] LLM baseline error: {e}")
    _dump_all_thread_stacks("[LLM-BASELINE-FAIL]")
    os._exit(1)

_check_deadline()

# ============================================================
# Phase 3: LightRAG initialization
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 3: LightRAG initialization  [{_elapsed():.1f}s]")
print(f"{'='*60}")

print(f"  [START] Importing and calling get_lightrag()  thread_count={threading.active_count()}")

from niu_api.internal.lightrag_manager import get_lightrag, call_async, _ensure_loop

t0 = time.monotonic()
print(f"  [START] get_lightrag() ...")
rag = get_lightrag()
t1 = time.monotonic()

if rag is None:
    print(f"  [FAIL] get_lightrag() returned None after {t1-t0:.2f}s")
    print(f"  LightRAG init may have failed. Check logs.")
    os._exit(1)

print(f"  [OK] LightRAG initialized in {t1-t0:.2f}s  thread_count={threading.active_count()}")

# Verify the lightrag-loop thread exists
loop = _ensure_loop()
print(f"  LightRAG event loop: running={loop.is_running()} thread_count={threading.active_count()}")

_check_deadline()

# ============================================================
# Phase 4: Single chunk insert test (short text < 1200 tokens)
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 4: Single chunk insert (short text)  [{_elapsed():.1f}s]")
print(f"{'='*60}")

SHORT_TEXT = """
Python是一种广泛使用的高级编程语言，由Guido van Rossum于1991年创建。
Python以其简洁的语法和强大的标准库而闻名，支持多种编程范式包括面向对象、
函数式和过程式编程。Python在Web开发、数据分析、人工智能和自动化等领域
有着广泛的应用。
"""

print(f"  Text length: {len(SHORT_TEXT)} chars")
print(f"  [START] call_async(rag.ainsert(SHORT_TEXT), timeout=600)  thread_count={threading.active_count()}")

t0 = time.monotonic()
try:
    track_id = call_async(rag.ainsert(SHORT_TEXT), timeout=600)
    t1 = time.monotonic()
    print(f"  [OK] Single chunk insert completed in {t1-t0:.2f}s")
    print(f"  track_id: {track_id}")
    print(f"  thread_count: {threading.active_count()}")
except Exception as e:
    t1 = time.monotonic()
    print(f"  [FAIL] Single chunk insert failed after {t1-t0:.2f}s: {type(e).__name__}: {e}")
    _dump_all_thread_stacks("[SINGLE-CHUNK-FAIL]")
    os._exit(1)

_check_deadline()

# ============================================================
# Phase 5: Multi chunk insert test (long text, 3-4 chunks)
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 5: Multi chunk insert (long text ~3-4 chunks)  [{_elapsed():.1f}s]")
print(f"{'='*60}")

LONG_TEXT = """
第一章：人工智能的基础概念

人工智能（Artificial Intelligence，简称AI）是计算机科学的一个分支，
致力于创建能够模拟人类智能行为的系统。AI的研究始于1950年代，
Alan Turing提出了著名的"图灵测试"来评估机器是否具有智能。
AI可以分为弱人工智能和强人工智能：弱AI专注于特定任务，
如语音识别或图像分类；强AI则追求通用智能，能够像人类一样思考和学习。

机器学习是AI的核心技术之一，它使计算机能够从数据中学习模式而无需
显式编程。常见的机器学习方法包括监督学习、无监督学习和强化学习。
监督学习使用标注数据训练模型，例如分类和回归问题；
无监督学习则从未标注数据中发现结构，如聚类和降维；
强化学习通过试错与奖励信号来优化决策策略。

第二章：深度学习与神经网络

深度学习是机器学习的一个子领域，使用多层神经网络来学习数据的
层次化表示。卷积神经网络（CNN）在图像处理中表现卓越，
能够自动提取空间特征。循环神经网络（RNN）及其变体LSTM和GRU
擅长处理序列数据如文本和时间序列。Transformer架构的引入
彻底改变了自然语言处理领域，BERT和GPT等模型展示了强大的
语言理解能力。

神经网络的训练依赖于反向传播算法和梯度下降优化。
训练过程中的关键挑战包括梯度消失、过拟合和计算资源需求。
解决方案包括使用残差连接、Dropout正则化和批量归一化等技术。
现代深度学习框架如PyTorch和TensorFlow提供了高效的自动微分
和分布式训练支持。

第三章：自然语言处理

自然语言处理（NLP）使计算机能够理解、生成和转换人类语言。
早期NLP系统依赖规则和统计方法，现代NLP则基于深度学习。
词嵌入技术如Word2Vec将词语映射为稠密向量表示，
捕获语义关系。注意力机制使模型能够聚焦于输入中最重要的部分，
Transformer架构完全基于注意力机制，摒弃了循环结构。

大语言模型（LLM）是NLP领域的重大突破。GPT系列采用自回归生成，
BERT使用掩码语言模型进行预训练。这些模型通过海量文本数据训练，
展现出强大的零样本和少样本学习能力。模型规模的扩大带来了
涌现能力——模型在参数量达到一定规模后突然获得新能力。

第四章：AI的应用与未来

AI技术已在众多领域产生深远影响。在医疗领域，AI辅助诊断和
药物发现；在金融领域，AI用于风险评估和算法交易；在制造业，
AI驱动的预测性维护减少停机时间。自动驾驶技术结合感知、
决策和控制模块，代表AI的综合应用能力。

AI的未来发展面临重要挑战：确保AI系统的安全性和可控性、
解决算法偏见和公平性问题、保护数据隐私、以及管理AI对
就业市场的影响。可解释AI（XAI）旨在让AI决策过程透明化，
人机协作模式探索AI与人类的最佳合作方式。负责任的AI发展
需要在技术创新与社会责任之间取得平衡。
"""

print(f"  Text length: {len(LONG_TEXT)} chars")
# Estimate chunks: LightRAG uses chunk_token_size=1200, overlap=50
# Rough estimate: len(text) / 4 ≈ tokens, so ~4 chunks expected
est_tokens = len(LONG_TEXT) // 4
est_chunks = max(1, (est_tokens - 1200) // (1200 - 50) + 1)
print(f"  Estimated: ~{est_tokens} tokens, ~{est_chunks} chunks (chunk_size=1200, overlap=50)")
print(f"  [START] call_async(rag.ainsert(LONG_TEXT), timeout=600)  thread_count={threading.active_count()}")

t0 = time.monotonic()
# Print progress every 30s while waiting
def _progress_monitor():
    while True:
        time.sleep(30)
        elapsed_wait = time.monotonic() - t0
        remaining_global = _deadline - time.monotonic()
        print(f"  [PROGRESS] Still waiting for ainsert... elapsed={elapsed_wait:.0f}s global_remaining={remaining_global:.0f}s thread_count={threading.active_count()}")

progress_thread = threading.Thread(target=_progress_monitor, daemon=True, name="progress-monitor")
progress_thread.start()

try:
    track_id = call_async(rag.ainsert(LONG_TEXT), timeout=600)
    t1 = time.monotonic()
    print(f"  [OK] Multi chunk insert completed in {t1-t0:.2f}s")
    print(f"  track_id: {track_id}")
    print(f"  thread_count: {threading.active_count()}")
except Exception as e:
    t1 = time.monotonic()
    print(f"  [FAIL] Multi chunk insert failed after {t1-t0:.2f}s: {type(e).__name__}: {e}")
    _dump_all_thread_stacks("[MULTI-CHUNK-FAIL]")
    os._exit(1)

_check_deadline()

# ============================================================
# Phase 6: inject_document via LightRAGIngester (the real bug path)
# ============================================================

print(f"\n{'='*60}")
print(f"  Phase 6: inject_document via LightRAGIngester (real bug path)  [{_elapsed():.1f}s]")
print(f"{'='*60}")

# This is the exact call chain used by the handler: inject_document -> call_async(rag.ainsert)
from niu_api.internal.lightrag_adapter import LightRAGIngester

ingester = LightRAGIngester()

DOC_TEXT = """
软件架构设计原则

模块化是软件架构的基础原则，将系统分解为独立的、可替换的模块。
每个模块封装特定的功能，通过定义良好的接口与其他模块交互。
模块化降低了系统的复杂度，提高了可维护性和可测试性。

关注点分离（Separation of Concerns）要求将不同功能领域的代码
分开组织。例如，业务逻辑与数据访问应该分层处理，
用户界面与核心算法应该独立演进。这种分离使变更的影响范围最小化。

依赖倒置原则（Dependency Inversion）强调高层模块不应依赖低层模块，
两者都应依赖抽象。抽象不应依赖细节，细节应依赖抽象。
这一原则促进了松耦合设计，使得系统更容易扩展和重构。

微服务架构将应用拆分为一组小型服务，每个服务围绕特定业务能力构建，
独立部署和扩展。服务间通过轻量级通信机制（如HTTP/REST或消息队列）
协作。微服务带来了部署灵活性和技术多样性，但也增加了分布式系统的
复杂性，包括服务发现、数据一致性和故障处理等挑战。
"""

print(f"  Text length: {len(DOC_TEXT)} chars")
print(f"  [START] ingester.inject_document(DOC_TEXT, doc_id='test-hang-repro')  thread_count={threading.active_count()}")

t0 = time.monotonic()
try:
    result = ingester.inject_document(DOC_TEXT, doc_id="test-hang-repro", file_path="test_hang_repro.md")
    t1 = time.monotonic()
    print(f"  [OK] inject_document completed in {t1-t0:.2f}s")
    print(f"  Result: {result}")
    print(f"  thread_count: {threading.active_count()}")
except Exception as e:
    t1 = time.monotonic()
    print(f"  [FAIL] inject_document failed after {t1-t0:.2f}s: {type(e).__name__}: {e}")
    _dump_all_thread_stacks("[INJECT-DOCUMENT-FAIL]")
    os._exit(1)

# ============================================================
# Summary
# ============================================================

total_elapsed = time.monotonic() - _start_time
print(f"\n{'='*60}")
print(f"  ALL PHASES COMPLETED  total_elapsed={total_elapsed:.1f}s")
print(f"{'='*60}")
print(f"  Phase 1 (health check): OK")
print(f"  Phase 2 (LLM baseline): OK")
print(f"  Phase 3 (LightRAG init): OK")
print(f"  Phase 4 (single chunk):  OK")
print(f"  Phase 5 (multi chunk):   OK")
print(f"  Phase 6 (inject_document): OK")
print(f"  Final thread_count: {threading.active_count()}")
print(f"\n  If you saw a hang, the periodic stack dumps above show where it stuck.")
print(f"  Compare stuck thread stacks to the expected call chain.")

# Disable timeout alarm since we completed successfully
signal.alarm(0)
if _stack_dump_timer:
    _stack_dump_timer.cancel()

os._exit(0)