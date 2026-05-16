"""
Experience Summarizer - 经验总结器

将 Agent 成功完成任务的经历提炼成可复用的 Skill。

核心概念（借鉴 GenericAgent）：
- 任务完成时提取关键经验
- 将环境事实、坑点、操作步骤写成 SOP
- 写入 memory/skills/ 目录，自动被 SkillSync 同步到向量库

优势（超越 GenericAgent）：
- 自动 + 手动触发
- 统一存储在 memory/skills/
- 自动被 SkillSync 索引，无需手动同步
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class ToolExecution:
    """单次工具执行记录"""
    tool_name: str
    args: dict
    result: str
    success: bool
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExperienceContext:
    """一轮对话的经验上下文"""
    user_input: str
    tool_executions: list[ToolExecution] = field(default_factory=list)
    final_result: str = ""
    turn_count: int = 0
    start_time: float = field(default_factory=time.time)


class ExperienceSummarizer:
    """
    经验总结器 - 将成功经验写成 Skill

    使用方式：
    1. 在 NiuHandler 中追踪工具执行
    2. 任务完成时调用 should_summarize() 判断是否需要总结
    3. 调用 summarize_and_write() 生成并写入 Skill
    """

    # Skill 文件存储目录
    SKILLS_DIR = Path(__file__).parent.parent.parent / "memory" / "skills"

    # 触发阈值
    DEFAULT_TURN_THRESHOLD = 10  # 超过 10 轮自动触发
    DEFAULT_MIN_TOOLS = 3  # 最少 3 次工具调用才考虑总结

    def __init__(self, turn_threshold: int = None, min_tools: int = None):
        self.turn_threshold = turn_threshold or self.DEFAULT_TURN_THRESHOLD
        self.min_tools = min_tools or self.DEFAULT_MIN_TOOLS
        self._ensure_skills_dir()

    def _ensure_skills_dir(self):
        """确保 skills 目录存在"""
        self.SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    def should_summarize(self, context: ExperienceContext) -> tuple[bool, str]:
        """
        判断是否应该总结经验

        Returns:
            (should_summarize, reason)
        """
        reasons = []

        # 条件 1: 任务成功完成
        if context.final_result and "error" not in context.final_result.lower():
            reasons.append("task_success")

        # 条件 2: 超过阈值轮数
        if context.turn_count >= self.turn_threshold:
            reasons.append(f"high_turn_count({context.turn_count})")

        # 条件 3: 多次工具调用
        if len(context.tool_executions) >= self.min_tools:
            reasons.append(f"multi_tool({len(context.tool_executions)})")

        # 条件 4: 有成功执行的关键工具
        successful_tools = [t for t in context.tool_executions if t.success]
        key_tools = {"code_run", "read", "write", "edit", "grep", "search", "remember"}
        if any(t.tool_name in key_tools for t in successful_tools):
            reasons.append("key_tool_success")

        should = len(reasons) >= 2  # 至少满足 2 个条件
        return should, ", ".join(reasons) if reasons else "not_enough_signals"

    def summarize_and_write(
        self,
        context: ExperienceContext,
        category: str = "general"
    ) -> Optional[Path]:
        """
        总结经验并写入 Skill 文件

        Args:
            context: 经验上下文
            category: 技能分类 (general, coding, file_ops, memory, etc.)

        Returns:
            写入的 Skill 文件路径，失败返回 None
        """
        if not context.tool_executions:
            logger.warning("[ExperienceSummarizer] No tool executions to summarize")
            return None

        # 生成技能内容
        skill_content = self._generate_skill_content(context, category)
        if not skill_content:
            return None

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 从用户输入提取关键词作为文件名
        safe_name = self._sanitize_filename(context.user_input)
        filename = f"{timestamp}_{safe_name}.md"

        # 写入文件
        skill_path = self.SKILLS_DIR / filename
        try:
            skill_path.write_text(skill_content, encoding="utf-8")
            logger.info(f"[ExperienceSummarizer] Wrote skill: {skill_path}")
            return skill_path
        except Exception as e:
            logger.error(f"[ExperienceSummarizer] Failed to write skill: {e}")
            return None

    def _generate_skill_content(
        self,
        context: ExperienceContext,
        category: str
    ) -> Optional[str]:
        """
        生成 Skill 文件内容

        格式：
        # Skill 标题

        ## 触发关键词
        xxx、yyy

        ## 标签
        category, tags

        ## 任务类型
        描述这是什么类型的任务

        ## 关键步骤
        1. 步骤 1
        2. 步骤 2

        ## 坑点提示
        - 坑点 1
        - 坑点 2

        ## 原始输入
        用户的原始输入（用于参考）
        """
        successful = [t for t in context.tool_executions if t.success]
        failed = [t for t in context.tool_executions if not t.success]

        if not successful:
            return None

        # 提取关键信息
        tools_used = list(set(t.tool_name for t in successful))
        tool_sequence = " → ".join(t.tool_name for t in context.tool_executions)

        # 生成标题
        title = self._generate_title(context.user_input, tools_used)

        # 生成触发词
        triggers = self._extract_triggers(context.user_input, tools_used)

        # 生成坑点
        pitfalls = self._extract_pitfalls(failed, successful)

        # 组装内容
        content = f"""# {title}

## 触发关键词
{triggers}

## 标签
{category}, auto-generated, experience

## 任务类型
从用户需求「{self._truncate(context.user_input, 50)}」提炼的经验

## 关键步骤
"""
        for i, tool in enumerate(successful, 1):
            content += f"{i}. **{tool.tool_name}**: {self._truncate(tool.result, 100)}\n"

        if pitfalls:
            content += f"\n## 坑点提示\n"
            for pitfall in pitfalls:
                content += f"- {pitfall}\n"

        content += f"""
## 执行统计
- 轮数: {context.turn_count}
- 工具调用: {len(successful)} 成功, {len(failed)} 失败
- 耗时: {time.time() - context.start_time:.1f}s

## 原始输入
```
{context.user_input}
```

---
*由 ExperienceSummarizer 自动生成 @ {datetime.now().isoformat()}*
"""
        return content

    def _generate_title(self, user_input: str, tools: list[str]) -> str:
        """生成技能标题"""
        # 提取用户输入中的关键动词和名词
        words = re.findall(r'[\w]+', user_input)
        key_words = [w for w in words if len(w) > 2][:3]

        tool_suffix = "-".join(tools[:2]) if tools else "task"

        if key_words:
            return f"经验_{'-'.join(key_words)}_{tool_suffix}"
        return f"经验_{tool_suffix}"

    def _extract_triggers(self, user_input: str, tools: list[str]) -> str:
        """提取触发关键词"""
        triggers = []

        # 从工具名提取
        for tool in tools:
            triggers.append(tool.replace("_", " "))

        # 从用户输入提取关键短语
        patterns = [
            r'(?:帮我|帮我|请|需要)(.+?)(?:完成|做|处理)',
            r'(?:如何|怎么)(.+?)(?:做|处理|完成)',
        ]
        for pattern in patterns:
            match = re.search(pattern, user_input)
            if match:
                triggers.append(match.group(1).strip())

        # 去重，保留前 5 个
        seen = set()
        unique = []
        for t in triggers:
            t_lower = t.lower()
            if t_lower not in seen:
                seen.add(t_lower)
                unique.append(t)
        return "、".join(unique[:5])

    def _extract_pitfalls(self, failed: list[ToolExecution], successful: list[ToolExecution]) -> list[str]:
        """提取坑点"""
        pitfalls = []

        # 从失败中提取
        for tool in failed:
            # 提取错误信息中的关键提示
            if tool.result:
                # 常见错误模式
                error_patterns = [
                    (r'权限.*?(?:不足|拒绝|无)', '权限不足需要注意'),
                    (r'文件.*?(?:不存在|未找到)', '文件路径需要确认'),
                    (r'超时', '操作可能超时'),
                    (r'格式.*?(?:错误|无效)', '输入格式需要检查'),
                ]
                for pattern, hint in error_patterns:
                    if re.search(pattern, tool.result, re.IGNORECASE):
                        pitfalls.append(f"{tool.tool_name}: {hint}")

        return list(set(pitfalls))[:5]  # 去重，限制 5 个

    def _sanitize_filename(self, text: str) -> str:
        """将文本转换为安全的文件名"""
        # 移除不安全字符
        text = re.sub(r'[<>:"/\\|?*]', '', text)
        # 替换空格和多余字符
        text = re.sub(r'\s+', '_', text)
        # 限制长度
        return text[:30] if len(text) > 30 else text

    def _truncate(self, text: str, max_len: int) -> str:
        """截断文本"""
        text = text.strip().replace('\n', ' ')
        if len(text) > max_len:
            return text[:max_len-3] + "..."
        return text


# 全局实例
_summarizer: Optional[ExperienceSummarizer] = None


def get_experience_summarizer() -> ExperienceSummarizer:
    """获取全局 ExperienceSummarizer 实例"""
    global _summarizer
    if _summarizer is None:
        _summarizer = ExperienceSummarizer()
    return _summarizer
