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
