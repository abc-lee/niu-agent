#!/usr/bin/env python3
"""
LightRAG Pipeline Monitor

Independent script that monitors LightRAG's ingestion pipeline progress
by directly reading its shared memory (_shared_dicts). Works completely
independently from the niu_api HTTP server.

Usage:
    python3 scripts/pipeline_monitor.py          # Continuous polling (default)
    python3 scripts/pipeline_monitor.py --once   # Poll once and exit
    python3 scripts/pipeline_monitor.py --watch  # Same as default (explicit)
"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

# ============== Config (same as lightrag_manager.py) ==============

PROXY_BASE_URL = "http://localhost:9876/llm/v1"
PROXY_API_KEY = "not-needed"
STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# ============== Embedding Model (standalone, no niu_api import) ==============

SUPPORTED_MODELS = {
    "bge-base-zh-v1.5": {
        "local_dir": "bge-base-zh-v1.5",
        "hf_id": "BAAI/bge-base-zh-v1.5",
        "dim": 768,
    },
    "bge-m3": {
        "local_dir": "bge-m3",
        "hf_id": "BAAI/bge-m3",
        "dim": 1024,
    },
    "minilm-l12": {
        "local_dir": "paraphrase-multilingual-MiniLM-L12-v2",
        "hf_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
    },
}

DEFAULT_MODEL = "bge-base-zh-v1.5"

_embedding_model = None
_embedding_model_name: Optional[str] = None
_embedding_lock = threading.Lock()


def _get_embedding_model_name() -> str:
    """Read embedding model name from preferences.json."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            model_name = prefs.get("lightrag", {}).get("embedding_model", "")
            if model_name and model_name in SUPPORTED_MODELS:
                return model_name
    except Exception:
        pass
    return DEFAULT_MODEL


def _get_embedding_dim() -> int:
    """Get embedding dimension from config."""
    model_name = _get_embedding_model_name()
    return SUPPORTED_MODELS[model_name]["dim"]


def _get_models_dir() -> Path:
    """Get models directory path."""
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])
    return PROJECT_ROOT / "models"


def _get_device() -> str:
    """Detect optimal device (GPU priority)."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _get_embedding_model():
    """Get or load the embedding model."""
    global _embedding_model, _embedding_model_name

    with _embedding_lock:
        requested = _get_embedding_model_name()

        if _embedding_model is not None and _embedding_model_name == requested:
            return _embedding_model

        if _embedding_model is not None and _embedding_model_name != requested:
            _embedding_model = None
            _embedding_model_name = None

        try:
            from sentence_transformers import SentenceTransformer

            models_dir = _get_models_dir()
            model_info = SUPPORTED_MODELS[requested]
            local_path = models_dir / model_info["local_dir"]
            device = _get_device()

            if local_path.exists():
                logger.info(f"Loading embedding model from: {local_path}")
                _embedding_model = SentenceTransformer(str(local_path))
            else:
                logger.info(f"Downloading embedding model: {model_info['hf_id']}")
                os.environ.pop("HF_HUB_OFFLINE", None)
                _embedding_model = SentenceTransformer(model_info["hf_id"])
                _embedding_model.save(str(local_path))

            _embedding_model = _embedding_model.to(device)
            _embedding_model_name = requested
            logger.info(f"Embedding model ready: {requested} ({model_info['dim']}d) on {device}")

        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise

        return _embedding_model


def _make_embedding_func():
    """Create embedding callable for LightRAG."""
    async def _embed(texts: List[str]):
        model = _get_embedding_model()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        )
    return _embed


# ============== Reranker (standalone) ==============

SUPPORTED_RERANKERS = {
    "bge-reranker-v2-m3": {
        "local_dir": "bge-reranker-v2-m3",
        "hf_id": "BAAI/bge-reranker-v2-m3",
    },
    "bge-reranker-v2-gemma": {
        "local_dir": "bge-reranker-v2-gemma",
        "hf_id": "BAAI/bge-reranker-v2-gemma",
    },
    "none": {"local_dir": "", "hf_id": ""},
}

_reranker_model = None
_reranker_name: Optional[str] = None
_reranker_lock = threading.Lock()


def _get_reranker_model_name() -> str:
    """Read reranker model name from preferences.json."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            model_name = prefs.get("lightrag", {}).get("reranker_model", "")
            if model_name and model_name in SUPPORTED_RERANKERS:
                return model_name
    except Exception:
        pass
    return "none"


def _get_reranker():
    """Get or load reranker model."""
    global _reranker_model, _reranker_name

    with _reranker_lock:
        requested = _get_reranker_model_name()

        if requested == "none":
            return None

        if _reranker_model is not None and _reranker_name == requested:
            return _reranker_model

        try:
            from sentence_transformers import CrossEncoder

            models_dir = _get_models_dir()
            model_info = SUPPORTED_RERANKERS[requested]
            local_path = models_dir / model_info["local_dir"]

            if local_path.exists():
                logger.info(f"Loading reranker from: {local_path}")
                _reranker_model = CrossEncoder(str(local_path))
            else:
                logger.info(f"Downloading reranker: {model_info['hf_id']}")
                os.environ.pop("HF_HUB_OFFLINE", None)
                _reranker_model = CrossEncoder(model_info["hf_id"])
                _reranker_model.save(str(local_path))

            _reranker_name = requested
            logger.info(f"Reranker ready: {requested}")

        except Exception as e:
            logger.error(f"Failed to load reranker: {e}")
            return None

        return _reranker_model


def _make_reranker_callable():
    """Create reranker callable for LightRAG."""
    if _get_reranker_model_name() == "none":
        return None

    def _reranker(query: str, documents: List[str]) -> List[tuple]:
        model = _get_reranker()
        if model is None:
            return [(i, 1.0) for i in range(len(documents))]

        pairs = [[query, doc] for doc in documents]
        scores = model.predict(pairs)
        results = [(i, float(scores[i])) for i in range(len(documents))]
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    return _reranker


# ============== Async/Sync Bridge (same as lightrag_manager.py) ==============

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Ensure the daemon event loop is running."""
    global _loop, _loop_thread

    if _loop is not None and _loop.is_running():
        return _loop

    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop

        _loop_ready.clear()

        def _run_loop():
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop_ready.set()
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="lightrag-loop")
        _loop_thread.start()

        if not _loop_ready.wait(timeout=5.0):
            raise RuntimeError("Event loop failed to start")

        return _loop


def call_async(coro, timeout: int = 120):
    """Run an async coroutine in the event loop (blocking)."""
    import concurrent.futures as _cf

    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except _cf.TimeoutError:
        future.cancel()
        raise
    except asyncio.CancelledError:
        future.cancel()
        raise
    except Exception:
        future.cancel()
        raise


# ============== LightRAG Initialization ==============

_rag_instance = None
_rag_lock = threading.Lock()


def _get_lightrag_config() -> Dict[str, Any]:
    """Read LightRAG config from preferences.json."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            return prefs.get("lightrag", {})
    except Exception:
        pass
    return {}


def _create_lightrag_instance():
    """Create a LightRAG instance."""
    try:
        from lightrag.lightrag import LightRAG, EmbeddingFunc
        from lightrag.llm.openai import openai_complete_if_cache
    except ImportError:
        raise ImportError("LightRAG not installed. Run: pip install lightrag-hku")

    config = _get_lightrag_config()
    embedding_dim = _get_embedding_dim()

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # LLM function (proxy to niu_api)
    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> str:
        return await openai_complete_if_cache(
            "proxy-model", prompt,
            system_prompt=system_prompt, history_messages=history_messages,
            base_url=PROXY_BASE_URL, api_key=PROXY_API_KEY,
            keyword_extraction=keyword_extraction, **kwargs,
        )

    # Embedding function
    embedding_func_config = dict(
        embedding_dim=embedding_dim,
        max_token_size=512,
        func=_make_embedding_func(),
    )

    # Reranker
    reranker_func = _make_reranker_callable()

    # Entity types
    CUSTOM_ENTITY_TYPES = [
        "Person", "Organization", "Technology", "Concept",
        "Location", "Event", "Document", "Photo", "Video",
        "Note", "Chat", "Skill", "Tool", "Knowledge",
        "InteractionHabit", "EpisodicEvent", "BrainRegion", "Other",
    ]

    rag_params = dict(
        working_dir=str(STORAGE_DIR),
        llm_model_func=_llm_model_func,
        llm_model_name="proxy-model",
        embedding_func=EmbeddingFunc(**embedding_func_config),
        chunk_overlap_token_size=50,
        chunk_token_size=1200,
        addon_params={
            "entity_types": CUSTOM_ENTITY_TYPES,
            "language": "Chinese",
        },
    )

    if reranker_func is not None:
        rag_params["rerank_model_func"] = reranker_func
        logger.info("Reranker enabled")
    else:
        logger.info("Reranker disabled")

    rag = LightRAG(**rag_params)
    call_async(rag.initialize_storages(), timeout=300)
    return rag


def get_lightrag():
    """Get the LightRAG instance (lazy-init)."""
    global _rag_instance

    if _rag_instance is not None:
        return _rag_instance

    with _rag_lock:
        if _rag_instance is not None:
            return _rag_instance

        try:
            logger.info("Initializing LightRAG instance...")
            _rag_instance = _create_lightrag_instance()
            logger.info("LightRAG instance ready")
        except ImportError as e:
            logger.warning(f"LightRAG not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            return None

    return _rag_instance


# ============== Progress Parsing (from kg_api.py) ==============

def _parse_file_progress(msg: str) -> int:
    """Parse within-file progress from LightRAG latest_message. Returns 0-100."""
    if not msg:
        return 0

    if msg.startswith("Completed processing file"):
        return 100
    if msg.startswith("Completed merging"):
        return 98

    if "Phase 3" in msg:
        return 95
    if "Phase 2" in msg:
        return 85
    if "Phase 1" in msg:
        return 75

    if "Merged:" in msg and "~" in msg:
        return 82
    if "LLMmrg:" in msg and "~" in msg:
        return 82

    if "LLMmrg:" in msg:
        return 78
    if "Merged:" in msg:
        return 78

    if msg.startswith("Merging stage"):
        return 70

    if msg.startswith("Chunks appended from relation"):
        return 86

    m = re.search(r"Chunk (\d+) of (\d+) extracted", msg)
    if m:
        chunk_cur = int(m.group(1))
        chunk_total = int(m.group(2))
        if chunk_total > 0:
            return int(chunk_cur / chunk_total * 70)
        return 35

    if msg.startswith("Processing d-id:"):
        return 1
    if "document(s)" in msg and "Processing" in msg:
        return 0

    if msg.startswith("Failed to extract document") or msg.startswith("User cancelled"):
        return -1
    if msg.startswith("Error processing"):
        return -1

    return -1


def _cleanup_failed_docs(rag) -> Dict[str, int]:
    """Remove unrecoverable FAILED entries from doc_status."""
    from lightrag.base import DocStatus

    counts = {"dup_deleted": 0, "empty_deleted": 0, "real_failures": 0}

    try:
        failed_docs = call_async(
            rag.doc_status.get_docs_by_status(DocStatus.FAILED), timeout=30
        )
    except Exception as e:
        logger.error(f"cleanup_failed_docs: failed to read doc_status: {e}")
        return counts

    if not failed_docs:
        return counts

    dup_ids: List[str] = []
    empty_ids: List[str] = []

    for doc_id, doc in failed_docs.items():
        if doc_id.startswith("dup-"):
            dup_ids.append(doc_id)
            continue

        is_empty = doc.content_length == 0
        if not is_empty:
            try:
                content_data = call_async(rag.full_docs.get_by_id(doc_id), timeout=10)
                if content_data is None:
                    is_empty = True
                elif not content_data.get("content"):
                    is_empty = True
            except Exception:
                pass

        if is_empty:
            empty_ids.append(doc_id)
        else:
            counts["real_failures"] += 1

    if dup_ids:
        try:
            call_async(rag.doc_status.delete(dup_ids), timeout=30)
            counts["dup_deleted"] = len(dup_ids)
            logger.info(f"cleanup_failed_docs: deleted {len(dup_ids)} dup- entries")
        except Exception as e:
            logger.error(f"cleanup_failed_docs: failed to delete dup- entries: {e}")

    if empty_ids:
        try:
            call_async(rag.doc_status.delete(empty_ids), timeout=30)
            counts["empty_deleted"] = len(empty_ids)
            logger.info(f"cleanup_failed_docs: deleted {len(empty_ids)} empty-content entries")
        except Exception as e:
            logger.error(f"cleanup_failed_docs: failed to delete empty-content entries: {e}")

    return counts


# ============== Monitor Logic ==============

def get_pipeline_status(rag) -> Dict[str, Any]:
    """Read pipeline status from LightRAG's shared memory."""
    try:
        from lightrag.kg.shared_storage import _shared_dicts, get_final_namespace

        ps_key = get_final_namespace("pipeline_status", rag.workspace)
        ps = _shared_dicts.get(ps_key)
        if ps is None:
            return {"busy": False, "progress": 0, "message": "pipeline_status not initialized"}
    except Exception as e:
        return {"busy": False, "progress": 0, "message": f"Error: {e}"}

    busy = bool(ps.get("busy", False))
    cur_batch = int(ps.get("cur_batch", 0))
    batchs = int(ps.get("batchs", 0))
    job_name = str(ps.get("job_name", ""))
    latest_message = str(ps.get("latest_message", ""))

    # Progress calculation (same as kg_api.py)
    if not busy:
        progress = 0
    elif batchs > 0:
        doc_base = (cur_batch - 1) / batchs * 100
        file_progress = _parse_file_progress(latest_message)
        if file_progress >= 0:
            progress = doc_base + file_progress / batchs
        else:
            progress = doc_base + 50 / batchs
        progress = min(int(progress), 99)
    else:
        progress = 1

    return {
        "busy": busy,
        "progress": progress,
        "cur_batch": cur_batch,
        "batchs": batchs,
        "job_name": job_name,
        "message": latest_message,
    }


def format_status_line(status: Dict[str, Any], start_time: Optional[float] = None) -> str:
    """Format a single-line status update."""
    timestamp = datetime.now().strftime("%H:%M:%S")

    if not status["busy"]:
        if status["progress"] == 0 and "Completed" not in status["message"]:
            return f"[{timestamp}] Idle"
        elapsed = ""
        if start_time is not None:
            elapsed_sec = int(time.time() - start_time)
            elapsed = f" in {elapsed_sec}s"
        return f"[{timestamp}] Done -- {status['batchs']} file(s) processed{elapsed}"

    progress = status["progress"]
    cur_batch = status["cur_batch"]
    batchs = status["batchs"]
    message = status["message"]

    # Truncate long messages
    if len(message) > 60:
        message = message[:57] + "..."

    batch_str = f"({cur_batch}/{batchs})" if batchs > 0 else "(?/?)"
    return f"[{timestamp}] >> {progress:3d}% {batch_str} {message}"


def monitor_loop(once: bool = False):
    """Main monitoring loop."""
    rag = get_lightrag()
    if rag is None:
        print("ERROR: LightRAG not available. Is the niu_api server running?")
        sys.exit(1)

    print("Monitoring LightRAG pipeline status...")
    print("Press Ctrl+C to stop\n")

    prev_busy = False
    start_time: Optional[float] = None
    cleanup_done = False

    try:
        while True:
            status = get_pipeline_status(rag)
            busy = status["busy"]

            # Detect pipeline start
            if busy and not prev_busy:
                start_time = time.time()
                cleanup_done = False
                print(f"\n--- Pipeline started: {status['job_name']} ---")

            # Run cleanup once when pipeline starts
            if busy and not cleanup_done:
                try:
                    result = _cleanup_failed_docs(rag)
                    if result["dup_deleted"] > 0 or result["empty_deleted"] > 0:
                        logger.info(
                            f"Cleanup: {result['dup_deleted']} dup, "
                            f"{result['empty_deleted']} empty deleted"
                        )
                except Exception as e:
                    logger.warning(f"Cleanup failed (non-fatal): {e}")
                cleanup_done = True

            # Print status line
            print(format_status_line(status, start_time), end="\r", flush=True)

            # Detect pipeline completion
            if not busy and prev_busy:
                print()  # New line after the status
                print(f"--- Pipeline completed ---")
                if start_time is not None:
                    elapsed = int(time.time() - start_time)
                    print(f"    Total time: {elapsed}s")
                print()
                start_time = None

            prev_busy = busy

            if once:
                print()  # New line for clean exit
                break

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Monitor LightRAG ingestion pipeline progress"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (useful for scripting)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuous polling (default, explicit flag)",
    )
    args = parser.parse_args()

    # --watch is the default, --once overrides
    monitor_loop(once=args.once)


if __name__ == "__main__":
    main()
