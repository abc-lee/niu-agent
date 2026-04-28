"""Analyze llm_interaction log to extract each LLM call's purpose."""
import re

with open('E:/tools/ai-bot/logs/llm_interaction_20260426.log', 'r', encoding='utf-8') as f:
    content = f.read()

blocks = re.split(r'={10,}', content)

for i, block in enumerate(blocks):
    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.+)', block)
    if not ts_match:
        continue
    ts = ts_match.group(1)
    model = ts_match.group(2).strip()

    is_keyword = 'keyword extractor' in block.lower() or 'high_level_keywords' in block
    has_sys_prompt = '你是一个全能型' in block

    if is_keyword:
        # Find the user query in the thinking chain
        query_match = re.search(r'The user query is "(.+?)"', block)
        if not query_match:
            query_match = re.search(r'The user query: "(.+?)"', block)
        if not query_match:
            query_match = re.search(r'user query is "(.+?)"', block)
        query = query_match.group(1) if query_match else '(not found)'

        # Find keywords result
        hl_match = re.search(r'"high_level_keywords":\s*\[(.*?)\]', block)
        ll_match = re.search(r'"low_level_keywords":\s*\[(.*?)\]', block)
        hl = hl_match.group(1).strip() if hl_match else ''
        ll = ll_match.group(1).strip() if ll_match else ''

        print(f'{ts} | KEYWORD_EXTRACT | query="{query[:80]}" | hl=[{hl}] ll=[{ll}]')
    elif has_sys_prompt:
        # Find what the agent decided to do
        ai_section = block[block.find('[AI回复]'):] if '[AI回复]' in block else ''
        tool_match = re.search(r'(file_read|bash|code_run|disk)', ai_section)
        tool = tool_match.group(1) if tool_match else '(no tool)'
        print(f'{ts} | AGENT_DECISION  | tool={tool}')
    else:
        preview = block[:150].replace('\n', ' ')
        print(f'{ts} | UNKNOWN        | {preview[:80]}')