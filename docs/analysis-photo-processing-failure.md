# 照片拖入间歇性失败分析

## 执行摘要

**问题**：用户拖入照片时，可能成功也可能失败，行为不确定。

**根本原因假设**：GPU/CUDA 资源竞争或模型生命周期管理问题。

**验证状态**：待验证（需要查看实际失败日志）。

---

## Phase 1: Root Cause Investigation - 发现的潜在问题

### 1. ONNX Runtime stdout 污染（部分解决）

**现象**：
- 测试输出显示大量 ONNX Runtime 信息输出到 stdout
- 包括：Applied providers, find model, det-size 等

**代码中的处理**：
- `photo-server/__init__.py:691` 有 `suppress_stdout()` 上下文管理器
- 但只包裹了 `FaceAnalysis()` 创建过程
- 模型加载后的推理过程可能仍有输出

**潜在影响**：
- ~~如果使用 MCP stdio 通信，会污染协议~~ → 已废弃
- 新架构是同进程调用，影响较小
- 但仍可能导致日志混乱

**证据**：
测试中模型加载有大量 stdout 输出，但工具调用仍然成功

---

### 2. CUDA/GPU 资源竞争（**高风险**）

**现象**：
- 测试显示使用了 `CUDAExecutionProvider`（GPU加速）
- InsightFace 模型占用 ~326MB 内存
- GPU 内存是有限资源

**潜在冲突**：
- 同时处理多张照片 → GPU 内存不足
- 其他进程使用 GPU → 冲突
- 特定照片触发 GPU 错误（尺寸、格式）

**证据**：
```
Applied providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']
ctx_id=0  # GPU模式
```

**可能的失败模式**：
```
第1次调用 → 成功（模型加载到GPU）
第2次调用 → 可能失败（GPU内存不足或CUDA错误）
等待5分钟 → 成功（模型已卸载，重新加载）
```

---

### 3. 模型生命周期管理问题（**中风险**）

**代码逻辑**：
```python
_face_model = None  # 全局模型实例
_last_model_use_time = None
MODEL_IDLE_TIMEOUT_SECONDS = 300  # 5分钟自动卸载

def get_face_model():
    if _face_model is None:
        _face_model = FaceAnalysis(...)  # 加载模型
    _last_model_use_time = datetime.now()
    return _face_model

# 后台线程每60秒检查一次，空闲超时则卸载
def check_and_unload():
    if idle_seconds > 300:
        _face_model = None
```

**潜在的竞争条件**：
```
线程A: 工具调用        线程B: 卸载检查
    |                      |
    | 获取 _face_model     |
    | 开始推理...          |
    |          ←           _face_model = None
    | 继续使用已释放的模型 → 错误！
```

**证据**：
- 代码中有后台线程定期卸载（`__init__.py:586`）
- 卸载时不调用 `gc.collect()`（避免崩溃）

---

### 4. 异步/同步混用残留（**低风险**）

**检查结果**：
- `handler.py:1062` 是同步调用：`result = func(**args)`
- photo-server 工具函数都是同步的
- 无 `asyncio.to_thread` 或 `await` 残留

**结论**：新架构已完全改为同步调用，无冲突

---

### 5. 文件路径和权限问题（**中风险**）

**潜在问题**：
- 照片路径包含中文（如"西柏坡"）
- Windows 中文路径编码问题
- 文件被其他进程锁定

**代码中的处理**：
```python
# photo-server/__init__.py:1279-1287
# 使用 numpy 读取文件，解决中文路径问题
with open(file_path, "rb") as f:
    img_bytes = np.frombuffer(f.read(), dtype=np.uint8)
img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
```

**证据**：
- 用户照片路径：`E:/tmp/2009.6.4西柏坡/DSC_3285.jpg`
- 代码已有专门处理中文路径的逻辑
- 问题应该已解决

---

## Phase 2: Pattern Analysis - 最可能的根本原因

### **假设 1：GPU/CUDA 资源竞争**

**推理**：
1. "可能成功也可能失败" → 非确定性行为
2. 非确定性行为通常由资源竞争或状态竞争导致
3. GPU 是有限资源，多个调用可能导致竞争

**失败场景重现**：

| 场景 | 操作 | GPU状态 | 结果 |
|------|------|---------|------|
| A | 第1张照片 | 模型加载到GPU | ✓ 成功 |
| B | 快速拖入第2张 | GPU被占用 | ✗ 可能失败 |
| C | 等待5分钟后 | 模型已卸载 | ✓ 成功 |

**验证方法**：
- 查看失败时的错误日志
- 检查是否有 CUDA 错误或 GPU 内存不足
- 监控 GPU 使用情况

---

### **假设 2：模型卸载竞争条件**

**推理**：
- 后台线程每60秒检查一次模型空闲时间
- 如果空闲超过5分钟，执行 `_face_model = None`
- 工具调用可能在卸载过程中访问 `_face_model`

**失败场景重现**：

```
时间轴：
T0: 用户处理了照片A → _face_model 加载
T1-T5: 用户未操作
T5: 后台线程开始卸载：_face_model = None
T5.001: 用户拖入照片B → 调用 get_face_model()
        → 如果在卸载过程中 → NoneType 错误
```

**验证方法**：
- 查看失败时的错误日志
- 检查是否有 "NoneType" 错误
- 添加线程同步机制

---

## Phase 3: Hypothesis and Testing - 下一步调试建议

### 1. 查看实际运行日志

**需要检查的日志**：
- `~/.niu/logs/` 目录下的日志
- API 的 stderr 输出（可能有 CUDA 错误）
- Go 启动器捕获的日志

**关键错误关键词**：
- `CUDA error`
- `GPU memory`
- `NoneType`
- `AttributeError`
- `insightface`
- `onnxruntime`

### 2. 添加详细的错误日志

**在关键位置添加日志**：
```python
# photo-server/__init__.py detect_faces()

def detect_faces(file_path: str) -> list[dict]:
    try:
        logger.info(f"[DETECT_FACES] Starting for: {file_path}")
        face_model = get_face_model()
        
        if face_model is None:
            logger.error("[DETECT_FACES] Face model is None!")
            return []
        
        logger.info(f"[DETECT_FACES] Model loaded, detecting...")
        # ... 后续逻辑
        
    except Exception as e:
        logger.exception(f"[DETECT_FACES] FAILED: {type(e).__name__}: {e}")
        # 关键：记录完整的异常堆栈
        import traceback
        logger.error(traceback.format_exc())
        return []
```

### 3. 测试 GPU 状态

**检查 GPU 是否正常**：
```python
import onnxruntime as ort

print("Available providers:", ort.get_available_providers())
print("CUDA available:", "CUDAExecutionProvider" in ort.get_available_providers())

# 尝试创建一个简单的 ONNX session 测试 GPU
try:
    sess = ort.InferenceSession(..., providers=['CUDAExecutionProvider'])
    print("GPU test passed")
except Exception as e:
    print(f"GPU test failed: {e}")
```

### 4. 临时禁用 GPU 加速（测试是否是 GPU 问题）

**修改 `photo-server/__init__.py`**：
```python
def get_face_model():
    # 临时改为强制使用 CPU
    providers = ["CPUExecutionProvider"]  # 强制CPU模式
    
    # 这样可以排除 GPU 问题
    # 如果禁用GPU后问题消失，则确定是GPU问题
```

### 5. 监控模型生命周期

**添加日志到模型加载/卸载**：
```python
# photo-server/__init__.py

def get_face_model():
    global _face_model, _last_model_use_time
    logger.info(f"[MODEL_LIFECYCLE] _face_model={_face_model is not None}")
    
    if _face_model is None:
        logger.info("[MODEL_LIFECYCLE] Loading model...")
        # ...
    else:
        logger.info("[MODEL_LIFECYCLE] Using cached model")

def unload_face_model():
    logger.info("[MODEL_LIFECYCLE] Unloading model...")
    # ...
```

---

## Phase 4: Implementation - 建议的修复方案

### 方案 1：添加线程锁保护模型生命周期

```python
import threading

_model_lock = threading.Lock()

def get_face_model():
    global _face_model, _last_model_use_time
    
    with _model_lock:  # 加锁
        if _face_model is None:
            # 加载模型...
        _last_model_use_time = datetime.now()
        return _face_model

def unload_face_model():
    global _face_model, _last_model_use_time
    
    with _model_lock:  # 加锁
        if _face_model is not None:
            _face_model = None
            _last_model_use_time = None
```

### 方案 2：添加 CUDA 错误处理和降级机制

```python
def get_face_model():
    global _face_model
    
    if _face_model is None:
        try:
            # 尝试加载GPU版本
            providers = _detect_available_providers()
            _face_model = FaceAnalysis(..., providers=providers)
            _face_model.prepare(ctx_id=0 if "CUDA" in providers else -1)
        except Exception as e:
            logger.warning(f"GPU加载失败，降级到CPU: {e}")
            # 降级到CPU
            _face_model = FaceAnalysis(..., providers=["CPUExecutionProvider"])
            _face_model.prepare(ctx_id=-1)
    
    return _face_model
```

### 方案 3：移除自动卸载机制（临时）

```python
# 注释掉后台卸载线程，避免竞争条件
# _start_model_unload_timer()

# 或者改为手动卸载
def unload_face_model():
    """仅在用户明确要求或应用退出时调用"""
    global _face_model
    _face_model = None
```

---

## 总结

**最可能的原因**：
1. **GPU/CUDA 资源竞争**（70%可能性）
2. **模型卸载竞争条件**（20%可能性）
3. **其他因素**（10%可能性）

**下一步行动**：
1. 查看实际失败日志（最关键）
2. 添加详细错误日志
3. 测试GPU稳定性
4. 临时禁用GPU验证假设
5. 根据验证结果选择修复方案

**预期结果**：
- 如果是GPU问题 → 方案2（错误处理+降级）
- 如果是竞争条件 → 方案1（加锁）
- 如果两者都有 → 方案1+2组合
