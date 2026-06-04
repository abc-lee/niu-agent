#!/usr/bin/env python3
"""
通过 HTTP API 触发目录入库 — 调用主进程的 LightRAG 实例

使用 lightrag-server 的 MCP 工具接口，通过 HTTP 调用入库
"""
import json
import time
import urllib.request
from datetime import datetime

API_HOST = "127.0.0.1"
API_PORT = 9876
MONITOR_URL = f"http://{API_HOST}:{API_PORT}/api/kg/test_status_monitor"
PIPELINE_URL = f"http://{API_HOST}:{API_PORT}/api/kg/pipeline_status"
LOG_FILE = "tests/directory_ingest_test.log"

# 测试文件内容
TEST_DOCS = [
    ("quantum_computing.md", "Quantum computing uses quantum bits called qubits for computation. Quantum superposition allows qubits to exist in multiple states simultaneously, while quantum entanglement creates correlations between qubits that enable powerful parallel processing. Major applications include cryptography, drug discovery, optimization problems, and financial modeling."),
    ("machine_learning.md", "Machine learning is a subset of artificial intelligence that enables systems to learn from data. Supervised learning uses labeled datasets, unsupervised learning discovers hidden patterns, and reinforcement learning optimizes through reward signals. Deep learning uses neural networks with many layers to model complex patterns."),
    ("nlp.md", "Natural language processing enables computers to understand, interpret, and generate human language. Key techniques include tokenization, named entity recognition, sentiment analysis, machine translation, and text summarization. Transformer architectures have revolutionized NLP with self-attention mechanisms."),
    ("computer_vision.md", "Computer vision allows machines to interpret visual information from images and videos. Convolutional neural networks extract hierarchical features, object detection identifies and locates objects, semantic segmentation classifies each pixel, and generative models create realistic images."),
    ("knowledge_graphs.md", "Knowledge graphs represent information as networks of entities and relationships. They enable semantic search, reasoning, and question answering. Key technologies include entity extraction, relation extraction, graph databases, and embedding methods for link prediction."),
]


def call_lightrag_insert(file_path, content):
    """通过 /api/kg/insert 端点调用入库"""
    url = f"http://{API_HOST}:{API_PORT}/api/kg/insert"
    data = json.dumps({"file_path": file_path, "content": content}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def check_endpoint_exists():
    """检查入库端点是否存在"""
    try:
        with urllib.request.urlopen(f"http://{API_HOST}:{API_PORT}/api/kg/test_status_monitor", timeout=3) as resp:
            return True
    except:
        return False


def main():
    if not check_endpoint_exists():
        print("ERROR: API server not running or test_status_monitor endpoint not available")
        print("Please restart niu program first")
        return

    with open(LOG_FILE, "w") as f:
        f.write(f"=== Directory Ingest Test {datetime.now()} ===\n\n")

        # 先触发入库 — 通过 lightrag-server 的 HTTP 接口
        # 检查有没有 insert 端点
        insert_url = f"http://{API_HOST}:{API_PORT}/api/kg/insert"
        try:
            urllib.request.urlopen(insert_url, timeout=2)
        except urllib.error.HTTPError as e:
            if e.code == 405:  # Method Not Allowed = endpoint exists
                print("Insert endpoint exists (405 = correct for GET on POST endpoint)")
            else:
                print(f"Insert endpoint returned: {e.code}")
        except:
            print("No insert endpoint, trying alternative...")

        # 如果没有 insert 端点，用另一种方式
        # 通过 chat API 让 agent 触发入库太慢了
        # 直接用 test 端点做持续监控，用户手动触发入库

        print("Monitoring started. Please trigger ingestion from the UI now.")
        print(f"Logging to {LOG_FILE}")

        max_progress = 0
        try:
            while True:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                monitor = fetch(MONITOR_URL)
                pipeline = fetch(PIPELINE_URL)

                m_ing = monitor.get("ingesting", False)
                m_prg = monitor.get("progress", 0)
                m_busy = monitor.get("busy", False)
                m_docs = monitor.get("doc_total", 0)

                p_busy = pipeline.get("busy", False)
                p_prg = pipeline.get("progress", 0)

                reg = ""
                if isinstance(m_prg, (int, float)) and isinstance(m_ing, bool) and m_ing and m_prg < max_progress and max_progress > 0:
                    reg = f" REGRESS!{max_progress}->{m_prg}"
                if isinstance(m_prg, (int, float)) and m_prg > max_progress:
                    max_progress = m_prg

                line = f"{ts} | monitor:ing={m_ing} prg={m_prg}% busy={m_busy} docs={m_docs} | pipeline:busy={p_busy} prg={p_prg}%{reg}\n"
                f.write(line)
                f.flush()
                time.sleep(0.5)
        except KeyboardInterrupt:
            f.write(f"\n=== Max progress: {max_progress}% ===\n")


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    main()
