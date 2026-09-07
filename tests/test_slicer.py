"""会话单元切割器测试（agent/context_assembler/slicer.py）。

覆盖计划 Task 1 测试清单：正常对话 / U-A-T 链 / 连续多 user /
首条非 user / 空列表 / 孤立 tool 头。
"""

from types import SimpleNamespace

from agent.context_assembler.slicer import slice_units


def m(role: str, mid: str = ""):
    return SimpleNamespace(role=role, id=mid or f"{role}-{id}")


def seq(*roles: str):
    return [m(r, f"m{i}") for i, r in enumerate(roles)]


class TestPlanChecklist:
    def test_empty_list(self):
        assert slice_units([]) == []

    def test_normal_dialogue(self):
        # U-A U-A → 两个单元
        assert slice_units(seq("user", "assistant", "user", "assistant")) == [(0, 1), (2, 3)]

    def test_user_assistant_tool_chain(self):
        # user 开启，派生 assistant/tool 链收口为一个单元
        roles = ("user", "assistant", "tool", "tool", "assistant", "user")
        assert slice_units(seq(*roles)) == [(0, 4), (5, 5)]

    def test_consecutive_users_same_unit(self):
        # 连续多条 user 视为同一单元开启（规则②）
        roles = ("user", "user", "user", "assistant", "tool", "user")
        assert slice_units(seq(*roles)) == [(0, 4), (5, 5)]

    def test_first_message_not_user_included_in_first_unit(self):
        # 首条非 user 归入第一单元（规则①）——切割点不早于 0；
        # 其后每个 user 仍正常开启新单元
        roles = ("system", "assistant", "user", "assistant", "user")
        assert slice_units(seq(*roles)) == [(0, 1), (2, 3), (4, 4)]

    def test_leading_orphan_tool(self):
        # 孤立 tool 头（无前导 assistant）归入第一单元（规则③）
        roles = ("tool", "user", "assistant", "user")
        assert slice_units(seq(*roles)) == [(0, 0), (1, 2), (3, 3)]


class TestExtendedRules:
    def test_isolated_tool_midway_goes_to_previous_unit(self):
        # 中段孤立 tool（前导 assistant 已收口）也延续当前单元
        roles = ("user", "assistant", "tool", "user")
        assert slice_units(seq(*roles)) == [(0, 2), (3, 3)]
        roles = ("user", "assistant", "user", "tool", "user")
        assert slice_units(seq(*roles)) == [(0, 1), (2, 3), (4, 4)]

    def test_subagent_msg_and_system_attach_to_current_unit(self):
        roles = ("user", "subagent_msg", "system", "assistant", "user")
        assert slice_units(seq(*roles)) == [(0, 3), (4, 4)]

    def test_single_message(self):
        assert slice_units(seq("user")) == [(0, 0)]
        assert slice_units(seq("tool")) == [(0, 0)]

    def test_all_tools_single_unit(self):
        assert slice_units(seq("tool", "tool", "tool")) == [(0, 2)]

    def test_dict_messages_duck_typing(self):
        msgs = [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]
        assert slice_units(msgs) == [(0, 1), (2, 2)]


class TestInvariants:
    def test_units_contiguous_and_cover_all(self):
        import random

        rng = random.Random(42)
        for _ in range(200):
            n = rng.randint(1, 40)
            msgs = seq(*(rng.choice(["user", "assistant", "tool", "system"]) for _ in range(n)))
            units = slice_units(msgs)
            # 无缝衔接
            for k in range(len(units) - 1):
                assert units[k][1] + 1 == units[k + 1][0]
            # 全覆盖
            assert units[0][0] == 0 and units[-1][1] == n - 1
            # 每个单元至多一个开启 user run（首条之后不再有相邻 user 对跨切割）
            for s, e in units:
                run_users = [
                    i for i in range(s + 1, e + 1)
                    if msgs[i].role == "user" and msgs[i - 1].role != "user"
                ]
                assert run_users == []


class TestCompressionTripletSeeThrough:
    """压缩三件套透视（规则⑤，spec 2026-09-07 §3.1/§4）。

    content 用本地字面量（startswith 前缀即可）——slicer 测试零依赖，
    与 agent_loop 常量的绑定由 parity 契约测试负责（test_compression_summary.py）。
    """

    PREPARE = "[系统提示] 上下文即将压缩，超出保留范围的早期对话将被归档移出。"
    DONE = "[系统提示] 上下文压缩已完成。"

    def mc(self, role: str, content: str):
        return SimpleNamespace(role=role, content=content)

    def test_manual_scenario_new_question_opens_own_unit(self):
        # 手动场景（P0 主形态）：三件套后新用户提问独立开单元——
        # 回看透视后有效前条 = 总结（assistant），不被规则②粘入旧单元
        msgs = [
            self.mc("user", "指令"),
            self.mc("assistant", "a"),
            self.mc("user", self.PREPARE),
            self.mc("assistant", "总结"),
            self.mc("user", self.DONE),
            self.mc("user", "新提问"),
            self.mc("assistant", "a2"),
        ]
        assert slice_units(msgs) == [(0, 4), (5, 6)]

    def test_nested_triplet_single_unit(self):
        # 嵌套场景：三件套落在指令单元工具循环中段 → 单一大单元，无 [完成] 独立单元
        msgs = [
            self.mc("user", "指令"),
            self.mc("assistant", "a1"),
            SimpleNamespace(role="tool", content="t1"),
            self.mc("user", self.PREPARE),
            self.mc("assistant", "总结"),
            self.mc("user", self.DONE),
            self.mc("assistant", "a3"),
            SimpleNamespace(role="tool", content="t2"),
        ]
        assert slice_units(msgs) == [(0, 7)]

    def test_empty_summary_two_rows_seen_through(self):
        # 空总结两行形态（[准备][完成] 相邻，总结 LLM 失败时）：连续三件套整体透视
        msgs = [
            self.mc("user", "指令"),
            self.mc("assistant", "a"),
            self.mc("user", self.PREPARE),
            self.mc("user", self.DONE),
            self.mc("user", "新提问"),
        ]
        assert slice_units(msgs) == [(0, 3), (4, 4)]

    def test_consecutive_compressions_single_unit(self):
        # 连续压缩（两套三件套）：三件套不独立算「轮」→ 单一大单元整体保留/归档
        msgs = [
            self.mc("user", "指令"),
            self.mc("assistant", "a"),
            self.mc("user", self.PREPARE),
            self.mc("assistant", "总结1"),
            self.mc("user", self.DONE),
            self.mc("assistant", "a2"),
            self.mc("user", self.PREPARE + "（第二次）"),
            self.mc("assistant", "总结2"),
            self.mc("user", self.DONE),
            self.mc("assistant", "a3"),
        ]
        assert slice_units(msgs) == [(0, 9)]

    def test_first_message_is_triplet(self):
        # DB 仅三件套：规则①首条恒为第一单元起点，无碍
        msgs = [
            self.mc("user", self.PREPARE),
            self.mc("assistant", "总结"),
            self.mc("user", self.DONE),
        ]
        assert slice_units(msgs) == [(0, 2)]

    def test_other_system_prompt_user_still_opens_unit(self):
        # 判据精确性负向锁：其他 [系统提示] 前缀 user（skipped-at 形态）
        # 不匹配两条压缩前缀 → 照常开单元，不透视
        msgs = [
            self.mc("user", "指令"),
            self.mc("assistant", "a"),
            self.mc("user", "[系统提示] 你上一轮的输出被跳过，如需执行请单独输出。"),
        ]
        assert slice_units(msgs) == [(0, 1), (2, 2)]

    def test_no_triplet_sequence_unchanged(self):
        # 零回归：无三件套序列切割结果与旧规则逐字节一致（判据恒 False）
        roles = ("user", "assistant", "tool", "user", "user", "assistant")
        assert slice_units(seq(*roles)) == [(0, 2), (3, 5)]
