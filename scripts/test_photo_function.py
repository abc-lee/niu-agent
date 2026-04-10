"""Test if photo-server functions actually execute"""
import sys
import time
from pathlib import Path

# Fix encoding
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Add paths
sys.path.insert(0, str(Path.cwd()))

# Load MCP config
import yaml
config_path = Path('config/mcp-servers.yaml')
with open(config_path, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Add workdirs
for server_name, server_config in config.items():
    if isinstance(server_config, dict) and 'workdir' in server_config:
        workdir = (Path.cwd() / server_config['workdir']).resolve()
        if workdir.exists():
            sys.path.insert(0, str(workdir))

# Import photo-server
import niu_photo_server

# Test 1: Check functions exist
print("=== Function check ===")
print(f"ingest_photo exists: {hasattr(niu_photo_server, 'ingest_photo')}")
print(f"get_face_model exists: {hasattr(niu_photo_server, 'get_face_model')}")
print(f"detect_faces exists: {hasattr(niu_photo_server, 'detect_faces')}")

# Test 2: Call ingest_photo with non-existent file (should fail fast)
print("\n=== Test: ingest_photo with non-existent file ===")
func = getattr(niu_photo_server, 'ingest_photo')
start = time.time()
try:
    result = func(file_path='E:/tmp/nonexistent_test.jpg', category='test')
    elapsed = time.time() - start
    print(f"Execution time: {elapsed:.3f}s")
    print(f"Status: {result.get('status')}")
    print(f"Error code: {result.get('error_code')}")
except Exception as e:
    elapsed = time.time() - start
    print(f"Exception time: {elapsed:.3f}s")
    print(f"Exception: {type(e).__name__}: {e}")

# Test 3: Check if there's a mock or stub
print("\n=== Function signature check ===")
import inspect
sig = inspect.signature(func)
print(f"Signature: {sig}")
print(f"Module: {func.__module__}")
print(f"Name: {func.__name__}")

# Test 4: Call detect_faces directly
print("\n=== Test: detect_faces with non-existent file ===")
detect_func = getattr(niu_photo_server, 'detect_faces')
start = time.time()
try:
    result = detect_func('E:/tmp/nonexistent.jpg')
    elapsed = time.time() - start
    print(f"Execution time: {elapsed:.3f}s")
    print(f"Result: {result}")
except Exception as e:
    elapsed = time.time() - start
    print(f"Exception time: {elapsed:.3f}s")
    print(f"Exception: {type(e).__name__}: {e}")
