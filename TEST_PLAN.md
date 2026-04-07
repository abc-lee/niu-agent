# 功能测试方案（P0/P1/P2）

## 一、测试环境准备

### 1.1 测试环境设置

**依赖安装**:
```bash
# 核心测试框架
pip install pytest pytest-asyncio pytest-cov pytest-timeout

# Mock 和测试工具
pip install pytest-mock aioresponses

# 性能测试
pip install locust memory_profiler

# 类型检查
pip install mypy
```

**数据库初始化**:
```bash
# 创建测试数据库
export TEST_DB_PATH=/tmp/test_niu_messages.db
export TEST_TASKS_DB=/tmp/test_scheduled_tasks.db

# 清理旧测试数据
rm -f $TEST_DB_PATH $TEST_TASKS_DB
```

**测试数据准备**:
```python
# scripts/prepare_test_data.py
"""准备测试数据"""
import asyncio
import sys
sys.path.insert(0, "E:/tools/ai-bot")

from agent.session import MessageStore

async def prepare_test_data():
    """创建测试对话历史"""
    store = await MessageStore.create("/tmp/test_niu_messages.db")

    # 创建 60 轮对话
    for i in range(60):
        await store.add_message(role="user", content=f"User message {i}")
        await store.add_message(role="assistant", content=f"Assistant response {i}")

    print("✓ Created 60 rounds of test conversation")
    return store

if __name__ == "__main__":
    asyncio.run(prepare_test_data())
```

### 1.2 测试工具配置

**pytest 配置** (`E:\tools\ai-bot\pytest.ini`):
```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
timeout = 30
addopts =
    -v
    --tb=short
    --cov=agent
    --cov=niu_api
    --cov-report=html
    --cov-report=term-missing
markers =
    p0: P0 critical tests
    p1: P1 optimization tests
    p2: P2 enhancement tests
    slow: slow running tests
```

**测试目录结构**:
```
E:/tools/ai-bot/tests/
├── conftest.py                 # pytest fixtures
├── test_p0/
│   ├── test_llmcore_fixes.py
│   ├── test_agent_loop.py
│   ├── test_compat.py
│   └── test_handler.py
├── test_p1/
│   ├── test_context_manager.py
│   ├── test_experience_summarizer.py
│   └── test_mcp_retry.py
├── test_p2/
│   ├── test_tool_duplicate_detection.py
│   └── test_token_counter.py
├── integration/
│   ├── test_long_conversation.py
│   ├── test_tool_failure.py
│   └── test_concurrent_sessions.py
└── performance/
    ├── test_memory_usage.py
    └── test_load.py
```

---

## 二、P0 功能测试（6个修复项）

### P0-1: 清理 BaseSession.history 废弃字段

**测试目标**: 验证删除废弃字段后无副作用

**测试方法**: 单元测试 + 集成测试

**测试脚本**: `tests/test_p0/test_llmcore_fixes.py`

**关键测试点**:
- BaseSession 无 history 字段
- NativeClaudeSession 无 history 字段
- 其他字段初始化正常
- 对话功能正常

**运行命令**:
```bash
pytest tests/test_p0/test_llmcore_fixes.py::TestHistoryFieldCleanup -v -m p0
```

**验证标准**:
- [ ] 测试通过：BaseSession 无 history 字段
- [ ] 测试通过：NativeClaudeSession 无 history 字段
- [ ] 无 AttributeError 异常

---

### P0-2: MessageStore 排序逻辑

**测试目标**: 验证 `get_messages(limit=N)` 返回最新 N 条消息

**测试方法**: 单元测试

**测试脚本**: `tests/test_p0/test_session.py`

**关键测试点**:
- `limit=5` 返回最新 5 条消息
- 消息按时间顺序排列（最旧在上）
- `limit=None` 返回所有消息

**运行命令**:
```bash
pytest tests/test_p0/test_session.py::TestMessageStoreSorting -v -m p0
```

**验证标准**:
- [ ] 返回最新 N 条消息
- [ ] 时间顺序正确
- [ ] SQL DESC + reverse() 逻辑正确

---

### P0-3: 上下文长度限制

**测试目标**: 验证历史加载限制在 50 条

**测试方法**: 集成测试

**测试脚本**: `tests/test_p0/test_compat.py`

**关键测试点**:
- 60 轮对话只加载 50 条
- Token 数量在合理范围
- 对话功能正常

**运行命令**:
```bash
pytest tests/test_p0/test_compat.py::TestContextLengthLimit -v -m p0
```

**验证标准**:
- [ ] 限制生效
- [ ] Token 数量 < 20K
- [ ] 日志正确

---

### P0-4: 替换 assert 为类型检查

**测试目标**: 验证类型检查在 -O 优化模式下仍有效

**测试方法**: 单元测试

**测试脚本**: `tests/test_p0/test_llmcore_fixes.py`

**关键测试点**:
- 传入字符串抛出 TypeError
- 传入 list 抛出 TypeError
- 传入 None 抛出 TypeError
- 错误信息清晰

**运行命令**:
```bash
pytest tests/test_p0/test_llmcore_fixes.py::TestTypeValidation -v -m p0
```

**验证标准**:
- [ ] 类型检查生效
- [ ] 错误信息包含期望类型
- [ ] -O 模式仍有效

---

### P0-5: 初始化 content_blocks（最关键）

**测试目标**: 验证修复后的 ask 方法能正确处理生成器返回值

**测试方法**: 单元测试 + 集成测试

**测试脚本**: `tests/test_p0/test_llmcore_fixes.py`

**关键测试点**:
- ask 方法不抛出 AttributeError
- 正确提取生成器返回值
- 处理空返回值
- 处理 None 返回值
- 工具调用正确提取

**运行命令**:
```bash
pytest tests/test_p0/test_llmcore_fixes.py::TestContentBlocksInitialization -v -m p0
```

**验证标准**:
- [ ] 无 AttributeError
- [ ] 生成器正确处理
- [ ] 工具调用提取正常

---

### P0-6: 添加 JSON 解析异常处理

**测试目标**: 验证 LLM 返回非法 JSON 时不会崩溃

**测试方法**: 单元测试

**测试脚本**: `tests/test_p0/test_agent_loop.py`

**关键测试点**:
- 非法 JSON 不导致崩溃
- 回退为空 dict 参数
- 记录错误信息
- 后续流程继续执行

**运行命令**:
```bash
pytest tests/test_p0/test_agent_loop.py::TestToolCallJSONParsing -v -m p0
```

**验证标准**:
- [ ] 不崩溃
- [ ] 回退机制生效
- [ ] 错误被记录

---

### P0-7: 使用 with 管理数据库连接

**测试目标**: 验证数据库连接在异常时正确关闭

**测试方法**: 单元测试

**测试脚本**: `tests/test_p0/test_handler.py`

**关键测试点**:
- with 语句正确使用
- 异常时连接关闭
- 无资源泄露
- 错误信息正确记录

**运行命令**:
```bash
pytest tests/test_p0/test_handler.py::TestDatabaseConnectionManagement -v -m p0
```

**验证标准**:
- [ ] with 语句使用
- [ ] 异常时关闭
- [ ] 无资源泄露

---

## 三、P1 功能测试（4个优化项）

### P1-1: ContextManager 统一历史管理

**测试目标**: 验证 ContextManager 正确加载和压缩历史

**测试方法**: 单元测试

**测试脚本**: `tests/test_p1/test_context_manager.py`

**验证标准**:
- [ ] ContextManager 创建成功
- [ ] 历史加载正常
- [ ] 压缩逻辑正确
- [ ] Token 计数准确

---

### P1-2: ExperienceSummarizer 集成

**测试目标**: 验证经验总结功能集成

**测试方法**: 集成测试

**测试脚本**: `tests/test_p1/test_experience_summarizer.py`

**验证标准**:
- [ ] 初始化成功
- [ ] 触发条件正确
- [ ] Skill 文件生成

---

### P1-3: MCP 工具调用重试机制

**测试目标**: 验证工具调用失败后自动重试

**测试方法**: 单元测试

**测试脚本**: `tests/test_p1/test_mcp_retry.py`

**验证标准**:
- [ ] 重试机制生效
- [ ] 最大重试次数正确
- [ ] 指数退避实现

---

## 四、P2 功能测试（3个完善项）

### P2-1: 工具重复调用检测

**测试目标**: 验证检测到重复工具调用并发出警告

**测试方法**: 集成测试

**测试脚本**: `tests/test_p2/test_tool_duplicate_detection.py`

**验证标准**:
- [ ] 检测重复调用
- [ ] 警告信息生成
- [ ] 不影响流程

---

### P2-2: AutonomousExplorer 启动

**测试目标**: 验证自主探索器正确启动

**测试方法**: 集成测试

**测试脚本**: `tests/test_p2/test_autonomous_explorer.py`

**验证标准**:
- [ ] 初始化成功
- [ ] 空闲检测生效
- [ ] 反思模式触发

---

### P2-3: Token 估算实现

**测试目标**: 验证 token 计数准确

**测试方法**: 单元测试

**测试脚本**: `tests/test_p2/test_token_counter.py`

**验证标准**:
- [ ] 计数准确
- [ ] 支持多种模型
- [ ] 性能可接受

---

## 五、自动化测试脚本

### 5.1 快速验证脚本

**脚本**: `E:\tools\ai-bot\quick_verify.py`

一键验证所有 P0 修复。

**运行命令**:
```bash
python quick_verify.py
```

---

### 5.2 完整测试套件

**Windows 脚本**: `E:\tools\ai-bot\run_all_tests.bat`

**Linux/Mac 脚本**: `E:\tools\ai-bot\run_all_tests.sh`

一键运行所有测试（P0/P1/P2/集成测试）。

**运行命令**:
```bash
# Windows
run_all_tests.bat

# Linux/Mac
./run_all_tests.sh
```

---

### 5.3 测试报告生成

**脚本**: `E:\tools\ai-bot\generate_test_report.py`

生成 HTML 测试报告和覆盖率报告。

**输出**:
- HTML 报告: `test_report.html`
- 覆盖率报告: `coverage_report/index.html`
- JUnit XML: `junit_report.xml`

---

## 六、测试覆盖率目标

| 优先级 | 覆盖率目标 | 说明 |
|--------|-----------|------|
| P0 | 100% | 所有关键修复必须有测试 |
| P1 | 80% | 优化功能主要场景覆盖 |
| P2 | 60% | 完善功能基本覆盖 |

**覆盖率检查脚本**: `E:\tools\ai-bot\check_coverage.py`

---

## 七、测试执行流程

### 推荐执行顺序

```
1️⃣ P0 快速验证
   python quick_verify.py

2️⃣ P0 完整测试
   pytest tests/test_p0/ -v -m p0

3️⃣ P1 优化测试
   pytest tests/test_p1/ -v -m p1

4️⃣ P2 完善测试
   pytest tests/test_p2/ -v -m p2

5️⃣ 集成测试
   pytest tests/integration/ -v

6️⃣ 覆盖率检查
   python check_coverage.py

7️⃣ 完整报告
   python generate_test_report.py
```

---

## 八、测试检查清单

### P0 修复验证清单

- [ ] **P0-1**: History 字段已删除
  - [ ] BaseSession 无 history 属性
  - [ ] NativeClaudeSession 无 history 属性
  - [ ] 对话功能正常

- [ ] **P0-2**: MessageStore 排序正确
  - [ ] `limit=N` 返回最新 N 条
  - [ ] 按时间顺序排列

- [ ] **P0-3**: 上下文限制生效
  - [ ] 60 轮对话加载 50 条
  - [ ] Token 数量合理

- [ ] **P0-4**: 类型检查生效
  - [ ] 非 dict 抛出 TypeError
  - [ ] 错误信息清晰

- [ ] **P0-5**: content_blocks 初始化
  - [ ] 无 AttributeError
  - [ ] 生成器正确处理

- [ ] **P0-6**: JSON 解析异常处理
  - [ ] 非法 JSON 不崩溃
  - [ ] 回退为空 dict

- [ ] **P0-7**: 数据库连接管理
  - [ ] with 语句使用
  - [ ] 异常时关闭

---

## 九、关键文件列表

### 需要创建的测试文件

**P0 测试**:
- `tests/test_p0/test_llmcore_fixes.py` - P0-1, P0-4, P0-5
- `tests/test_p0/test_session.py` - P0-2
- `tests/test_p0/test_compat.py` - P0-3
- `tests/test_p0/test_agent_loop.py` - P0-6
- `tests/test_p0/test_handler.py` - P0-7

**P1 测试**:
- `tests/test_p1/test_context_manager.py`
- `tests/test_p1/test_experience_summarizer.py`
- `tests/test_p1/test_mcp_retry.py`

**P2 测试**:
- `tests/test_p2/test_tool_duplicate_detection.py`
- `tests/test_p2/test_autonomous_explorer.py`
- `tests/test_p2/test_token_counter.py`

**集成测试**:
- `tests/integration/test_long_conversation.py`
- `tests/integration/test_tool_failure.py`
- `tests/integration/test_concurrent_sessions.py`

**配置文件**:
- `pytest.ini` - pytest 配置
- `tests/conftest.py` - pytest fixtures

**自动化脚本**:
- `quick_verify.py` - 快速验证
- `run_all_tests.bat` - Windows 完整测试
- `run_all_tests.sh` - Linux/Mac 完整测试
- `generate_test_report.py` - 报告生成
- `check_coverage.py` - 覆盖率检查

---

## 十、执行计划

**Phase 1**: 创建测试框架（30 分钟）
- 创建测试目录结构
- 编写 pytest.ini 和 conftest.py
- 准备测试数据脚本

**Phase 2**: 编写 P0 测试（2 小时）
- 每个修复项单独测试文件
- 确保每个测试可独立运行

**Phase 3**: 编写 P1/P2 测试（1.5 小时）
- 根据实施进度逐步添加

**Phase 4**: 编写自动化脚本（30 分钟）
- 快速验证
- 完整测试套件
- 报告生成

**Phase 5**: 执行测试并修复问题（1 小时）
- 运行所有测试
- 修复失败的测试
- 生成报告

**总预计时间**: 5.5 小时
