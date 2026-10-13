"""generate_l0_abstract 新语义单元测试。

背景：旧实现 0 人脸时写「单人照片」，但 person_names 为空只表示**没检出脸**
（风景/物品/截图全落这里），不是"单人"；且该文本会进 LightRAG 文档正文。
新语义只描述检测结果：
- 0 人 → `未检测到人物`
- 1 人 → 就是人名（不写合影、不写单人照片）
- ≥2 人 → `A、B合影`
日期行为不变：可 parse `%Y:%m:%d %H:%M:%S` → `Y年M月D日`；parse 失败 → 前 10 字符；无 → 不追加。
"""
import os
import sys

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_photo_server  # noqa: E402


def test_zero_persons_no_date():
    """0 人 + 无日期 → 恰为 `未检测到人物`，不含 `单人`/`合影`。"""
    assert niu_photo_server.generate_l0_abstract([], None) == "未检测到人物"
    out = niu_photo_server.generate_l0_abstract([], None)
    assert "单人" not in out
    assert "合影" not in out


def test_one_person_no_date():
    """1 人 + 无日期 → 恰为人名，不含 `合影`。"""
    out = niu_photo_server.generate_l0_abstract(["张三"], None)
    assert out == "张三"
    assert "合影" not in out


def test_two_persons():
    """>=2 人 → `A、B合影`。"""
    assert niu_photo_server.generate_l0_abstract(["张三", "李四"], None) == "张三、李四合影"


def test_two_unnamed_persons():
    """未命名人物也算「人」。"""
    assert (
        niu_photo_server.generate_l0_abstract(["未命名人物_1", "未命名人物_2"], None)
        == "未命名人物_1、未命名人物_2合影"
    )


def test_zero_persons_with_valid_date():
    """对真机现场的重现：无脸 + EXIF 日期 → `未检测到人物，2008年07月03日`。"""
    out = niu_photo_server.generate_l0_abstract([], "2008:07:03 10:42:20")
    assert out == "未检测到人物，2008年07月03日"
    assert "单人" not in out


def test_zero_persons_unparseable_date():
    """无法 parse 的 taken_at：取前 10 字符的行为不变（未回归）。"""
    out = niu_photo_server.generate_l0_abstract([], "2008")
    assert out == "未检测到人物，2008"
    # >=10 字符的不可 parse 值：确实取前 10 字符
    out = niu_photo_server.generate_l0_abstract([], "20080703104220")
    assert out == "未检测到人物，2008070310"
def test_one_person_with_valid_date():
    """1 人 + 合法日期 → 人名 + `，` + 日期。"""
    out = niu_photo_server.generate_l0_abstract(["张三"], "2008:07:03 10:42:20")
    assert out == "张三，2008年07月03日"


def test_same_person_twice_is_single():
    """现场重现：同一人两张脸 → ['张三','张三'] → 恰为 `张三`，不含 `合影`。"""
    out = niu_photo_server.generate_l0_abstract(["张三", "张三"], None)
    assert out == "张三"
    assert "合影" not in out


def test_three_faces_two_people_preserves_order():
    """保序去重：['张三','李四','张三'] → 按首次出现顺序拼 `A、B合影`。"""
    assert (
        niu_photo_server.generate_l0_abstract(["张三", "李四", "张三"], None)
        == "张三、李四合影"
    )


def test_unnamed_same_person_twice():
    """未命名人物同理：['未命名人物_1','未命名人物_1','未命名人物_2'] → 两人合影。"""
    assert (
        niu_photo_server.generate_l0_abstract(
            ["未命名人物_1", "未命名人物_1", "未命名人物_2"], None
        )
        == "未命名人物_1、未命名人物_2合影"
    )
