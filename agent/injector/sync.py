"""
Skill Sync

Skills 目录同步服务。定时扫描 ~/.niu/skills/ 目录，同步变化到 LightRAG 知识图谱。
通过 entity_type="skill" 标签区分，供 LightRAGAdapter.search_skills() 检索。
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from niu_api.internal.lightrag_manager import wait_lightrag_ready
from niu_api.internal.region_manager import BELONGS_TO_RELATION

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# watchdog 相关导入
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileDeletedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None  # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    FileCreatedEvent = ()  # type: ignore[assignment,misc]  # empty tuple for isinstance fallback
    FileModifiedEvent = ()  # type: ignore[assignment,misc]
    FileDeletedEvent = ()  # type: ignore[assignment,misc]


if WATCHDOG_AVAILABLE:
    _SkillFileHandlerBase = FileSystemEventHandler
else:
    _SkillFileHandlerBase = object  # type: ignore[misc]


class SkillFileHandler(_SkillFileHandlerBase):  # type: ignore[misc]
    """
    Skill 文件变化处理器，带防抖和 self_writing 过滤
    """

    def __init__(self, sync_instance: "SkillSync", debounce: float = 1.0):
        if WATCHDOG_AVAILABLE:
            super().__init__()
        self._sync = sync_instance
        self._debounce = debounce
        self._pending: dict[str, tuple[str, threading.Timer]] = {}  # path -> (action, timer)
        self._lock = threading.Lock()

    def on_created(self, event):
        """文件创建事件"""
        if WATCHDOG_AVAILABLE and isinstance(event, FileCreatedEvent):
            path = str(event.src_path)
            if not event.is_directory and path.endswith(".md"):
                logger.debug(f"[SkillFileHandler] File created: {path}")
                self._schedule_sync(path, "sync")

    def on_modified(self, event):
        """文件修改事件"""
        if WATCHDOG_AVAILABLE and isinstance(event, FileModifiedEvent):
            path = str(event.src_path)
            if not event.is_directory and path.endswith(".md"):
                # 过滤 self_writing
                try:
                    mtime = Path(path).stat().st_mtime
                    if not self._sync._is_self_write(path, mtime):
                        logger.debug(f"[SkillFileHandler] File modified: {path}")
                        self._schedule_sync(path, "sync")
                except Exception as e:
                    logger.warning(f"[SkillFileHandler] Failed to check mtime: {e}")

    def on_deleted(self, event):
        """文件删除事件"""
        if WATCHDOG_AVAILABLE and isinstance(event, FileDeletedEvent):
            path = str(event.src_path)
            if not event.is_directory and path.endswith(".md"):
                logger.debug(f"[SkillFileHandler] File deleted: {path}")
                self._schedule_sync(path, "delete")

    def _schedule_sync(self, path: str, action: str):
        """
        防抖调度：在指定时间内重复事件只执行最后一次

        Args:
            path: 文件路径
            action: 动作类型 ("sync" 或 "delete")
        """
        with self._lock:
            # 取消之前的 timer
            if path in self._pending:
                _, old_timer = self._pending[path]
                old_timer.cancel()

            # 设置新 timer
            timer = threading.Timer(self._debounce, self._execute, args=(path, action))
            self._pending[path] = (action, timer)
            timer.start()

    def _execute(self, path: str, action: str):
        """执行同步动作"""
        with self._lock:
            self._pending.pop(path, None)

        try:
            name = Path(path).stem
            if action == "sync":
                if self._sync._sync_skill(name, Path(path)):
                    # 更新 hash 到状态文件，避免下次定时扫描重复注入
                    content_hash = self._sync._compute_file_hash(Path(path))
                    if content_hash:
                        with self._sync._lock:
                            self._sync._last_scan[name] = content_hash
                        self._sync._save_state()
            elif action == "delete":
                if self._sync._delete_skill_from_lightrag(name):
                    with self._sync._lock:
                        self._sync._last_scan.pop(name, None)
                    self._sync._save_state()
        except Exception as e:
            logger.error(f"[SkillFileHandler] Failed to execute {action} for {path}: {e}")


class SkillSync:
    """
    Skills 目录同步服务

    扫描 ~/.niu/skills/ 目录，检测文件变化，同步到 LightRAG 知识图谱。
    通过 entity_type="skill" 标签区分，供 LightRAGAdapter.search_skills() 检索。

    变化检测基于文件内容哈希（SHA256），状态持久化到 ~/.niu/skill_sync_state.json，
    进程重启后不会误判已有 skill 为"新增"。mtime 变但内容不变则跳过。
    """

    def __init__(self, skills_dir: Optional[str] = None, scan_interval: int = 60, use_watchdog: bool = True):
        self.skills_dir = Path(skills_dir or self._default_skills_dir())
        self.scan_interval = scan_interval
        self.use_watchdog = use_watchdog and WATCHDOG_AVAILABLE

        # 持久化状态文件（磁盘唯一真相来源）
        self._state_file = Path.home() / ".niu" / "skill_sync_state.json"

        # 内存缓存 {name: hash_str}，从状态文件加载
        self._last_scan: dict[str, str] = self._load_state()
        # notes 状态也持久化到同一个文件 {note_id: content_hash}
        self._last_notes_scan: dict[str, str] = self._load_notes_state()
        self._lock = threading.Lock()  # 线程锁

        # 后台线程
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 首次扫描完成事件（供 run_repair_on_user_request 等待）
        # 在 scan_and_sync 首次执行完毕后 set()，让 repair 流程知道
        # SkillSync 的 ghost 清理已跑完，可以重检 + 二次 repair。
        self._first_scan_complete = threading.Event()

        # watchdog 相关
        if WATCHDOG_AVAILABLE:
            from watchdog.observers import Observer as _ObserverType
            self._observer: Optional[_ObserverType] = None  # type: ignore[unused-ignore]
        else:
            self._observer = None  # type: ignore[assignment]

        # self_writing 检测
        self._last_write_times: dict[str, float] = {}  # path -> mtime
        self._self_write_window = 2.0  # 秒，自己写入后的冷却时间

    @staticmethod
    def _default_skills_dir() -> str:
        """默认 skills 目录"""
        return str(Path.home() / ".niu" / "skills")

    def _is_self_write(self, path: str, mtime: float) -> bool:
        """
        检测是否为 self_writing（自己写入触发的修改事件）

        Args:
            path: 文件路径
            mtime: 当前文件的修改时间

        Returns:
            是否为 self_writing
        """
        last = self._last_write_times.get(path, 0)
        return (mtime - last) < self._self_write_window

    def _record_self_write(self, path: str):
        """
        记录自己写入的文件

        Args:
            path: 文件路径
        """
        self._last_write_times[path] = time.time()

    @staticmethod
    def _compute_file_hash(path: Path) -> str:
        """计算文件内容的 SHA256 哈希"""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _load_state(self) -> dict[str, str]:
        """从磁盘加载持久化状态文件；损坏或不存在时返回空 dict。

        状态文件格式：
        {
          "browser-automation": "sha256hash1",
          "python-patterns": "sha256hash2",
          "_notes": { "note_id1": "hash1" }
        }

        返回时排除 "_notes" 键，只返回 {name: hash_str}。
        """
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if k != "_notes" and isinstance(v, str)}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SkillSync] State file corrupt or unreadable, starting fresh: {e}")
        return {}

    def _load_notes_state(self) -> dict[str, str]:
        """从磁盘加载 notes 子状态；损坏或不存在时返回空 dict"""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    notes = data.get("_notes", {})
                    if isinstance(notes, dict):
                        return {k: v for k, v in notes.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SkillSync] State file corrupt or unreadable for notes, starting fresh: {e}")
        return {}

    def _save_state(self) -> None:
        """将当前内存状态持久化到磁盘（异常保护）"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {**self._last_scan, "_notes": self._last_notes_scan}
            self._state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[SkillSync] Failed to save state file: {e}")

    def scan_and_sync(self) -> tuple[int, int, int]:
        """
        扫描目录，同步变化的 skills 到 LightRAG 知识图谱

        变化检测基于文件内容哈希（SHA256），状态持久化到磁盘文件，
        进程重启后不会误判已有 skill 为"新增"。

        仅在注入/删除成功时才更新状态文件中的 hash，
        失败的 skill 保留旧 hash（或新增失败的不写入），
        下次扫描时会重试。

        无变化不写盘：入口对 _last_scan + _last_notes_scan 做深拷贝快照，
        出口对比是否变化，无变化跳过 _save_state（避免每分钟无意义重写）。

        Returns:
            (added, updated, deleted) 计数；若 LightRAG 不可用返回 (-1, -1, -1)
            表示"未真正扫描"，调用方据此不触发 _first_scan_complete。
        """
        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return 0, 0, 0

        # 入口快照：scan 之前的 _last_scan + _last_notes_scan 状态
        # 位置：在 skills_dir 检查之后、LightRAG 可用性检查（L286-293）之前
        # 出口对比是否变化，无变化跳过 _save_state
        # 注意：LightRAG 不可用提前 return -1 时不做快照也无副作用（不写盘就行）
        with self._lock:
            skills_snapshot_before: dict[str, str] = dict(self._last_scan)
            notes_snapshot_before: dict[str, str] = dict(self._last_notes_scan)

        # 0. 检查 LightRAG 是否可用——不可用则跳过本次扫描（不算"首次扫描完成"）
        # 原因：run_repair_on_user_request 等待 _first_scan_complete 触发后重检，
        # 若 LightRAG 不可用时 set() 会让 repair 流程过早重检，而真正的清理
        # 要等 LightRAG 可用后的下一轮 scan_and_sync 才发生。
        try:
            from niu_api.internal.lightrag_manager import get_lightrag
            if get_lightrag() is None:
                logger.debug("[SkillSync] LightRAG not available, skipping scan_and_sync")
                return -1, -1, -1
        except Exception as e:
            logger.warning(f"[SkillSync] LightRAG availability check failed: {e}")
            return -1, -1, -1

        # 1. 从状态文件加载 known_skills = {name: hash}
        with self._lock:
            known_skills = dict(self._last_scan)

        # 2. 扫描 skills 目录，计算当前 current_hashes = {name: hash}
        current_hashes: dict[str, str] = {}
        for skill_file in self.skills_dir.glob("*.md"):
            name = skill_file.stem
            try:
                content_hash = self._compute_file_hash(skill_file)
            except OSError as e:
                logger.error(f"[SkillSync] Cannot read skill file {name}: {e}")
                continue
            current_hashes[name] = content_hash

        added, updated, deleted = 0, 0, 0
        # 最终写入状态文件的 dict，初始值为 known_skills 的副本
        next_scan: dict[str, str] = dict(known_skills)

        # 3. 对比检测变化
        for name, current_hash in current_hashes.items():
            known_hash = known_skills.get(name)
            if known_hash is None:
                # 新增
                skill_file = self.skills_dir / f"{name}.md"
                if self._sync_skill(name, skill_file):
                    next_scan[name] = current_hash  # 成功才写入新 hash
                    added += 1
                    logger.info(f"[SkillSync] Added skill: {name}")
                else:
                    logger.error(f"[SkillSync] Failed to add skill {name}, will retry next scan")
                    # 不写入 next_scan，下次扫描仍视为新增
            elif known_hash != current_hash:
                # 修改：先删旧再注新，任一步失败都保留旧 hash
                if self._delete_skill_from_lightrag(name):
                    skill_file = self.skills_dir / f"{name}.md"
                    if self._sync_skill(name, skill_file):
                        next_scan[name] = current_hash  # 成功才更新 hash
                        updated += 1
                        logger.info(f"[SkillSync] Updated skill: {name} (content changed)")
                    else:
                        logger.error(f"[SkillSync] Failed to inject updated skill {name}, keeping old hash for retry")
                        next_scan[name] = known_hash
                else:
                    logger.error(f"[SkillSync] Failed to delete old skill {name}, keeping old hash for retry")
                    next_scan[name] = known_hash
            else:
                # 不变：hash 相同 → 保留
                next_scan[name] = current_hash

        # 删除：在 known_skills 中但不在 current_hashes 中 → 从图谱删除
        step3_deleted: set[str] = set()
        for name in known_skills:
            if name not in current_hashes:
                if self._delete_skill_from_lightrag(name):
                    next_scan.pop(name, None)
                    deleted += 1
                    step3_deleted.add(name)
                    logger.info(f"[SkillSync] Deleted skill: {name}")
                else:
                    logger.error(f"[SkillSync] Failed to delete '{name}' from LightRAG, keeping in state for retry")
                    next_scan[name] = known_skills[name]

        # 3.5 交叉比对：向量库中的 skill 实体 vs 磁盘文件
        # 从非 SkillSync 路径（ainsert/MCP工具）进入向量库的 skill 实体，
        # SkillSync 的 hash 对比看不到它们，需要单独清理
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
            kg_result = adapter.list_entities(list_type="entities", entity_type="skill", limit=200)
            if isinstance(kg_result, dict) and kg_result.get("status") == "ok":
                kg_skills = kg_result.get("data", [])
                current_hashes_lower = {k.lower() for k in current_hashes}
                for entity in kg_skills:
                    entity_name = entity.get("entity_name", "")
                    if entity_name and entity_name.lower() not in current_hashes_lower and entity_name not in step3_deleted:
                        # 向量库中有但磁盘上不存在 → 幽灵 skill，删除
                        if self._delete_skill_from_lightrag(entity_name):
                            logger.info(f"[SkillSync] Cleaned ghost skill '{entity_name}' from KG (not on disk)")
                            next_scan.pop(entity_name, None)
                            deleted += 1
                        else:
                            logger.warning(f"[SkillSync] Failed to delete ghost skill '{entity_name}' from KG")
                            next_scan[entity_name] = next_scan.get(entity_name, "")
        except Exception as e:
            logger.warning(f"[SkillSync] KG skill cleanup failed: {e}")

        # Scan notes
        try:
            note_added, note_updated = self._scan_notes()
            added += note_added
            updated += note_updated
        except Exception as e:
            logger.error(f"[SkillSync] Notes scan failed: {e}")

        # 4. 合并 watchdog 并发修改 + 出口对比 + 写盘成功才更新内存
        with self._lock:
            # 合并 scan 期间 watchdog 新增/修改的条目
            # 1. 不在 known_skills 快照中的 → watchdog 新增的，合并进 next_scan
            # 2. 在 known_skills 中但 hash 跟 _last_scan 不同 → watchdog 改了已有 key 的 hash
            #    （覆盖 next_scan 里 scan 算出的旧 hash，避免被覆盖回旧值）
            for name, hash_val in self._last_scan.items():
                if name not in known_skills or next_scan.get(name) != hash_val:
                    next_scan[name] = hash_val

            # 出口对比：_last_scan 或 _last_notes_scan 跟入口快照不同才写盘
            # 覆盖以下场景（added/updated/deleted 可能全 0 但状态确实变了）：
            # - watchdog 并发往 _last_scan 塞新条目或改已有 key 的 hash
            # - KG ghost cleanup 失败时往 next_scan 塞空字符串（L378）
            # - _scan_notes 修改了 _last_notes_scan
            skills_changed = next_scan != skills_snapshot_before
            notes_changed = self._last_notes_scan != notes_snapshot_before

        if skills_changed or notes_changed:
            # 写盘成功后才更新内存，避免 _save_state 抛 OSError 时
            # 内存已改但磁盘未改，下次 scan 入口快照读到新内存状态，
            # 出口对比"无变化"不写盘，磁盘永远旧状态
            self._save_state()
            with self._lock:
                self._last_scan = next_scan

        return added, updated, deleted

    def _sync_skill(self, name: str, skill_file: Path) -> bool:
        """同步单个 skill 到 LightRAG 知识图谱

        读取文件全文，调用 _inject_skill_to_lightrag 做结构化注入。

        Returns:
            True on success, False on failure.
        """
        try:
            content = skill_file.read_text(encoding="utf-8")
            return self._inject_skill_to_lightrag(name, content)
        except Exception as e:
            logger.error(f"[SkillSync] Failed to sync skill {name}: {e}")
            return False

    def _get_ingester(self):
        """Get LightRAGIngester instance."""
        from niu_api.internal.lightrag_adapter import LightRAGIngester
        return LightRAGIngester()

    def _inject_skill_to_lightrag(self, skill_name: str, content: str) -> bool:
        """Inject a skill entity into the LightRAG knowledge graph.

        Uses inject_custom_kg (structured injection) so that:
        - Entity name is the skill name directly (natural language), no LLM auto-extraction drift
        - Description is the primary vector-matching key, written precisely
        - Same-name entity merges on re-injection (no duplicates)
        - belongs_to_region edge links skill to 知识体系脑区

        注入前会检查实体是否已存在：如果已存在则先通过 ToolRegistry 调用
        lightrag_delete_entity 删除旧实体，再注入新实体。
        这样即使 scan_and_sync 中的 _delete_skill_from_lightrag 删除失败，
        此处也能补救，避免"旧实体删不掉 + 新实体被 dedup 跳过"的双重失败。

        Args:
            skill_name: Skill name (natural language, e.g., "photo-processing").
            content: Full text content of the skill file.

        Returns:
            True on success, False on failure.
        """
        try:
            # 防御性删除：注入前检查实体是否已存在，若存在则先删除
            # 场景：scan_and_sync 的 _delete_skill_from_lightrag 可能失败，
            # 而 MCP 层 lightrag_insert_custom_kg 有 dedup 检查会跳过已存在实体，
            # 导致 skill 更新彻底失败。此处补救：发现已存在就先删再注。
            try:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
                if adapter.has_entity(skill_name):
                    logger.info(
                        "[SkillSync] Entity '%s' already exists before injection, deleting first",
                        skill_name,
                    )
                    try:
                        from agent.tool_registry import get_registry
                        registry = get_registry()
                        delete_fn = registry.get("lightrag-server/lightrag_delete_entity")
                        if delete_fn is not None:
                            del_result = delete_fn(entity_name=skill_name)
                            if isinstance(del_result, dict) and del_result.get("status") == "ok":
                                logger.info(
                                    "[SkillSync] Pre-inject delete succeeded for '%s'",
                                    skill_name,
                                )
                            else:
                                logger.warning(
                                    "[SkillSync] Pre-inject delete returned non-ok for '%s': %s",
                                    skill_name,
                                    del_result.get("message", "") if isinstance(del_result, dict) else del_result,
                                )
                        else:
                            # ToolRegistry 不可用时降级到 adapter 直接删除
                            logger.warning(
                                "[SkillSync] lightrag_delete_entity not in registry, fallback to adapter"
                            )
                            del_result = adapter.delete_entity(skill_name)
                            if isinstance(del_result, dict) and del_result.get("status") != "ok":
                                logger.warning(
                                    "[SkillSync] Fallback adapter delete returned non-ok for '%s': %s",
                                    skill_name,
                                    del_result.get("message", ""),
                                )
                    except Exception as del_err:
                        # 删除失败不阻断注入，inject_custom_kg 本身是 upsert 语义
                        logger.warning(
                            "[SkillSync] Pre-inject delete failed for '%s' (will try inject anyway): %s",
                            skill_name,
                            del_err,
                        )
            except Exception as check_err:
                # has_entity 检查失败不阻断注入，继续走正常注入流程
                logger.debug(
                    "[SkillSync] has_entity check failed for '%s' (proceeding with inject): %s",
                    skill_name,
                    check_err,
                )

            ingester = self._get_ingester()

            entity_name = skill_name
            source_id = f"skill://{skill_name}"

            # Extract frontmatter for a concise description
            fm = parse_yaml_frontmatter(content)
            description = extract_description(content, fm)

            if not description:
                logger.warning(
                    "[SkillSync] Skill '%s' has no description, skipping injection",
                    skill_name,
                )
                return False

            # 标记草稿 skill（fm 已在上方由 parse_yaml_frontmatter 解析）
            status = fm.get("status") if isinstance(fm, dict) else None
            if status == "draft":
                description = f"[草稿] {description}"
            elif status == "deprecated":
                description = f"[待观察] {description}"

            # Build triggers/tags for enriched description
            triggers = extract_triggers(content, fm)
            tags = extract_tags(content, fm, triggers)

            extra_parts = []
            if triggers:
                extra_parts.append(f"触发词: {', '.join(triggers)}")
            if tags:
                extra_parts.append(f"标签: {', '.join(tags)}")
            full_description = description
            if extra_parts:
                full_description += " | " + "; ".join(extra_parts)

            entities = [{
                "entity_name": entity_name,
                "entity_type": "Skill",
                "description": full_description,
                "source_id": source_id,
            }]

            relationships = [{
                "src_id": "知识体系脑区",
                "tgt_id": entity_name,
                "keywords": BELONGS_TO_RELATION,
                "description": f"{skill_name} 属于知识体系",
                "source_id": source_id,
                "weight": 1.0,
            }]

            chunks = [{
                "content": f"Skill: {skill_name}\n\n{content}",
                "source_id": source_id,
            }]

            result = ingester.inject_custom_kg(
                entities=entities,
                relationships=relationships,
                chunks=chunks,
                source_id=source_id,
            )
            if isinstance(result, dict) and result.get("status") == "ok":
                logger.info("[SkillSync] Injected skill '%s' into KG via inject_custom_kg", skill_name)
                return True
            else:
                logger.warning(
                    "[SkillSync] inject_custom_kg returned non-ok for '%s': %s",
                    skill_name,
                    result.get("message", "") if isinstance(result, dict) else result,
                )
                return False
        except Exception as e:
            logger.warning("[SkillSync] LightRAG skill inject failed for '%s': %s", skill_name, e)
            return False

    def _delete_skill_from_lightrag(self, skill_name: str) -> bool:
        """从 LightRAG 知识图谱删除 skill 节点

        调用 adapter.delete_entity(entity_name) 删除实体。
        skill 的实体名就是 skill_name 本身（自然语言命名，与注入时一致）。

        Args:
            skill_name: skill 名称（自然语言，不含前缀）

        Returns:
            True if deletion succeeded, False otherwise.
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter

            adapter = LightRAGAdapter()
        except Exception as e:
            logger.warning("[SkillSync] LightRAG adapter construction failed for '%s': %s", skill_name, e)
            return False

        entity_name = skill_name
        try:
            result = adapter.delete_entity(entity_name)
            if isinstance(result, dict) and result.get("status") == "ok":
                logger.info("[SkillSync] Deleted skill '%s' from KG (entity: %s)", skill_name, entity_name)
                return True
            else:
                logger.warning(
                    "[SkillSync] delete_entity returned non-ok for '%s': %s",
                    skill_name,
                    result.get("message", "") if isinstance(result, dict) else result,
                )
                return False
        except Exception as e:
            logger.warning("[SkillSync] LightRAG skill delete failed for '%s': %s", skill_name, e)
            return False

    def _scan_notes(self) -> tuple[int, int]:
        """扫描 workspace/notes/notes.json，将变化同步到 LightRAG"""
        ws = os.environ.get("WORKSPACE_PATH", "")
        if not ws:
            return 0, 0
        notes_path = Path(ws) / "notes" / "notes.json"
        if not notes_path.exists():
            return 0, 0

        try:
            notes = json.loads(notes_path.read_text(encoding="utf-8"))
            if not isinstance(notes, list):
                logger.warning(f"[SkillSync] notes.json is not a list")
                return 0, 0
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SkillSync] Failed to read notes.json: {e}")
            return 0, 0

        added, updated = 0, 0
        current_ids: set[str] = set()

        # Collect changed notes for full-file injection
        changed_notes: list[dict] = []
        old_hashes: dict[str, Optional[str]] = {}  # note_id -> old_hash for rollback
        updated_note_ids: set[str] = set()  # note_ids that were modified (not new)

        for note in notes:
            note_id = note.get("id", "")
            if not note_id:
                continue
            current_ids.add(note_id)

            content_hash = hashlib.sha256(
                json.dumps({"c": note.get("content") or "", "t": note.get("tags") or []}, sort_keys=True).encode()
            ).hexdigest()
            last_hash = self._last_notes_scan.get(note_id)
            old_hashes[note_id] = last_hash

            if last_hash is None:
                changed_notes.append(note)
                with self._lock:
                    self._last_notes_scan[note_id] = content_hash
                added += 1
                logger.info(f"[SkillSync] Added note: {note_id}")
            elif last_hash != content_hash:
                changed_notes.append(note)
                with self._lock:
                    self._last_notes_scan[note_id] = content_hash
                updated_note_ids.add(note_id)
                updated += 1
                logger.info(f"[SkillSync] Updated note: {note_id}")
            else:
                # Unchanged — already synced
                with self._lock:
                    self._last_notes_scan[note_id] = content_hash

        # Inject all changed notes as a single full-file document
        if changed_notes:
            failed_ids = self._inject_note_to_lightrag(changed_notes, updated_ids=updated_note_ids)
            if failed_ids:
                # Rollback: only restore hashes for failed notes
                with self._lock:
                    for note in changed_notes:
                        nid = note.get("id", "")
                        if nid and nid in failed_ids:
                            old_h = old_hashes.get(nid)
                            if old_h is not None:
                                self._last_notes_scan[nid] = old_h
                            else:
                                self._last_notes_scan.pop(nid, None)
                # Adjust counts for failures
                added -= sum(1 for n in changed_notes if n.get("id", "") in failed_ids and n.get("id", "") not in updated_note_ids)
                updated -= sum(1 for n in changed_notes if n.get("id", "") in failed_ids and n.get("id", "") in updated_note_ids)

        # Detect deleted notes
        with self._lock:
            deleted_ids = [nid for nid in self._last_notes_scan if nid not in current_ids]

        if deleted_ids:
            try:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
                for note_id in deleted_ids:
                    try:
                        doc_id = f"note:{note_id}"
                        result = adapter.delete_document(doc_id)
                        if isinstance(result, dict) and result.get("status") == "ok":
                            with self._lock:
                                self._last_notes_scan.pop(note_id, None)
                            logger.info(f"[SkillSync] Deleted note: {note_id} (doc_id={doc_id})")
                        else:
                            logger.warning(f"[SkillSync] Note deletion failed for {note_id}: {result}")
                    except Exception as e:
                        # Keep hash so deletion is retried on next scan
                        logger.warning(f"[SkillSync] Failed to delete note {note_id}: {e}")
            except Exception as e:
                logger.warning(f"[SkillSync] LightRAG unavailable for note deletion: {e}")

        # notes 状态持久化统一在 scan_and_sync 末尾出口对比时处理
        # （对比 _last_notes_scan 跟入口快照，无变化不写盘）
        return added, updated

    def _inject_note_to_lightrag(self, notes_data: list[dict], updated_ids: set[str] | None = None) -> set[str]:
        """将便签注入 LightRAG，每个 note 使用独立 doc_id 以便按文档级联删除

        Args:
            notes_data: 便签数据列表，每项包含 id/content/tags 等字段
            updated_ids: 修改过的 note_id 集合，注入前先删除旧文档避免去重拒绝

        Returns:
            失败的 note_id 集合（空集合表示全部成功）
        """
        if not notes_data:
            return set()

        # 对修改的 note 先删除旧文档，避免 LightRAG ainsert 去重拒绝
        if updated_ids:
            try:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
                for note_id in updated_ids:
                    doc_id = f"note:{note_id}"
                    del_result = adapter.delete_document(doc_id)
                    if isinstance(del_result, dict) and del_result.get("status") != "ok":
                        logger.warning("[SkillSync] Failed to delete old note doc %s: %s", doc_id, del_result.get("message", ""))
            except Exception as e:
                logger.warning("[SkillSync] Failed to delete old note documents: %s", e)

        try:
            import json

            from agent.tool_registry import get_registry

            registry = get_registry()
            insert_tool = registry.get("lightrag-server/lightrag_insert")
            if insert_tool is None:
                logger.warning("lightrag-server/lightrag_insert tool not found in registry")
                return {n.get("id", "") for n in notes_data if n.get("id")}

            failed_ids: set[str] = set()
            for note in notes_data:
                note_id = note.get("id", "")
                if not note_id:
                    continue
                content = json.dumps(note, ensure_ascii=False, indent=2)
                doc_id = f"note:{note_id}"
                result = insert_tool(content=content, doc_id=doc_id)
                if isinstance(result, dict) and result.get("status") == "ok":
                    logger.debug(f"[SkillSync] Injected note '{note_id}' with doc_id={doc_id}")
                else:
                    logger.warning("[SkillSync] lightrag_insert returned non-ok for note '%s': %s", note_id, result)
                    failed_ids.add(note_id)

            return failed_ids
        except Exception:
            logger.exception("Failed to inject notes to LightRAG")
            return {n.get("id", "") for n in notes_data if n.get("id")}

    def _start_watchdog(self):
        """启动 watchdog 监控"""
        if not self.use_watchdog or self._observer:
            return

        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return

        try:
            handler = SkillFileHandler(self, debounce=1.0)
            if Observer is None:
                logger.error("[SkillSync] Observer not available, cannot start watchdog")
                return
            observer = Observer()
            observer.schedule(handler, str(self.skills_dir), recursive=False)
            observer.start()
            self._observer = observer
            logger.info(f"[SkillSync] Started watchdog monitoring: {self.skills_dir}")
        except Exception as e:
            logger.error(f"[SkillSync] Failed to start watchdog: {e}")
            self._observer = None

    def _stop_watchdog(self):
        """停止 watchdog 监控"""
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
                self._observer = None
                logger.info("[SkillSync] Stopped watchdog monitoring")
            except Exception as e:
                logger.error(f"[SkillSync] Failed to stop watchdog: {e}")

    def start_background_sync(self):
        """启动后台同步"""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        # 启动 watchdog（如果启用）
        if self.use_watchdog:
            self._start_watchdog()

        # 保留定时扫描作为 fallback
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(f"[SkillSync] Started background sync (interval: {self.scan_interval}s, watchdog: {self.use_watchdog})")

    def stop_background_sync(self):
        """停止后台同步"""
        self._stop_event.set()

        # 停止 watchdog
        if self.use_watchdog:
            self._stop_watchdog()

        if self._thread:
            self._thread.join(timeout=5)

    def _sync_loop(self):
        """后台同步循环（异常不会终止线程）"""
        logger.info("[SkillSync] Waiting for LightRAG to be ready before first scan...")
        # Wait for LightRAG readiness signal instead of fixed delay.
        # If LightRAG init succeeds quickly, we start immediately;
        # if it fails or takes longer, we wait up to 30s then proceed.
        if not wait_lightrag_ready(timeout=30):
            # Timeout — try to trigger init ourselves
            from niu_api.internal.lightrag_manager import get_lightrag
            rag = get_lightrag()
            if rag is None:
                logger.warning("[SkillSync] LightRAG not available, proceeding anyway")
            else:
                logger.info("[SkillSync] LightRAG initialized on retry")
        while not self._stop_event.is_set():
            try:
                scan_result = self.scan_and_sync()
            except Exception as e:
                logger.error(f"[SkillSync] scan_and_sync failed: {e}", exc_info=True)
                scan_result = None
            # 首次 scan_and_sync 真正跑完（非 LightRAG 不可用的提前返回）才 set。
            # 若 scan_and_sync 返回 (-1, -1, -1) 表示 LightRAG 还不可用，
            # 不算"首次扫描完成"，下一轮再尝试。
            if not self._first_scan_complete.is_set():
                if scan_result is not None and scan_result[0] != -1:
                    self._first_scan_complete.set()
                    logger.info(
                        f"[SkillSync] First scan complete (added={scan_result[0]}, "
                        f"updated={scan_result[1]}, deleted={scan_result[2]}) — signal sent to waiters"
                    )
                else:
                    logger.debug("[SkillSync] First scan skipped (LightRAG not ready), will retry")
            self._stop_event.wait(self.scan_interval)


# 全局实例
_skill_sync: Optional[SkillSync] = None
_skill_sync_lock = threading.Lock()


def get_skill_sync(skills_dir: Optional[str] = None, auto_start: bool = True) -> SkillSync:
    """获取全局 SkillSync 实例（线程安全）"""
    global _skill_sync
    if _skill_sync is None:
        with _skill_sync_lock:
            if _skill_sync is None:
                instance = SkillSync(skills_dir)
                if auto_start:
                    instance.start_background_sync()
                _skill_sync = instance
    return _skill_sync


def wait_first_scan_complete(timeout: float = 90.0) -> bool:
    """等待 SkillSync 首次 scan_and_sync 完成（或超时）。

    供 run_repair_on_user_request 在 get_lightrag 后调用，
    确保 SkillSync 的 ghost skill 清理已跑完，再重检 + 二次 repair。

    Args:
        timeout: 最大等待秒数（默认 90s = 30s wait_lightrag_ready + 60s scan_interval）

    Returns:
        True 如果首次扫描完成；False 如果超时。
    """
    global _skill_sync
    if _skill_sync is None:
        # SkillSync 未启动（如测试环境）→ 视为已完成
        return True
    return _skill_sync._first_scan_complete.wait(timeout=timeout)

def parse_yaml_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter（--- 包裹的头部区域）

    Returns:
        解析后的字典，解析失败返回空字典
    """
    stripped = content.strip()
    if not stripped.startswith("---"):
        return {}

    end = stripped.find("---", 3)
    if end == -1:
        return {}

    yaml_text = stripped[3:end].strip()
    if not yaml_text:
        return {}

    if YAML_AVAILABLE:
        try:
            result = yaml.safe_load(yaml_text)  # type: ignore[union-attr]
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.debug(f"YAML frontmatter parse failed: {e}")

    # Fallback: 简单正则解析（无 PyYAML 时）
    # 注意：仅支持标量值和内联列表 [a, b]，多行列表需安装 PyYAML
    parsed = {}
    for line in yaml_text.split("\n"):
        match = re.match(r"^(\w+)\s*:\s*(.+)$", line.strip())
        if match:
            key, value = match.group(1), match.group(2).strip().strip("\"'")
            # Handle inline list syntax: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
                parsed[key] = items
            else:
                parsed[key] = value
    if not YAML_AVAILABLE and any(k in ("triggers", "tags") for k in parsed):
        logger.warning("PyYAML not installed, YAML list fields (triggers/tags) may not parse correctly")
    return parsed


def extract_description(content: str, fm: dict | None = None) -> str:
    """从 skill 内容提取描述

    优先级：
    1. YAML frontmatter description（"Use when..." 格式，CSO 最佳实践）
    2. 第一个 # 标题（降级）
    3. 默认：拒绝同步
    """
    # 优先级 1: YAML frontmatter description
    if fm is None:
        fm = parse_yaml_frontmatter(content)
    fm_desc = fm.get("description", "")
    if fm_desc and isinstance(fm_desc, str) and fm_desc.strip():
        return fm_desc.strip()

    # 优先级 2: 第一个 # 标题（降级）
    # Strip frontmatter before scanning for titles
    scan_content = content
    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            scan_content = stripped[end + 3:]

    lines = scan_content.strip().split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("# "):
            logger.warning("Skill 缺少 YAML frontmatter description，使用标题降级")
            return line[2:].strip()

    # 默认：拒绝同步
    logger.error("Skill 缺少 YAML frontmatter description，无法同步到 LightRAG")
    return ""


def extract_triggers(content: str, fm: dict | None = None) -> list[str]:
    """从 skill 内容提取触发词

    优先级：YAML frontmatter triggers > Markdown 触发关键词 > triggers: []
    """
    triggers = []

    # 优先级 1: YAML frontmatter triggers
    if fm is None:
        fm = parse_yaml_frontmatter(content)
    fm_triggers = fm.get("triggers")
    if fm_triggers is not None:
        if isinstance(fm_triggers, list) and fm_triggers:
            triggers.extend(str(t) for t in fm_triggers)
        elif isinstance(fm_triggers, str):
            triggers.extend(re.split(r"[,，、]", fm_triggers))
            triggers = [t.strip() for t in triggers if t.strip()]

    # 优先级 2: **触发关键词**：xxx、yyy (Markdown 加粗)
    if not triggers:
        match_bold = re.search(r"\*\*触发关键词\*\*[：:]\s*(.+)", content)
        if match_bold:
            keywords = match_bold.group(1)
            triggers.extend(re.split(r"[,，、]", keywords))
            triggers = [t.strip() for t in triggers if t.strip()]

    # 优先级 3: 触发关键词：xxx、yyy (无加粗)
    if not triggers:
        match1 = re.search(r"触发关键词[：:]\s*(.+)", content)
        if match1:
            keywords = match1.group(1)
            triggers.extend(re.split(r"[,，、]", keywords))
            triggers = [t.strip() for t in triggers if t.strip()]

    # 优先级 4: triggers: [xxx, yyy]
    if not triggers:
        match2 = re.search(r"triggers:\s*\[(.+)\]", content, re.IGNORECASE)
        if match2:
            keywords = match2.group(1)
            triggers.extend([t.strip().strip("\"'") for t in keywords.split(",")])

    return list(set(triggers))[:10]


def extract_tags(content: str, fm: dict | None = None, triggers: list[str] | None = None) -> list[str]:
    """从 skill 内容提取标签

    优先级：YAML frontmatter tags > tags: [] > 标签：xxx
    """
    tags = []

    # 优先级 1: YAML frontmatter tags
    if fm is None:
        fm = parse_yaml_frontmatter(content)
    fm_tags = fm.get("tags")
    if fm_tags is not None:
        if isinstance(fm_tags, list) and fm_tags:
            tags.extend(str(t) for t in fm_tags)
        elif isinstance(fm_tags, str):
            tags.extend(re.split(r"[,，、]", fm_tags))
            tags = [t.strip() for t in tags if t.strip()]

    # 优先级 2: tags: [xxx, yyy]
    if not tags:
        match1 = re.search(r"tags:\s*\[(.+)\]", content, re.IGNORECASE)
        if match1:
            keywords = match1.group(1)
            tags.extend([t.strip().strip("\"'") for t in keywords.split(",")])

    # 优先级 3: 标签：xxx、yyy
    if not tags:
        match2 = re.search(r"标签[：:]\s*(.+)", content)
        if match2:
            keywords = match2.group(1)
            tags.extend(re.split(r"[,，、]", keywords))
            tags = [t.strip() for t in tags if t.strip()]

    # 从触发词中提取标签（触发词也可以作为标签）
    if triggers is None:
        triggers = extract_triggers(content, fm)
    for t in triggers:
        if t not in tags:
            tags.append(t)

    return list(set(tags))[:10]
