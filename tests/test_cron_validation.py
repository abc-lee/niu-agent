"""Tests for cron_expr validation in TaskStore.create_task/update_task"""
import pytest
from niu_api.internal.scheduler.task_store import TaskStore


class TestCreateTaskCronValidation:
    """create_task 的 cron_expr 校验"""

    def test_invalid_cron_8L_rejected(self, tmp_path):
        """非法 cron_expr（8L）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="Invalid weekday"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 ? * 8L"
            )

    def test_invalid_cron_1_hash_6_rejected(self, tmp_path):
        """非法 cron_expr（1#6）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="Invalid N"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 ? * 1#6"
            )

    def test_invalid_cron_mutex_rejected(self, tmp_path):
        """互斥校验失败（# + 具体 dom）创建时被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="day-of-month 必须是"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr="0 9 15 * 1#2"
            )

    def test_recurring_without_cron_rejected(self, tmp_path):
        """is_recurring=True 但无 cron_expr 被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="循环任务必须提供 cron_expr"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=True,
                cron_expr=None
            )

    def test_onetime_with_cron_rejected(self, tmp_path):
        """is_recurring=False 但传了 cron_expr 被拒"""
        store = TaskStore(str(tmp_path / "test.db"))
        with pytest.raises(ValueError, match="一次性任务不应提供 cron_expr"):
            store.create_task(
                content="test",
                scheduled_at="2026-08-01T09:00:00",
                is_recurring=False,
                cron_expr="0 9 * * *"
            )

    def test_valid_recurring_accepted(self, tmp_path):
        """合法循环任务正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 9 ? * 1#2"
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 9 ? * 1#2"

    def test_valid_onetime_without_cron_accepted(self, tmp_path):
        """合法一次性任务（无 cron）正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=False,
            cron_expr=None
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] is None

    def test_valid_advanced_modifier_accepted(self, tmp_path):
        """合法高级修饰符（LW）正常创建"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 0 LW * *"
        )
        assert task_id is not None
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 0 LW * *"


class TestUpdateTaskCronValidation:
    """update_task 的 cron_expr 校验"""

    def _create_store_with_task(self, tmp_path):
        """辅助：建临时文件库并预置一个合法循环任务"""
        store = TaskStore(str(tmp_path / "test.db"))
        task_id = store.create_task(
            content="test",
            scheduled_at="2026-08-01T09:00:00",
            is_recurring=True,
            cron_expr="0 9 * * *"
        )
        return store, task_id

    def test_update_to_invalid_cron_rejected(self, tmp_path):
        """更新为非法 cron_expr 被拒"""
        store, task_id = self._create_store_with_task(tmp_path)
        with pytest.raises(ValueError, match="Invalid weekday"):
            store.update_task(
                task_id=task_id,
                cron_expr="0 9 ? * 8L"
            )

    def test_update_to_invalid_mutex_rejected(self, tmp_path):
        """更新为互斥违规被拒"""
        store, task_id = self._create_store_with_task(tmp_path)
        with pytest.raises(ValueError, match="day-of-month 必须是"):
            store.update_task(
                task_id=task_id,
                cron_expr="0 9 15 * 1#2"
            )

    def test_update_to_valid_cron_accepted(self, tmp_path):
        """更新为合法 cron_expr 成功"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            cron_expr="0 9 ? * 1#2"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 9 ? * 1#2"

    def test_update_to_valid_L_accepted(self, tmp_path):
        """更新为合法 L 修饰符成功"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            cron_expr="0 17 ? * 5L"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["cron_expr"] == "0 17 ? * 5L"

    def test_update_without_cron_not_validated(self, tmp_path):
        """不传 cron_expr 时不触发校验（其他字段更新正常）"""
        store, task_id = self._create_store_with_task(tmp_path)
        success = store.update_task(
            task_id=task_id,
            content="updated content"
        )
        assert success is True
        task = store.get_task(task_id)
        assert task["content"] == "updated content"
        # cron_expr 保持不变
        assert task["cron_expr"] == "0 9 * * *"
