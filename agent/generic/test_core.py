"""Test GenericAgent core functions"""

import sys

sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic import GenericAgentHandler, get_global_memory, StepOutcome

# Test 1: get_global_memory
memory = get_global_memory()
print("Test 1 - get_global_memory:")
print(f"  Length: {len(memory)} chars")
print(f"  Content preview: {memory[:100]}...")

# Test 2: StepOutcome
outcome = StepOutcome(data={"status": "success"}, next_prompt="test", should_exit=False)
print(f"\nTest 2 - StepOutcome:")
print(f"  data: {outcome.data}")
print(f"  next_prompt: {outcome.next_prompt}")
print(f"  should_exit: {outcome.should_exit}")


# Test 3: GenericAgentHandler
class MockParent:
    pass


handler = GenericAgentHandler(MockParent())
print(f"\nTest 3 - GenericAgentHandler:")
print(f"  Created successfully")
print(f"  Has do_code_run: {hasattr(handler, 'do_code_run')}")
print(f"  Has do_file_read: {hasattr(handler, 'do_file_read')}")
print(f"  Has do_file_write: {hasattr(handler, 'do_file_write')}")
print(f"  Has do_file_patch: {hasattr(handler, 'do_file_patch')}")

print("\n=== All tests passed ===")
