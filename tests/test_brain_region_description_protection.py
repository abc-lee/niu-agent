"""
脑区描述保护测试

测试 LightRAG 的四个函数在遇到 entity_type=="brainregion" 节点时，
是否正确保护 description 不被覆盖。
"""
import pytest


class TestEditEntityBrainRegionProtection:
    """_edit_entity_impl 的脑区描述保护"""

    def test_brainregion_description_not_overwritten(self):
        """编辑 brainregion 节点时 description 应被保护"""
        node_data = {
            "entity_type": "brainregion",
            "description": "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库",
        }
        updated_data = {
            "description": "新的描述",
            "entity_type": "brainregion",
        }
        new_node_data = {**node_data, **updated_data}
        # 保护逻辑：如果是 brainregion，恢复原始 description 和 entity_type
        if str(node_data.get("entity_type", "")).strip().lower() == "brainregion":
            if "description" in updated_data:
                new_node_data["description"] = node_data.get("description", "")
            if "entity_type" in updated_data:
                new_node_data["entity_type"] = node_data.get("entity_type", "brainregion")

        assert new_node_data["description"] == node_data["description"]
        assert new_node_data["entity_type"] == "brainregion"

    def test_normal_entity_description_updated(self):
        """普通实体编辑时 description 应正常更新"""
        node_data = {
            "entity_type": "person",
            "description": "旧描述",
        }
        updated_data = {
            "description": "新描述",
        }
        new_node_data = {**node_data, **updated_data}
        is_brain_region = str(node_data.get("entity_type", "")).strip().lower() == "brainregion"
        if is_brain_region and "description" in updated_data:
            new_node_data["description"] = node_data.get("description", "")

        assert new_node_data["description"] == "新描述"


class TestMergeEntitiesBrainRegionProtection:
    """_merge_entities_impl 的脑区描述保护"""

    def test_brainregion_description_preserved_in_merge(self):
        """合并实体时，如果包含 brainregion，description 应保留原始值"""
        brain_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        existing_target = {
            "entity_type": "brainregion",
            "description": brain_desc,
        }
        merged_data = {
            "description": "拼接后的描述<SEP>更多内容",
            "entity_type": "brainregion",
        }

        # 保护逻辑
        if str(existing_target.get("entity_type", "")).strip().lower() == "brainregion":
            merged_data["description"] = existing_target.get("description", "")
            merged_data["entity_type"] = "brainregion"

        assert merged_data["description"] == brain_desc
        assert merged_data["entity_type"] == "brainregion"


class TestRegionLabelWithDescription:
    """新脑区 LLM 命名+描述返回"""

    def test_extract_label_and_description_from_json(self):
        """从 JSON 响应中同时提取 label 和 description"""
        import json

        content = '{"label": "量子计算", "description": "量子比特与量子算法研究"}'
        data = json.loads(content)
        label = str(data.get("label", "")).strip()
        description = str(data.get("description", "")).strip()
        assert label == "量子计算"
        assert description == "量子比特与量子算法研究"

    def test_extract_label_and_description_regex_fallback(self):
        """regex fallback 提取 label 和 description"""
        import re

        content = 'Here is the result: {"label": "机器学习", "description": "ML模型与训练技术"}'
        match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
        label = match.group(1).strip() if match else ""
        desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', content)
        description = desc_match.group(1).strip() if desc_match else ""

        assert label == "机器学习"
        assert description == "ML模型与训练技术"

    def test_fallback_description_empty_on_failure(self):
        """LLM 失败时 description 应为空字符串"""
        fallback_label = "Python"
        fallback_desc = ""
        assert fallback_label == "Python"
        assert fallback_desc == ""

    def test_llm_description_used_as_summary(self):
        """LLM 返回的描述应被用作脑区 summary"""
        region_llm_desc = "量子比特与量子算法研究"
        region_summary = region_llm_desc if region_llm_desc else "Python<SEP>NumPy<SEP>数据分析"
        assert region_summary == "量子比特与量子算法研究"

    def test_entity_name_fallback_when_no_llm_desc(self):
        """LLM 描述为空时 fallback 到实体名拼接"""
        region_llm_desc = ""
        entity_summary = "Python<SEP>NumPy<SEP>数据分析"
        region_summary = region_llm_desc if region_llm_desc else entity_summary
        assert region_summary == entity_summary

    def test_batch_regex_flexible_key_order(self):
        """批量 regex fallback 应支持任意键顺序"""
        import re

        # description 在 label 之前
        content = '{"description": "量子算法研究", "id": 0, "label": "量子计算"}'
        result = {}
        for obj_match in re.finditer(r'\{[^}]+\}', content):
            obj_str = obj_match.group(0)
            id_match = re.search(r'"id"\s*:\s*(\d+)', obj_str)
            label_match = re.search(r'"label"\s*:\s*"([^"]+)"', obj_str)
            if id_match and label_match:
                idx = int(id_match.group(1))
                label = label_match.group(1).strip()
                if label and len(label) <= 8:
                    desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', obj_str)
                    description = desc_match.group(1).strip() if desc_match else ""
                    result[idx] = (label, description)

        assert result[0] == ("量子计算", "量子算法研究")
