# LiteLLM Integration Plan

**Status**: Draft
**Created**: 2026-04-08
**Author**: Claude Code
**Priority**: High

---

## Context

### Problem Statement

The current LLM integration layer has critical compatibility issues with different providers:

1. **MiniMax returns empty JSON `{}`** - The regex-based response parser in `agent/generic/llmcore.py` fails when MiniMax returns malformed responses
2. **No unified token tracking** - Need to implement compression feature, but lack standardized `prompt_tokens`, `completion_tokens`, `total_tokens` across providers
3. **Provider-specific quirks** - Manual handling of temperature constraints, message formats, streaming events
4. **High maintenance cost** - Adding new models requires extensive code changes

**User's Key Insight**:
> "This is not about model quality, it's about system compatibility. If Claude Code works fine with MiniMax, why doesn't our system?"

**Root Cause**: Application layer directly parses LLM responses with regex, lacking a standardized response format adapter.

### Proposed Solution

Integrate **LiteLLM** as a unified LLM interface layer:
- Supports 100+ providers (MiniMax, GLM, Claude, OpenAI, DeepSeek, etc.)
- Standardizes responses to OpenAI format
- Built-in token tracking and reasoning separation
- Unified tool calling format
- Built-in retry and fallback mechanisms

---

## Architecture Impact Analysis

### 1. LLM Calling Layer (`agent/generic/llmcore.py`)

**Current State** (1458 lines):
- Multiple Session types: `ClaudeSession`, `LLMSession`, `NativeClaudeSession`, `NativeOAISession`
- Regex parsing in `_parse_mixed_response()` (Lines 1067-1143)
- Provider-specific SSE parsers
- Manual message format conversion `_msgs_claude2oai()`

**Key Logic to Preserve**:
- ✅ Thinking chain separation (`<thinking>`, `<summary>` tags)
- ✅ Tool result format in messages
- ✅ Prompt caching (Claude/OpenAI)
- ✅ History compression (`compress_history_tags()`)

**What LiteLLM Replaces**:
- 🔄 Session classes → LiteLLM unified interface
- 🔄 SSE parsing → LiteLLM internal handling
- 🔄 Message format conversion → LiteLLM auto-detection
- 🔄 Provider-specific quirks → LiteLLM adapters

**Breaking Points**:
- 🔴 Tool call format mismatch (MockToolCall vs LiteLLM format)
- 🔴 Thinking tags may be lost
- 🟡 Streaming response structure changes
- 🟡 Error handling format differences

### 2. Agent Loop Mechanism (`agent/generic/agent_loop.py`)

**Current Flow**:
```
Initialize messages
  ↓
Loop (turn < max_turns):
  Call LLM → Parse tool_calls → Dispatch tools
  ↓
Check exit: should_exit / next_prompt empty / TASK_DONE
  ↓
Build next message (tool_results)
  ↓
Continue loop
```

**Must Preserve**:
- ✅ `StepOutcome` structure (controls loop continuation)
- ✅ `next_prompt` and `should_exit` logic
- ✅ Working memory injection (every turn)
- ✅ Safety mechanisms (35-turn limit, repeated tool detection)

**Critical Code** (Lines 118-140):
```python
for tc in response.tool_calls:
    args = json.loads(tc.function.arguments)  # Must be valid JSON
    tool_calls.append({"tool_name": tc.function.name, "args": args})
```

**Impact**:
- 🔴 Tool call parsing must adapt to LiteLLM format
- 🟡 Message format conversion needed (custom `tool_results` field)

### 3. Self-Evolution System

**Components**:
1. Working memory (`update_working_checkpoint` tool)
2. Long-term memory (`start_long_term_update` tool)
3. Auto skill generation (`ExperienceSummarizer`)
4. Dynamic injection (`_inject_dynamic_resources()`)
5. Tool execution tracking (`tool_after_callback`)

**Good News**:
- ✅ Dynamic injection - pure vector search, no LLM dependency
- ✅ Auto skill generation - tracks tool calls, doesn't parse LLM responses
- ✅ Vector search - uses embeddings, independent of LLM format

**Dependencies**:
- ⚠️ Tool calling mechanism must work
- ⚠️ `<summary>` tags expected but have fallback
- ⚠️ Memory tools must be callable

**Overall Impact**: **Minimal** - System is well-decoupled from LLM response formats.

---

## Risk Assessment

| Risk | Impact | Severity | Probability | Mitigation |
|------|--------|----------|-------------|------------|
| Tool call format mismatch | Agent loop breaks | Critical | High | Format adapter layer |
| Thinking tags lost | Working memory degrades | High | Medium | Post-processing injection |
| Message format incompatible | LLM calls fail | Critical | High | Conversion functions |
| Streaming interruption | UI freezes | High | Medium | Enhanced error handling |
| Prompt caching fails | Cost increases | Medium | Low | Accept cost increase |
| Memory tools unreachable | Evolution disabled | High | Low | Tool call adapter |

---

## Implementation Plan

### Phase 1: Compatibility Layer (2-3 days)

**Goal**: Create LiteLLM adapter without affecting existing code

**Key Files**:
- `agent/generic/litellm_adapter.py` (NEW)
- `agent/runner.py` (minimal changes)
- `config/user-config.json`

**Implementation Steps**:

1. **Create LiteLLMSession class**
   ```python
   # agent/generic/litellm_adapter.py

   import litellm
   from .llmcore import BaseSession, MockResponse, MockToolCall

   class LiteLLMSession(BaseSession):
       """LiteLLM adapter session"""

       def raw_ask(self, prompt, model=None, **kwargs):
           # 1. Parse protocol prompt to standard messages
           messages = self._parse_protocol_prompt(prompt)

           # 2. Call LiteLLM
           response = litellm.completion(
               model=self._get_litellm_model(),
               messages=messages,
               tools=kwargs.get("tools"),
               stream=True,
               **self._get_provider_params()
           )

           # 3. Convert response to MockResponse
           return self._convert_response(response, stream=True)

       def _parse_protocol_prompt(self, prompt: str) -> list:
           """Convert protocol prompt to standard messages"""
           # Extract === USER === and === ASSISTANT === sections
           # ... implementation ...

       def _convert_response(self, response, stream: bool):
           """Convert LiteLLM response to MockResponse"""
           if stream:
               for chunk in response:
                   delta = chunk.choices[0].delta
                   if delta.content:
                       yield delta.content
                   # Extract reasoning_content
                   if hasattr(delta, 'reasoning_content'):
                       yield f"<thinking>{delta.reasoning_content}</thinking>"
           else:
               return self._build_mock_response(response)

       def _build_mock_response(self, response) -> MockResponse:
           """Build MockResponse object"""
           choice = response.choices[0]

           # Extract thinking
           thinking = ""
           if hasattr(choice.message, 'reasoning_content'):
               thinking = choice.message.reasoning_content

           # Extract content
           content = choice.message.content or ""

           # Extract tool_calls
           tool_calls = []
           if choice.message.tool_calls:
               for tc in choice.message.tool_calls:
                   tool_calls.append(MockToolCall(
                       name=tc.function.name,
                       args=tc.function.arguments,
                       id=tc.id
                   ))

           # Extract usage
           usage = response.usage

           mock_resp = MockResponse(
               thinking=thinking,
               content=content,
               tool_calls=tool_calls
           )
           mock_resp.usage = {
               "prompt_tokens": usage.prompt_tokens,
               "completion_tokens": usage.completion_tokens,
               "total_tokens": usage.total_tokens
           }

           return mock_resp

       def _get_litellm_model(self) -> str:
           """Map model names to LiteLLM format"""
           MODEL_MAP = {
               "MiniMax-M2.7-highspeed": "minimax/MiniMax-M2.7-highspeed",
               "glm-5": "zhipu/glm-5",
               "claude-3-5-sonnet-20241022": "claude-3-5-sonnet-20241022",
           }
           return MODEL_MAP.get(self.default_model, self.default_model)

       def _get_provider_params(self) -> dict:
           """Get provider-specific parameters"""
           params = {}
           if "minimax" in self.default_model.lower():
               params["extra_body"] = {"reasoning_split": True}
           if "claude" in self.default_model.lower():
               params["extra_headers"] = {
                   "anthropic-beta": "prompt-caching-2024-07-31"
               }
           return params
   ```

2. **Modify create_client() function**
   ```python
   # agent/runner.py

   def create_client(config: Dict[str, Any]):
       """Create LLM client"""
       client_type = config.get("type", "openai")

       # NEW: Check if LiteLLM is enabled
       use_litellm = config.get("use_litellm", False)

       if use_litellm:
           from .generic.litellm_adapter import LiteLLMSession
           session = LiteLLMSession(config)
           return ToolClient(session)

       # Existing logic unchanged
       cfg = {
           "apikey": config.get("apikey") or config.get("api_key", ""),
           "apibase": config.get("apibase") or config.get("api_base", ""),
           "model": config.get("model", ""),
       }

       if client_type in ("native_claude", "native"):
           session = NativeClaudeSession(cfg)
           return NativeToolClient(session)
       # ... other branches ...
   ```

3. **Configuration support**
   ```json
   // config/user-config.json
   {
     "llm": {
       "presetId": "MiniMax-M2.7-highspeed",
       "apiKey": "sk-cp-...",
       "model": "MiniMax-M2.7-highspeed",
       "type": "anthropic",
       "use_litellm": false
     }
   }
   ```

**Verification**:
```bash
# Test LiteLLM adapter
python -c "
from agent.generic.litellm_adapter import LiteLLMSession

config = {
    'apikey': 'sk-cp-...',
    'model': 'MiniMax-M2.7-highspeed',
    'use_litellm': True
}
session = LiteLLMSession(config)
response = session.raw_ask('Hello')
print(response)
"
```

**Success Criteria**:
- ✅ LiteLLMSession successfully calls MiniMax
- ✅ Response converted to MockResponse
- ✅ Existing code unaffected (use_litellm=false)

### Phase 2: Tool Call Adapter (1-2 days)

**Goal**: Ensure tool calling mechanism works

**Key Files**:
- `agent/generic/agent_loop.py`
- `agent/generic/litellm_adapter.py`

**Implementation Steps**:

1. **Tool call format conversion**
   ```python
   # agent/generic/litellm_adapter.py

   def _format_tool_call(self, tc) -> str:
       """Convert LiteLLM tool_call to current XML format"""
       args_str = tc.function.arguments
       if isinstance(args_str, str):
           pass
       else:
           args_str = json.dumps(args_str, ensure_ascii=False)

       return f'<tool_use>{{"name": "{tc.function.name}", "arguments": {args_str}}}</tool_use>'
   ```

2. **Message format conversion**
   ```python
   # agent/generic/litellm_adapter.py

   def _to_litellm_messages(self, messages: list) -> list:
       """Convert custom format to LiteLLM standard format"""
       litellm_msgs = []

       for msg in messages:
           role = msg.get("role")
           content = msg.get("content")

           # Handle tool_results field
           tool_results = msg.get("tool_results")
           if tool_results:
               for tr in tool_results:
                   litellm_msgs.append({
                       "role": "tool",
                       "tool_call_id": tr.get("tool_use_id"),
                       "content": tr.get("content")
                   })

           litellm_msgs.append({
               "role": role,
               "content": content
           })

       return litellm_msgs
   ```

3. **Adapt agent_loop.py**
   ```python
   # agent/generic/agent_loop.py:118-140

   if not response.tool_calls:
       tool_calls = [{"tool_name": "no_tool", "args": {}}]
   else:
       for tc in response.tool_calls:
           # Check if already MockToolCall format
           if hasattr(tc, 'function') and hasattr(tc.function, 'arguments'):
               args_str = tc.function.arguments
           else:
               # LiteLLM format
               args_str = tc.get('function', {}).get('arguments', '{}')

           try:
               args = json.loads(args_str) if isinstance(args_str, str) else args_str
           except json.JSONDecodeError:
               args = {}

           tool_name = tc.function.name if hasattr(tc, 'function') else tc.get('function', {}).get('name')
           tool_calls.append({"tool_name": tool_name, "args": args})
   ```

**Verification**:
```bash
# Test tool calling
# 1. Enable LiteLLM
# 2. Send "5分钟后提醒我吃饭"
# 3. Check if schedule_task tool is called
# 4. Verify database task created
```

**Success Criteria**:
- ✅ LiteLLM tool calls parsed correctly
- ✅ Tools execute successfully
- ✅ StepOutcome format correct
- ✅ Agent loop continues/exits properly

### Phase 3: Thinking Chain & Tags (1 day)

**Goal**: Preserve `<thinking>` and `<summary>` tag functionality

**Key Files**:
- `agent/generic/litellm_adapter.py`
- `agent/handler.py`

**Implementation Steps**:

1. **Thinking chain extraction**
   ```python
   # agent/generic/litellm_adapter.py

   def _build_mock_response(self, response) -> MockResponse:
       choice = response.choices[0]

       # Extract reasoning_content
       thinking = ""
       if hasattr(choice.message, 'reasoning_content'):
           thinking = choice.message.reasoning_content
       elif hasattr(choice.message, 'reasoning_details'):
           thinking = choice.message.reasoning_details

       # Fallback: extract from content
       content = choice.message.content or ""
       if not thinking and "<thinking>" in content:
           import re
           think_match = re.search(r"<think(?:ing)?>(.*?)</think(?:ing)?>", content, re.DOTALL)
           if think_match:
               thinking = think_match.group(1).strip()
               content = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", content, flags=re.DOTALL)

       return MockResponse(
           thinking=thinking,
           content=content,
           tool_calls=tool_calls
       )
   ```

2. **Summary tag injection**
   ```python
   # agent/generic/litellm_adapter.py

   def _get_provider_params(self) -> dict:
       params = {}

       # Request <summary> tags in prompt
       params["extra_instructions"] = (
           "After each action, provide a brief summary in <summary> tags "
           "(max 30 characters) for working memory."
       )

       return params
   ```

3. **Enhanced fallback logic**
   ```python
   # agent/handler.py:246-250

   def tool_after_callback(self, tool_name, args, response, ret):
       content = getattr(response, "content", "") if response else ""

       # Try to extract <summary> tag
       rsumm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
       if rsumm:
           summary = rsumm.group(1).strip()[:200]
       else:
           # Fallback: auto-generate summary (existing logic)
           clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
           summary = f"调用工具{tool_name}, args: {clean_args}"

       self.history_info.append("[Agent] " + summary[:100])
   ```

**Verification**:
```bash
# Test thinking chain and summary
# 1. Enable LiteLLM
# 2. Execute multi-turn task
# 3. Check history_info contains summaries
# 4. Verify thinking extracted correctly
```

**Success Criteria**:
- ✅ Thinking chain content in `MockResponse.thinking`
- ✅ `<summary>` tags extracted (or fallback summary)
- ✅ Working memory injection contains correct summaries

### Phase 4: Token Tracking (0.5 days)

**Goal**: Enable LiteLLM token tracking for compression feature

**Key Files**:
- `agent/generic/litellm_adapter.py`
- `niu_api/routes/chat.py`

**Implementation Steps**:

1. **Extract token usage**
   ```python
   # agent/generic/litellm_adapter.py

   def _build_mock_response(self, response) -> MockResponse:
       # ... other logic ...

       usage = response.usage

       mock_resp = MockResponse(
           thinking=thinking,
           content=content,
           tool_calls=tool_calls
       )

       # NEW: usage attribute
       mock_resp.usage = {
           "prompt_tokens": usage.prompt_tokens,
           "completion_tokens": usage.completion_tokens,
           "total_tokens": usage.total_tokens,
           "cache_creation_tokens": getattr(usage.prompt_tokens_details, 'cached_tokens', 0),
           "reasoning_tokens": getattr(usage.completion_tokens_details, 'reasoning_tokens', 0)
       }

       return mock_resp
   ```

2. **Token tracking callback (optional)**
   ```python
   # agent/generic/litellm_adapter.py

   import litellm

   litellm.success_callback = ["log_token_usage"]

   def log_token_usage(kwargs, completion_response, start_time, end_time):
       model = kwargs.get("model")
       usage = completion_response.usage

       print(f"[TokenUsage] model={model} "
             f"prompt={usage.prompt_tokens} "
             f"completion={usage.completion_tokens} "
             f"total={usage.total_tokens}")
   ```

3. **Compression feature integration**
   ```python
   # niu_api/routes/chat.py

   def chat_endpoint():
       response = runner.chat(...)

       usage = response.usage
       if usage["prompt_tokens"] > CONTEXT_WINDOW * 0.8:
           trigger_compression(session_id)
   ```

**Verification**:
```bash
# Test token tracking
# 1. Enable LiteLLM
# 2. Send message
# 3. Check logs for [TokenUsage] records
# 4. Verify response.usage contains correct counts
```

**Success Criteria**:
- ✅ Every LLM call has token statistics
- ✅ Statistics format correct (prompt, completion, total)
- ✅ Can trigger compression feature

### Phase 5: Full Testing & Switch (2-3 days)

**Goal**: Validate all functionality, gradually switch models to LiteLLM

**Test Checklist**:

**Basic Features**:
- [ ] Normal conversation (no tools)
- [ ] Tool calling (single tool)
- [ ] Tool calling (multiple tools serial)
- [ ] Tool call failure recovery
- [ ] Streaming response
- [ ] Error handling (API errors, timeouts)

**Agent Loop**:
- [ ] Single-turn exit correctly
- [ ] Multi-turn tool loop (5-10 turns)
- [ ] 35-turn forced question
- [ ] Repeated tool detection
- [ ] next_prompt controls loop
- [ ] should_exit forces exit

**Self-Evolution**:
- [ ] Working memory update (`update_working_checkpoint`)
- [ ] Long-term memory storage (`start_long_term_update`)
- [ ] Auto skill generation (`ExperienceSummarizer`)
- [ ] Dynamic injection (`_inject_dynamic_resources`)
- [ ] Tool execution tracking (`<summary>` tags)

**Provider Compatibility**:
- [ ] MiniMax (problem model)
- [ ] GLM (Baidu)
- [ ] Claude (native)
- [ ] OpenAI
- [ ] DeepSeek (thinking chain model)

**Performance Comparison**:
- [ ] Response time (LiteLLM vs native)
- [ ] Token usage
- [ ] Error rate
- [ ] Cost (prompt caching effectiveness)

**Switch Strategy**:

1. **Grayscale rollout**:
   - First test in development environment
   - Run 1 week, monitor error rate
   - Gradually expand to production

2. **Model switch order**:
   - Week 1: MiniMax (problem model, priority)
   - Week 2: GLM (already in use)
   - Week 3: DeepSeek (thinking chain model)
   - Week 4: Claude, OpenAI

3. **Fallback mechanism**:
   ```json
   {
     "llm": {
       "use_litellm": true,
       "fallback_to_native": true
     }
   }
   ```

**Success Criteria**:
- ✅ All test cases pass
- ✅ Error rate <= native implementation
- ✅ Response time difference < 10%
- ✅ MiniMax empty JSON issue resolved
- ✅ Token statistics accurate for compression

### Phase 6: Cleanup & Optimization (1 week later)

**Goal**: Remove redundant code, optimize performance

**Cleanup Items**:
- Remove unused Session classes (keep one fallback)
- Remove deprecated logic in `_parse_mixed_response()`
- Remove `_msgs_claude2oai()` conversion function
- Remove provider-specific temperature adjustment code

**Optimization Directions**:
- Prompt caching optimization (leverage LiteLLM's cache mechanism)
- Concurrent request optimization (LiteLLM supports async)
- Token usage optimization (monitoring and quota management)

---

## Testing Strategy

### Functional Testing

#### Test Scenario 1: Basic Conversation

**Input**:
```
User: 你好，介绍一下你自己
```

**Expected Results**:
- ✅ LLM responds normally
- ✅ No tool calls
- ✅ Exits after single turn
- ✅ Token statistics accurate

#### Test Scenario 2: Tool Calling (Scheduled Task)

**Input**:
```
User: 5分钟后提醒我吃饭
```

**Expected Results**:
- ✅ LLM calls `schedule_task` tool
- ✅ Correct parameters: `{"delay_minutes": 5, "message": "吃饭"}`
- ✅ Database task created
- ✅ Agent returns confirmation

**Verification SQL**:
```sql
SELECT * FROM scheduled_tasks
WHERE message LIKE '%吃饭%'
ORDER BY scheduled_at DESC LIMIT 1;
```

#### Test Scenario 3: Multi-turn Tool Loop

**Input**:
```
User: 拖入照片 DSC_3335.jpg，分类：旅行
```

**Expected Results**:
- ✅ Calls `chat-with-file-processor` sub-agent
- ✅ Sub-agent calls `photo-server/ingest_photo`
- ✅ Tool results returned correctly
- ✅ Main agent summarizes results

#### Test Scenario 4: Thinking Chain Processing

**Input**:
```
User: 分析一下这个复杂问题...（使用DeepSeek R1模型）
```

**Expected Results**:
- ✅ `MockResponse.thinking` contains thinking chain
- ✅ `MockResponse.content` contains final answer
- ✅ Thinking chain not shown to user

#### Test Scenario 5: Self-Evolution

**Input**:
```
# Multi-turn conversation (10+ turns)
User: 帮我完成X任务（复杂任务）
...
User: 记住我的偏好：简洁回答
```

**Expected Results**:
- ✅ LLM calls `update_working_checkpoint`
- ✅ `working.key_info` updated
- ✅ `key_info` injected in subsequent turns
- ✅ (Optional) Skill file auto-generated

**Verification**:
```python
# Check working memory
print(handler.working)

# Check Skills directory
ls memory/skills/

# Check vector database
python -c "
from agent.vector_search import get_vector_search
vs = get_vector_search()
results = vs.search('用户偏好', limit=3)
print(results)
"
```

### Performance Testing

#### Response Time Comparison

**Method**:
```bash
# Native implementation
time curl -X POST http://localhost:9876/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test", "message": "你好"}'

# LiteLLM implementation (after use_litellm=true)
time curl -X POST http://localhost:9876/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test", "message": "你好"}'
```

**Expected**:
- ✅ LiteLLM response time difference < 10%

#### Token Usage Comparison

**Method**:
```bash
# Check logs for token statistics
tail -f logs/api_stderr.log | grep "TokenUsage"
```

**Expected**:
- ✅ prompt_tokens, completion_tokens, total_tokens all have values
- ✅ Values match actual usage

### Compatibility Testing

#### MiniMax Empty JSON Issue

**Method**:
```bash
# Switch to MiniMax model
# Repeat previously failing scenarios (photo ingestion, scheduled tasks)
# Check if empty JSON still returned
```

**Expected**:
- ✅ No more empty JSON
- ✅ Tool calls work properly
- ✅ Response format stable

#### Multi-model Switching

**Method**:
```bash
# Switch models in order: MiniMax -> GLM -> DeepSeek -> Claude
# Test basic conversation + tool calling for each
```

**Expected**:
- ✅ All models work properly
- ✅ No code changes needed to switch models

---

## Rollback Plan

### Trigger Conditions

Rollback immediately if:
- 🔴 Success rate < 90%
- 🔴 Tool call failure rate > 20%
- 🔴 User complaints increase significantly
- 🔴 Critical features (scheduled tasks, file processing) fail

### Rollback Steps

1. **Immediate config switch**:
   ```json
   // config/user-config.json
   {
     "llm": {
       "use_litellm": false
     }
   }
   ```

2. **Restart application**:
   ```bash
   pkill -f niu.exe
   go run main.go
   ```

3. **Verify rollback**:
   ```bash
   curl -X POST http://localhost:9876/api/chat \
     -H "Content-Type: application/json" \
     -d '{"session_id": "test", "message": "你好"}'
   ```

4. **Log analysis**:
   ```bash
   grep "ERROR" logs/api_stderr.log > errors.txt
   # Analyze root cause
   ```

5. **Re-launch after fix**:
   - Fix the issue
   - Restart testing from Phase 1
   - Do not skip phases

---

## Success Criteria Summary

### Technical Metrics

- ✅ **Compatibility**: Support all current models (MiniMax, GLM, Claude, OpenAI, DeepSeek)
- ✅ **Stability**: Success rate >= 95%, matching native implementation
- ✅ **Performance**: Response time difference < 10%
- ✅ **Feature Completeness**: All current features work (tool calling, thinking chain, self-evolution)
- ✅ **Token Tracking**: Accurate and usable for compression feature
- ✅ **Code Quality**: No new technical debt, cleaner code

### Business Metrics

- ✅ **Problem Resolution**: MiniMax empty JSON issue no longer occurs
- ✅ **User Experience**: Seamless switch, more stable functionality
- ✅ **Maintenance Cost**: 80% reduction in new model adaptation work
- ✅ **Extensibility**: Future models require only config, no code changes

### Risk Control

- ✅ **Rollback Capability**: Can quickly rollback to native implementation at any stage
- ✅ **Monitoring**: Key metrics have monitoring and alerts
- ✅ **Test Coverage**: Core features have automated tests
- ✅ **Documentation**: Migration process has detailed records

---

## Implementation Notes

### Development Principles

1. **Minimal Changes** - Only modify necessary parts, keep existing code stable
2. **Gradual Migration** - Don't replace everything at once, validate incrementally
3. **Backward Compatibility** - Keep existing interfaces unchanged
4. **Preserve Fallback** - Don't delete old implementation, use as degradation option
5. **Comprehensive Testing** - Each phase has verification steps

### Risk Control

1. **Code Protection** - Git commit current state before starting
2. **Config Switch** - Control via use_litellm, can rollback anytime
3. **Monitoring & Alerts** - Key metrics monitored, anomalies notified immediately
4. **Documentation** - Detailed record of each change and test result

### Technical Debt Management

1. **No New Debt** - New code follows project conventions
2. **Clean Old Debt** - Phase 6 removes redundant code
3. **Update Docs** - Update CLAUDE.md and architecture docs
4. **Knowledge Transfer** - Team members review code and docs

---

## Key Files

### New Files

- `agent/generic/litellm_adapter.py` - LiteLLM adapter
- `agent/tests/test_litellm_phase1.py` - Phase 1 tests
- `agent/tests/test_litellm_phase2.py` - Phase 2 tests
- `agent/tests/test_litellm_phase3.py` - Phase 3 tests
- `agent/tests/test_litellm_phase4.py` - Phase 4 tests
- `scripts/compare_litellm_vs_native.py` - Performance comparison script

### Modified Files

- `agent/runner.py` - create_client() function add LiteLLM branch
- `agent/generic/llmcore.py` - Keep as fallback
- `agent/generic/agent_loop.py` - Tool call parsing adapter
- `agent/handler.py` - Thinking chain and summary tag handling
- `config/user-config.json` - Add use_litellm config

### Potentially Deleted Files (After Phase 6)

- Some Session classes in `agent/generic/llmcore.py` (keep BaseSession and fallback)
- Deprecated regex logic in `_parse_mixed_response()`
- `_msgs_claude2oai()` message conversion function
- Provider-specific temperature adjustment code

---

## Timeline Estimate

| Phase | Effort | Time | Dependencies |
|-------|--------|------|--------------|
| Phase 1: Compatibility Layer | 3-4 days | 2-3 days | None |
| Phase 2: Tool Call Adapter | 2-3 days | 1-2 days | Phase 1 |
| Phase 3: Thinking Chain & Tags | 1-2 days | 1 day | Phase 2 |
| Phase 4: Token Tracking | 0.5-1 day | 0.5 day | Phase 3 |
| Phase 5: Full Testing & Switch | 3-4 days | 2-3 days | Phase 4 |
| Phase 6: Cleanup & Optimization | 1-2 days | 1 week later | Phase 5 stable |

**Total Estimate**: 8-13 days (~2 weeks)

---

## Future Optimization Directions

### Short-term (within 1 month)

1. **Async Optimization** - Leverage LiteLLM's async support for better concurrency
2. **Cache Optimization** - Integrate LiteLLM's prompt caching to reduce costs
3. **Monitoring Enhancement** - Add token usage trend analysis, cost alerts

### Mid-term (within 3 months)

1. **Multi-model Load Balancing** - Auto-select optimal model based on cost/performance
2. **Intelligent Fallback** - Auto-switch to backup model on failure
3. **A/B Testing Framework** - Easy testing of different model effects

### Long-term (6+ months)

1. **Cost Optimization** - Select appropriate model based on task complexity
2. **Performance Optimization** - Response caching, warm-up mechanisms
3. **Rapid New Model Integration** - Only config needed, no code changes

---

## Conclusion

**Feasibility Assessment**: ✅ Highly Feasible

**Reasoning**:
1. **Sound Architecture Design** - Current architecture highly modular, easy to adapt
2. **Controlled Risk** - Gradual migration and dual-track operation reduce risk
3. **Clear Benefits** - Solves MiniMax compatibility, unifies token tracking, reduces maintenance
4. **Mature Technology** - LiteLLM is a mature open-source project with active community

**Recommendation**: Start Phase 1 immediately to create compatibility layer and validate feasibility before proceeding.

**Critical Success Factors**:
- Strictly follow gradual migration strategy
- Comprehensive test coverage
- Robust monitoring and alerting
- Quick rollback mechanism

**Expected Benefits**:
- Solve MiniMax empty JSON issue
- Standardize response formats, reduce maintenance cost by 80%
- Standardize token tracking, support compression feature
- Reduce future model adaptation work by 80%
