"""
Memory - Three-layer memory system

L0: Meta rules (≤30 lines index) - global_mem_insight.txt
L2: Global facts - global_mem.txt
L3: Task Skills - *.sop.md files

Based on GenericAgent's memory architecture.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger


class MemoryManager:
    """
    Three-layer memory system

    L0 (index): Quick navigation, triggers, rules
    L2 (facts): Environment facts, paths, credentials
    L3 (skills): Task-specific SOPs
    """

    def __init__(self, memory_dir: str = None):
        if memory_dir is None:
            # Default to project's memory directory
            memory_dir = Path(__file__).parent.parent.parent / "memory"

        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # L0: Index file
        self.l0_file = self.memory_dir / "global_mem_insight.txt"

        # L2: Facts file
        self.l2_file = self.memory_dir / "global_mem.txt"

        # L3: Skills directory
        self.l3_dir = self.memory_dir / "skills"
        self.l3_dir.mkdir(exist_ok=True)

    def get_l0(self) -> str:
        """Get L0 index (triggers and rules)"""
        if self.l0_file.exists():
            return self.l0_file.read_text(encoding="utf-8")
        return ""

    def get_l2(self) -> str:
        """Get L2 facts"""
        if self.l2_file.exists():
            return self.l2_file.read_text(encoding="utf-8")
        return ""

    def get_l3(self, skill_name: str) -> Optional[str]:
        """Get a specific L3 skill"""
        skill_file = self.l3_dir / f"{skill_name}.md"
        if skill_file.exists():
            return skill_file.read_text(encoding="utf-8")
        return None

    def list_skills(self) -> List[str]:
        """List available L3 skills"""
        return [f.stem for f in self.l3_dir.glob("*.md")]

    def update_l0(self, content: str):
        """Update L0 index (should be kept ≤30 lines)"""
        lines = content.strip().split("\n")
        if len(lines) > 35:
            logger.warning(f"L0 index too long: {len(lines)} lines (should be ≤30)")

        self.l0_file.write_text(content, encoding="utf-8")
        logger.info(f"Updated L0 index: {len(lines)} lines")

    def update_l2(self, content: str):
        """Update L2 facts"""
        self.l2_file.write_text(content, encoding="utf-8")
        logger.info(f"Updated L2 facts: {len(content)} chars")

    def update_l3(self, skill_name: str, content: str):
        """Create or update an L3 skill"""
        skill_file = self.l3_dir / f"{skill_name}.md"
        skill_file.write_text(content, encoding="utf-8")
        logger.info(f"Updated L3 skill: {skill_name}")

    def get_prompt_context(self, user_input: str = None) -> str:
        """Get memory context for LLM prompt, with dynamic skill loading"""
        parts = []

        l0 = self.get_l0()
        if l0:
            parts.append(f"### [L0 索引]\n{l0}")

        l2 = self.get_l2()
        if l2:
            parts.append(f"### [L2 事实]\n{l2}")

        # 动态加载相关 skill
        if user_input:
            relevant_skill = self._find_relevant_skill(user_input)
            if relevant_skill:
                skill_content = self.get_l3(relevant_skill)
                if skill_content:
                    parts.append(f"### [相关技能: {relevant_skill}]\n{skill_content}")

        skills = self.list_skills()
        if skills:
            parts.append(f"### [可用技能]\n{', '.join(skills)}")

        return "\n\n".join(parts) if parts else ""

    def _find_relevant_skill(self, user_input: str) -> Optional[str]:
        """根据用户输入找到相关的 skill"""
        keywords_map = {
            "photo-processing": [
                "照片",
                "图片",
                "人脸",
                "人物",
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                "photo",
            ],
            "document-processing": ["文档", "pdf", "word", "合同", ".pdf", ".doc", ".txt", ".md"],
            "event-management": ["日程", "会议", "提醒", "定时", "任务"],
            "memory-management": ["记忆", "偏好", "设置", "配置"],
        }

        # 检查文件扩展名和关键词
        for skill_name, keywords in keywords_map.items():
            for kw in keywords:
                if kw.lower() in user_input.lower():
                    return skill_name

        # 默认：如果包含"入库"，根据文件扩展名判断
        if "入库" in user_input:
            if any(ext in user_input.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]):
                return "photo-processing"
            if any(ext in user_input.lower() for ext in [".pdf", ".doc", ".docx", ".txt", ".md"]):
                return "document-processing"

        return None
