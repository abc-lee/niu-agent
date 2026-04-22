"""
Re-index Vector Store

Re-encodes all document embeddings using the current embedding model.
Use after switching embedding models (e.g., minilm-l12 → bge-m3) when
the vector dimension changes.

Usage:
    python scripts/reindex_vectors.py [--dry-run] [--batch-size 50]
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def find_vectors_db() -> Path:
    """Find vectors.db path."""
    # Try WORKSPACE_PATH env
    import os
    if "WORKSPACE_PATH" in os.environ:
        return Path(os.environ["WORKSPACE_PATH"]) / "vectors.db"

    # Try memory.json
    mp = Path.home() / ".niu" / "memory.json"
    if mp.exists():
        mem = json.loads(mp.read_text(encoding="utf-8"))
        wp = mem.get("workspace", {}).get("path", "")
        if wp and Path(wp).exists():
            return Path(wp) / "vectors.db"

    raise FileNotFoundError("Cannot find vectors.db. Set WORKSPACE_PATH or check memory.json")


def main():
    parser = argparse.ArgumentParser(description="Re-index vector store embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without modifying")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for encoding")
    args = parser.parse_args()

    db_path = find_vectors_db()
    if not db_path.exists():
        print(f"ERROR: vectors.db not found at {db_path}")
        sys.exit(1)

    # Import embedding module
    from niu_api.internal.embedding import get_model, get_embedding_dim, get_current_model_info

    model_info = get_current_model_info()
    target_dim = get_embedding_dim()

    print(f"Embedding model: {model_info['name']} ({target_dim}d)")
    print(f"Database: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    # Count documents
    cursor.execute("SELECT COUNT(*) FROM documents")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM documents WHERE embedding IS NOT NULL")
    with_emb = cursor.fetchone()[0]

    print(f"Documents: {total} total, {with_emb} with embeddings")

    if total == 0:
        print("No documents to re-index.")
        conn.close()
        return

    if args.dry_run:
        # Check current embedding dimensions
        cursor.execute("SELECT id, embedding FROM documents WHERE embedding IS NOT NULL LIMIT 1")
        row = cursor.fetchone()
        if row and row[1]:
            import numpy as np
            current_dim = len(np.frombuffer(row[1], dtype=np.float32))
            print(f"Current embedding dim: {current_dim}")
            print(f"Target embedding dim: {target_dim}")
            if current_dim == target_dim:
                print("Dimensions match. Re-indexing not strictly needed (but may improve quality).")
            else:
                print(f"Dimension change: {current_dim} → {target_dim}. Re-indexing REQUIRED.")
        print(f"\n[DRY RUN] Would re-index {with_emb} documents")
        conn.close()
        return

    # Force load model
    print("Loading embedding model...")
    model = get_model()

    # Re-index in batches
    cursor.execute("SELECT id, content FROM documents ORDER BY id")
    rows = cursor.fetchall()

    reindexed = 0
    errors = 0
    start_time = time.time()

    import numpy as np

    for i in range(0, len(rows), args.batch_size):
        batch = rows[i : i + args.batch_size]
        ids = [r[0] for r in batch]
        contents = [r[1] for r in batch]

        try:
            embeddings = model.encode(contents, convert_to_numpy=True)
            for doc_id, embedding in zip(ids, embeddings):
                blob = embedding.astype(np.float32).tobytes()
                conn.execute(
                    "UPDATE documents SET embedding = ? WHERE id = ?",
                    (blob, doc_id),
                )
            conn.commit()
            reindexed += len(batch)
            elapsed = time.time() - start_time
            rate = reindexed / elapsed if elapsed > 0 else 0
            print(f"  {reindexed}/{len(rows)} ({rate:.0f} docs/s)")
        except Exception as e:
            errors += len(batch)
            print(f"  ERROR on batch {i}: {e}")

    conn.close()
    elapsed = time.time() - start_time
    print(f"\nDone: {reindexed} re-indexed, {errors} errors, {elapsed:.1f}s")
    print(f"New embedding dimension: {target_dim}")


if __name__ == "__main__":
    main()
