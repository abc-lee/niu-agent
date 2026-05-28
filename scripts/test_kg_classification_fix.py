"""全面验证知识图谱分类修复 + 文档入库修复"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.mcp_loader import load_mcp_tools

# ============== 1. ToolRegistry 验证 ==============
print("=" * 60)
print("1. ToolRegistry 验证")
print("=" * 60)

registry = load_mcp_tools()
schemas = registry.get_schemas()
all_tool_names = registry.list_tools()

print(f"Total tools registered: {len(all_tool_names)}")

# 检查 ingest_document 是否注册
has_ingest_doc = registry.has_tool("photo-server/ingest_document")
has_ingest = registry.has_tool("photo-server/ingest")
print(f"photo-server/ingest_document: {has_ingest_doc}")
print(f"photo-server/ingest: {has_ingest}")

if not has_ingest_doc:
    print("  FAIL: ingest_document not registered!")
else:
    print("  PASS: ingest_document registered")

# 检查 ingest_document schema
if has_ingest_doc:
    schema = registry._schemas.get("photo-server/ingest_document")
    if schema:
        print(f"  Schema name: {schema.get('name')}")
        print(f"  Has input_schema: {bool(schema.get('input_schema'))}")
        required = schema.get('input_schema', {}).get('required', [])
        print(f"  Required params: {required}")
        if 'file_path' in required:
            print("  PASS: required param is 'file_path' (not 'path')")
        else:
            print("  FAIL: required param should be 'file_path'")

# 检查 photo-server 所有工具
photo_tools = [t for t in all_tool_names if t.startswith("photo-server/")]
print(f"\nphoto-server tools ({len(photo_tools)}):")
for t in sorted(photo_tools):
    print(f"  {t}")

# ============== 2. CUSTOM_ENTITY_TYPES 验证 ==============
print("\n" + "=" * 60)
print("2. CUSTOM_ENTITY_TYPES 验证")
print("=" * 60)

from niu_api.internal.lightrag_manager import _create_lightrag_instance
# 不实际创建实例，只验证常量定义
import niu_api.internal.lightrag_manager as mgr_module

# 找到 CUSTOM_ENTITY_TYPES 定义
source_file = mgr_module.__file__
with open(source_file, 'r', encoding='utf-8') as f:
    content = f.read()

# 提取 CUSTOM_ENTITY_TYPES 列表
import re
match = re.search(r'CUSTOM_ENTITY_TYPES\s*=\s*\[(.*?)\]', content, re.DOTALL)
if match:
    types_str = match.group(1)
    types = [t.strip().strip('"').strip("'") for t in types_str.split(',') if t.strip()]
    print(f"CUSTOM_ENTITY_TYPES ({len(types)}): {types}")

    expected = 18
    if len(types) == expected:
        print(f"  PASS: {expected} types found")
    else:
        print(f"  FAIL: expected {expected}, got {len(types)}")

    # 检查关键类型
    required_types = ["BrainRegion", "InteractionHabit", "EpisodicEvent", "Other"]
    for rt in required_types:
        if rt in types:
            print(f"  PASS: '{rt}' present")
        else:
            print(f"  FAIL: '{rt}' missing!")

# ============== 3. ENTITY_TYPES 验证 ==============
print("\n" + "=" * 60)
print("3. ENTITY_TYPES 验证")
print("=" * 60)

from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()

entity_types = adapter.ENTITY_TYPES
print(f"ENTITY_TYPES ({len(entity_types)}): {sorted(entity_types)}")

if len(entity_types) == 18:
    print("  PASS: 18 types")
else:
    print(f"  FAIL: expected 18, got {len(entity_types)}")

# ============== 4. _ENTITY_TYPE_TO_CATEGORY 验证 ==============
print("\n" + "=" * 60)
print("4. _ENTITY_TYPE_TO_CATEGORY 验证")
print("=" * 60)

mapping = adapter._ENTITY_TYPE_TO_CATEGORY
print(f"Mapping entries: {len(mapping)}")
for k, v in sorted(mapping.items()):
    print(f"  {k} -> {v}")

if len(mapping) == 18:
    print("  PASS: 18 mappings")
else:
    print(f"  FAIL: expected 18, got {len(mapping)}")

# 检查关键映射
critical_mappings = {
    "tool": "knowledge",
    "concept": "knowledge",
    "brainregion": "knowledge",
    "episodicevent": "knowledge",
    "other": "other",
}
for key, expected_val in critical_mappings.items():
    actual = mapping.get(key)
    if actual == expected_val:
        print(f"  PASS: '{key}' -> '{actual}'")
    else:
        print(f"  FAIL: '{key}' -> '{actual}' (expected '{expected_val}')")

# ============== 5. entityType 默认值验证 ==============
print("\n" + "=" * 60)
print("5. entityType 默认值验证")
print("=" * 60)

# 检查 kg_api.py 中是否有残留的旧默认值
kg_api_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "niu_api", "kg_api.py")
with open(kg_api_path, 'r', encoding='utf-8') as f:
    kg_content = f.read()

# 搜索 "UNKNOWN" 或 "entity" 作为 entityType 默认值
bad_defaults = []
for i, line in enumerate(kg_content.split('\n'), 1):
    if '"entity_type", "UNKNOWN"' in line or '"type", "entity"' in line:
        bad_defaults.append((i, line.strip()))

if bad_defaults:
    print("  FAIL: Found old default values:")
    for line_no, line in bad_defaults:
        print(f"    Line {line_no}: {line}")
else:
    print("  PASS: No 'UNKNOWN' or 'entity' defaults in kg_api.py")

# 检查 lightrag_adapter.py
adapter_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "niu_api", "internal", "lightrag_adapter.py")
with open(adapter_path, 'r', encoding='utf-8') as f:
    adapter_content = f.read()

bad_defaults_adapter = []
for i, line in enumerate(adapter_content.split('\n'), 1):
    if '"UNKNOWN"' in line and 'entity_type' in line.lower():
        bad_defaults_adapter.append((i, line.strip()))

if bad_defaults_adapter:
    print("  FAIL: Found 'UNKNOWN' defaults in lightrag_adapter.py:")
    for line_no, line in bad_defaults_adapter:
        print(f"    Line {line_no}: {line}")
else:
    print("  PASS: No 'UNKNOWN' defaults in lightrag_adapter.py")

# ============== 6. 前端一致性验证 ==============
print("\n" + "=" * 60)
print("6. 前端一致性验证 (renderer.js)")
print("=" * 60)

renderer_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "graph", "renderer.js")
with open(renderer_path, 'r', encoding='utf-8') as f:
    renderer_content = f.read()

# 提取 typeColors 键
tc_match = re.search(r'const typeColors\s*=\s*\{(.*?)\}', renderer_content, re.DOTALL)
if tc_match:
    tc_str = tc_match.group(1)
    tc_keys = [k.strip() for k in re.findall(r"(\w+)\s*:", tc_str)]
    print(f"typeColors keys ({len(tc_keys)}): {tc_keys}")
    if len(tc_keys) == 18:
        print("  PASS: 18 type colors")
    else:
        print(f"  FAIL: expected 18, got {len(tc_keys)}")

# 检查 brainregion 颜色
if 'brainregion: \'#6C5CE7\'' in renderer_content:
    print("  PASS: brainregion color is #6C5CE7 (distinct from location #9B59B6)")
elif 'brainregion: \'#9B59B6\'' in renderer_content:
    print("  FAIL: brainregion color #9B59B6 conflicts with location!")
else:
    print("  WARN: brainregion color not found in expected format")

# 检查 isCoreNode 使用 mapNodeType
if 'return mapNodeType(orig) === currentPerspective' in renderer_content:
    print("  PASS: isCoreNode uses unified mapNodeType()")
elif 'orig.nodeType === \'Document\'' in renderer_content and 'docSubtypes' in renderer_content:
    print("  FAIL: isCoreNode still uses old Document subtype logic!")
else:
    print("  WARN: isCoreNode logic changed, verify manually")

# 检查媒体预览不限制 nodeType
if 'if (orig.uri)' in renderer_content and 'orig.nodeType === \'Document\'' not in renderer_content.split('Media thumbnail')[1].split('Related edges')[0]:
    print("  PASS: Media preview no longer requires nodeType===Document")
else:
    print("  FAIL: Media preview still requires nodeType===Document")

# ============== 7. 前端按钮验证 ==============
print("\n" + "=" * 60)
print("7. 前端按钮验证 (index.html)")
print("=" * 60)

html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "graph", "index.html")
with open(html_path, 'r', encoding='utf-8') as f:
    html_content = f.read()

buttons = re.findall(r'data-core="(\w+)"', html_content)
print(f"Perspective buttons ({len(buttons)}): {buttons}")
if len(buttons) == 17:
    print("  PASS: 17 buttons (excluding 'Other')")
else:
    print(f"  FAIL: expected 17, got {len(buttons)}")

if 'brainregion' in buttons:
    print("  PASS: brainregion button present")
else:
    print("  FAIL: brainregion button missing!")

# ============== 8. CSS 验证 ==============
print("\n" + "=" * 60)
print("8. CSS 验证 (styles.css)")
print("=" * 60)

css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "graph", "styles.css")
with open(css_path, 'r', encoding='utf-8') as f:
    css_content = f.read()

# 检查文字颜色定义
text_color_types = re.findall(r'\.persp-btn\[data-core="(\w+)"\]\s*\{\s*color:', css_content)
print(f"Text color definitions ({len(text_color_types)}): {text_color_types}")
if len(text_color_types) == 17:
    print("  PASS: 17 text color definitions")
else:
    print(f"  FAIL: expected 17, got {len(text_color_types)}")

# 检查 active 背景色定义
active_color_types = re.findall(r'\.persp-btn\.active\[data-core="(\w+)"\]', css_content)
print(f"Active color definitions ({len(active_color_types)}): {active_color_types}")
if len(active_color_types) == 17:
    print("  PASS: 17 active color definitions")
else:
    print(f"  FAIL: expected 17, got {len(active_color_types)}")

# 检查新增类型是否有颜色
new_types = ['skill', 'tool', 'knowledge', 'interactionhabit', 'episodicevent', 'brainregion']
for nt in new_types:
    if nt in text_color_types:
        print(f"  PASS: '{nt}' has text color")
    else:
        print(f"  FAIL: '{nt}' missing text color!")
    if nt in active_color_types:
        print(f"  PASS: '{nt}' has active color")
    else:
        print(f"  FAIL: '{nt}' missing active color!")

# ============== 9. ingest_document skipped 路径验证 ==============
print("\n" + "=" * 60)
print("9. ingest_document skipped 路径验证")
print("=" * 60)

photo_server_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "mcp-servers", "photo-server", "src", "niu_photo_server", "__init__.py")
with open(photo_server_path, 'r', encoding='utf-8') as f:
    photo_content = f.read()

# 检查 skipped 路径是否包含 LightRAG 写入
skipped_section = photo_content[photo_content.find('if action == "skipped"'):photo_content.find('if action == "skipped"') + 1500]

if 'lightrag_insert' in skipped_section or 'LightRAG ainsert' in skipped_section:
    print("  PASS: skipped path now includes LightRAG ainsert")
else:
    print("  FAIL: skipped path still skips LightRAG!")

# 检查 TOOL_SCHEMAS 包含 ingest_document
if '"ingest_document"' in photo_content[:photo_content.find('name_person')]:
    print("  PASS: ingest_document in TOOL_SCHEMAS")
else:
    print("  FAIL: ingest_document missing from TOOL_SCHEMAS!")

# ============== 10. 注入点 entity_type 验证 ==============
print("\n" + "=" * 60)
print("10. 注入点 entity_type 验证")
print("=" * 60)

# 检查所有注入点使用 PascalCase
injection_files = {
    "agent/injector/sync.py": ["Skill"],
    "agent/injector/lightrag_sync.py": ["Person", "Skill", "Tool"],
    "niu_api/injector.py": ["Skill"],
    "niu_api/notes_api.py": ["Note"],
    "agent/brain_tools.py": ["Tool"],
    "niu_api/internal/region_manager.py": ["BrainRegion"],
}

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for rel_path, expected_types in injection_files.items():
    full_path = os.path.join(project_root, rel_path)
    if not os.path.exists(full_path):
        print(f"  SKIP: {rel_path} not found")
        continue
    with open(full_path, 'r', encoding='utf-8') as f:
        file_content = f.read()

    for et in expected_types:
        # 搜索 entity_type 相关行
        patterns = [
            f'entity_type="{et}"',
            f'entity_type = "{et}"',
            f'ENTITY_TYPE = "{et}"',
            f'"{et}"',
        ]
        found = False
        for p in patterns:
            if p in file_content:
                found = True
                break
        if found:
            print(f"  PASS: {rel_path} uses '{et}'")
        else:
            print(f"  WARN: {rel_path} - '{et}' not found in expected patterns")

# ============== Summary ==============
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print("All verification checks completed. Review PASS/FAIL results above.")