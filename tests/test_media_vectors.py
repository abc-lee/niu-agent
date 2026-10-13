"""媒体向量独立存储（niu_api/internal 纯索引三键形态，spec D-12/D-13/D-23）：接口行为测试。

覆盖：
- 写入两条（不同实体）→ count()==2；
- 同实体覆盖 → count() 不变；
- search() 对已知向量返回降序且首位正确；cosine 并列 → 按 entity_name 升序 tie-break；
- 命中项键集合恰为 {entity_name, media_type, score}（纯索引，无 file_path——D-23）；
- 维度不符 → 抛错（add 与 search 两侧）；写侧校验：全零向量 / NaN-inf 拒绝（不落库）；
- 文件不存在时 count()==0 不抛；读侧容错：旧形态条目（残留 file_path 键）/ 缺冗余
  entity_name 字段 → 不炸检索（命中取 dict 键）；
- 旧形态库文件自愈：任意一次写入后全量条目归一化为三键，file_path 键消失；
- 并发契约：两线程并发 add 不同实体 → 两条都在（无锁 RMW 会静默丢更新）；
- add_many 批量写入（批内同名覆盖、整批校验失败不落、返回写入条数）；add() 是其单条薄包装；
- 测试接缝：NIU_MEDIA_STORE 环境变量重定向到临时目录（绝不写真 ~/.niu）。

全 mock / 纯临时目录：不 import LightRAG、不加载任何模型、不启动任何服务。
"""
import json

import pytest


def _vec(*flat: int | float) -> list[float]:
    """构造 DIM 维向量：零向量上按平铺的 (index, value, index, value, ...) 置位。"""
    from niu_api.internal import media_vectors

    v = [0.0] * media_vectors.DIM
    for i in range(0, len(flat), 2):
        v[flat[i]] = flat[i + 1]
    return v


@pytest.fixture(autouse=True)
def _redirect_media_store(tmp_path, monkeypatch):
    """强制媒体库落到临时目录（NIU_MEDIA_STORE 接缝），任何测试都不写真 ~/.niu。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_vectors.json"))


def test_add_two_entities_count_is_2(tmp_path):
    """写入两条不同实体 → count()==2，has() 对两者为真。"""
    from niu_api.internal import media_vectors

    media_vectors.add("img_a", "image", _vec(0, 1.0))
    media_vectors.add("img_b", "image", _vec(1, 1.0))

    assert media_vectors.count() == 2
    assert media_vectors.has("img_a")
    assert media_vectors.has("img_b")
    assert not media_vectors.has("img_c")


def test_add_same_entity_overwrites_count_unchanged(tmp_path):
    """同 entity_name 覆盖：count() 不变，search 命中新向量（旧向量不再可召回）。"""
    from niu_api.internal import media_vectors

    old = _vec(0, 1.0)      # 沿 e0
    new = _vec(1, 1.0)      # 沿 e1
    media_vectors.add("img_a", "image", old)
    assert media_vectors.count() == 1

    media_vectors.add("img_a", "image", new)
    assert media_vectors.count() == 1  # 覆盖不是追加

    hits = media_vectors.search(_vec(1, 1.0), top_k=10)
    assert len(hits) == 1
    assert hits[0]["entity_name"] == "img_a"
    # 查询沿 e1：命中新向量（score≈1.0）；若实现是"追加而非覆盖"，
    # 旧向量（沿 e0）的 score≈0 也会进结果集，或 count()==2 已先红
    assert hits[0]["score"] > 0.99


def test_search_returns_descending_scores_with_correct_top(tmp_path):
    """search() 按 cosine 降序，首位正确，score 数值正确。"""
    from niu_api.internal import media_vectors

    media_vectors.add("img_a", "image", _vec(0, 1.0))
    media_vectors.add("img_b", "image", _vec(1, 1.0))
    media_vectors.add("img_c", "image", _vec(0, 1.0, 1, 0.5))

    # 查询向量 = 0.8*e0 + 0.6*e1（未归一化；a/b 是单位向量，cosine 即点积）
    query = _vec(0, 0.8, 1, 0.6)
    hits = media_vectors.search(query, top_k=10)

    names = [h["entity_name"] for h in hits]
    # 正确降序：img_c(0.984) > img_a(0.8) > img_b(0.6)
    # img_c = e0 + 0.5*e1，cos = (0.8*1.0 + 0.6*0.5)/sqrt(1.25) = 1.1/1.118 ≈ 0.984
    assert names[0] == "img_c", f"首位应是 img_c（cos≈0.984），实际: {names}"
    assert names[1] == "img_a"
    assert names[2] == "img_b"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True), f"必须按 cosine 降序: {scores}"
    assert hits[0]["score"] == pytest.approx(0.984, abs=1e-3)
    assert hits[1]["score"] == pytest.approx(0.8, abs=1e-6)
    assert hits[2]["score"] == pytest.approx(0.6, abs=1e-6)
    assert hits[0]["media_type"] == "image"


def test_search_top_k_limits_results(tmp_path):
    """top_k 生效：只返回前 top_k 条。"""
    from niu_api.internal import media_vectors

    for i in range(5):
        media_vectors.add(f"img_{i}", "image", _vec(i, 1.0))

    hits = media_vectors.search(_vec(0, 1.0, 1, 1.0), top_k=2)
    assert len(hits) == 2
    assert hits[0]["entity_name"] == "img_0"
    assert hits[1]["entity_name"] == "img_1"


def test_search_result_item_shape(tmp_path):
    """返回项键集合恰为 {entity_name, media_type, score}（纯索引，无 file_path——D-23）。"""
    from niu_api.internal import media_vectors

    media_vectors.add("img_a", "image", _vec(0, 1.0))
    hit = media_vectors.search(_vec(0, 1.0), top_k=1)[0]
    assert set(hit) == {"entity_name", "media_type", "score"}


def test_add_wrong_dimension_raises(tmp_path):
    """add 维度 != DIM(768) → 抛错（不得静默写入）。"""
    from niu_api.internal import media_vectors

    with pytest.raises(ValueError):
        media_vectors.add("img_a", "image", [0.0] * 767)
    # 抛错后不得留下任何条目
    assert media_vectors.count() == 0


def test_search_wrong_dimension_raises(tmp_path):
    """search 查询向量维度 != DIM(768) → 抛错。"""
    from niu_api.internal import media_vectors

    with pytest.raises(ValueError):
        media_vectors.search([0.0] * 100, top_k=5)


def test_missing_file_count_zero_no_raise(tmp_path, monkeypatch):
    """文件不存在时 count()==0、has()==False、search() 返回 []——一律不抛。"""
    from niu_api.internal import media_vectors

    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "never_written.json"))
    assert media_vectors.count() == 0
    assert not media_vectors.has("anyone")
    assert media_vectors.search(_vec(0, 1.0), top_k=5) == []


def test_media_store_path_env_seam(tmp_path, monkeypatch):
    """接缝形态：NIU_MEDIA_STORE 设了就用它（返回该路径本身）。"""
    from niu_api.internal import media_vectors

    target = tmp_path / "custom_store.json"
    monkeypatch.setenv("NIU_MEDIA_STORE", str(target))
    assert media_vectors.media_store_path() == target


def test_media_store_path_default_under_home(tmp_path, monkeypatch):
    """未设 NIU_MEDIA_STORE 时落家目录 ~/.niu 下（每次现读，不缓存）。"""
    from pathlib import Path
    from niu_api.internal import media_vectors

    monkeypatch.delenv("NIU_MEDIA_STORE", raising=False)
    path = media_vectors.media_store_path()
    assert path.is_absolute()
    assert path.parent == Path.home() / ".niu"
    assert path.suffix == ".json"


def test_media_store_path_relative_env_raises(tmp_path, monkeypatch):
    """NIU_MEDIA_STORE 为相对路径 → 抛错（同 NIU_DB_PATH 惯例，防误写相对目录）。"""
    from niu_api.internal import media_vectors

    monkeypatch.setenv("NIU_MEDIA_STORE", "relative/store.json")
    with pytest.raises(ValueError):
        media_vectors.media_store_path()


def test_add_rejects_all_zero_vector(tmp_path):
    """写侧校验：全零向量 → ValueError（零向量永不可检索，不应静默入库）。"""
    from niu_api.internal import media_vectors

    with pytest.raises(ValueError):
        media_vectors.add("img_a", "image", [0.0] * media_vectors.DIM)
    assert media_vectors.count() == 0


def test_add_rejects_nan_or_inf_vector(tmp_path):
    """写侧校验：NaN / inf → ValueError（NaN 污染分数排序，inf 范数未定义）。"""
    from niu_api.internal import media_vectors

    for bad in (float("nan"), float("inf")):
        vec = _vec(0, 1.0)
        vec[1] = bad
        with pytest.raises(ValueError):
            media_vectors.add("img_a", "image", vec)
    assert media_vectors.count() == 0


def test_add_many_batch_write_returns_count(tmp_path):
    """add_many：N 条一次 read-modify-write，返回写入条数（批内同名去重为覆盖）。"""
    from niu_api.internal import media_vectors

    n = media_vectors.add_many([
        ("img_a", "image", _vec(0, 1.0)),
        ("img_b", "image", _vec(1, 1.0)),
        ("img_a", "image", _vec(2, 1.0)),  # 批内同名覆盖
    ])
    assert n == 2  # img_a 被覆盖去重，实际写入 2 条
    assert media_vectors.count() == 2
    hit = media_vectors.search(_vec(2, 1.0), top_k=1)[0]
    assert hit["entity_name"] == "img_a"
    assert hit["media_type"] == "image"


def test_add_many_empty_is_noop(tmp_path):
    """add_many([]) → 返回 0，不触碰文件（文件不存在时 count 仍为 0 不抛）。"""
    from niu_api.internal import media_vectors

    assert media_vectors.add_many([]) == 0
    assert media_vectors.count() == 0


def test_add_many_validation_failure_writes_nothing(tmp_path):
    """整批先校验：任一条不合格 → 整批抛错、一条不落（不留部分写）。"""
    from niu_api.internal import media_vectors

    media_vectors.add("img_ok", "image", _vec(0, 1.0))
    with pytest.raises(ValueError):
        media_vectors.add_many([
            ("img_new", "image", _vec(1, 1.0)),
            ("img_bad", "image", [0.0] * media_vectors.DIM),
        ])
    assert not media_vectors.has("img_new")  # 同批合格条目也不落
    assert media_vectors.count() == 1


def test_add_is_add_many_thin_wrapper(tmp_path):
    """add() 行为与 add_many 单条一致：同名覆盖、写侧校验（空绑定键抛错）。"""
    from niu_api.internal import media_vectors

    media_vectors.add("img_a", "image", _vec(0, 1.0))
    hit = media_vectors.search(_vec(0, 1.0), top_k=1)[0]
    assert hit["score"] == pytest.approx(1.0, abs=1e-9)
    with pytest.raises(ValueError):
        media_vectors.add("", "image", _vec(0, 1.0))
    assert media_vectors.count() == 1


def test_concurrent_add_no_lost_update(tmp_path, monkeypatch):
    """并发契约：两线程并发 add 不同实体 → 两条都必须存在。

    注入慢磁盘写（write 前 sleep）把 RMW 窗口撑大：无锁实现下第二线程的 load 会先于
    第一线程的整文件 replace 发生，其后的 replace 静默丢掉第一线程的更新；模块锁实现下
    慢写发生在锁内，第二线程整体阻塞到第一线程写完，load 看到已有条目，两条都在。
    """
    import threading
    import time

    from niu_api.internal import media_vectors

    real_write = media_vectors._atomic_write_store

    def slow_write(path, entries):
        time.sleep(0.05)  # 撑大竞争窗口
        real_write(path, entries)

    monkeypatch.setattr(media_vectors, "_atomic_write_store", slow_write)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=5)  # 两线程同时进入 add
            media_vectors.add(name, "image", _vec(0, 1.0))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("img_x", "img_y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"并发 add 抛异常: {errors}"
    assert media_vectors.has("img_x") and media_vectors.has("img_y"), (
        "并发 add 丢更新：read-modify-write 未被串行化（固定 .tmp 名下后 replace 者整文件覆盖先写者）"
    )
    assert media_vectors.count() == 2


def test_search_tie_break_by_entity_name(tmp_path):
    """cosine 并列 → 按 entity_name 升序确定性排序（与插入顺序无关）。"""
    from niu_api.internal import media_vectors

    # 两条同值向量、反名字序插入
    media_vectors.add("img_b", "image", _vec(0, 1.0))
    media_vectors.add("img_a", "image", _vec(0, 1.0))
    hits = media_vectors.search(_vec(0, 1.0), top_k=2)
    names = [h["entity_name"] for h in hits]
    assert names == ["img_a", "img_b"], f"并列分数须按 entity_name 升序: {names}"
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-9)


def test_search_survives_entry_missing_entity_name_field(tmp_path):
    """读侧容错：条目值缺冗余 entity_name 字段 → 不炸检索，命中实体名取 dict 键（绑定键）。"""
    from niu_api.internal import media_vectors

    store = tmp_path / "media_vectors.json"  # autouse fixture 设的接缝路径
    store.write_text(json.dumps({
        "dim": media_vectors.DIM,
        "entries": {"img_a": {"media_type": "image", "file_path": "/pics/a.jpg", "vector": _vec(0, 1.0)}},
    }), encoding="utf-8")
    hit = media_vectors.search(_vec(0, 1.0), top_k=1)[0]
    assert hit["entity_name"] == "img_a"
    assert hit["score"] == pytest.approx(1.0, abs=1e-9)


def test_legacy_store_self_heals_to_three_keys_on_write(tmp_path):
    """旧形态库文件自愈（D-23）：库文件含 v2.x 的 file_path 键；一次 add 之后，
    整文件重写为三键——全部条目只剩 {entity_name, media_type, vector}，file_path 键消失；
    旧向量仍可被检索命中（读侧容忍 + 写侧归一化，不需要迁移路径）。"""
    from niu_api.internal import media_vectors

    store = tmp_path / "media_vectors.json"
    old_vec = _vec(0, 1.0)
    store.write_text(json.dumps({
        "dim": media_vectors.DIM,
        "entries": {
            "img_legacy": {
                "entity_name": "img_legacy",
                "media_type": "image",
                "file_path": "/pics/legacy.jpg",
                "vector": old_vec,
            },
        },
    }), encoding="utf-8")
    # 旧形态可读、可检索（读侧容忍 file_path 残留键）
    assert media_vectors.count() == 1
    assert media_vectors.has("img_legacy")
    hit = media_vectors.search(old_vec, top_k=1)[0]
    assert hit["entity_name"] == "img_legacy"
    assert hit["score"] == pytest.approx(1.0, abs=1e-9)

    # 任意一次写入触发整库归一化重写
    media_vectors.add("img_new", "image", _vec(1, 1.0))

    raw = json.loads(store.read_text(encoding="utf-8"))
    entries = raw["entries"]
    assert set(entries) == {"img_legacy", "img_new"}
    for entry in entries.values():
        assert set(entry) == {"entity_name", "media_type", "vector"}, (
            f"自愈后条目须只含三键，实际: {sorted(entry)}"
        )
        assert "file_path" not in entry

    # 旧向量归一化后仍可命中（向量未被改写）
    hit = media_vectors.search(old_vec, top_k=2)[0]
    assert hit["entity_name"] == "img_legacy"
    assert hit["score"] == pytest.approx(1.0, abs=1e-9)
