"""
Skill Sync

Skills 目录同步服务。定时扫描 memory/skills/ 目录，同步变化到 LightRAG 知识图谱。
通过 entity_type="skill" 标签区分，供 LightRAGAdapter.search_skills() 检索。
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

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
    Observer = None
    FileSystemEventHandler = object  # 占位符


class SkillFileHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
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
            if not event.is_directory and event.src_path.endswith(".md"):
                logger.debug(f"[SkillFileHandler] File created: {event.src_path}")
                self._schedule_sync(event.src_path, "sync")

    def on_modified(self, event):
        """文件修改事件"""
        if WATCHDOG_AVAILABLE and isinstance(event, FileModifiedEvent):
            if not event.is_directory and event.src_path.endswith(".md"):
                # 过滤 self_writing
                try:
                    mtime = Path(event.src_path).stat().st_mtime
                    if not self._sync._is_self_write(event.src_path, mtime):
                        logger.debug(f"[SkillFileHandler] File modified: {event.src_path}")
                        self._schedule_sync(event.src_path, "sync")
                except Exception as e:
                    logger.warning(f"[SkillFileHandler] Failed to check mtime: {e}")

    def on_deleted(self, event):
        """文件删除事件"""
        if WATCHDOG_AVAILABLE and isinstance(event, FileDeletedEvent):
            if not event.is_directory and event.src_path.endswith(".md"):
                logger.debug(f"[SkillFileHandler] File deleted: {event.src_path}")
                self._schedule_sync(event.src_path, "delete")

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
                self._sync._sync_skill(name, Path(path))
            elif action == "delete":
                self._sync._delete_skill(name)
        except Exception as e:
            logger.error(f"[SkillFileHandler] Failed to execute {action} for {path}: {e}")


class SkillSync:
    """
    Skills 目录同步服务

    扫描 memory/skills/ 目录，检测文件变化，同步到 LightRAG 知识图谱。
    通过 entity_type="skill" 标签区分，供 LightRAGAdapter.search_skills() 检索。
    """

    def __init__(self, skills_dir: str = None, scan_interval: int = 60, use_watchdog: bool = True):
        self.skills_dir = Path(skills_dir or self._default_skills_dir())
        self.scan_interval = scan_interval
        self.use_watchdog = use_watchdog and WATCHDOG_AVAILABLE

        # 记录上次扫描状态 {skill_name: mtime}
        self._last_scan: dict[str, float] = {}
        self._last_notes_scan: dict[str, int] = {}  # note_id -> content hash
        self._lock = threading.Lock()  # 线程锁

        # 后台线程
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # watchdog 相关
        self._observer: Optional[Observer] = None

        # self_writing 检测
        self._last_write_times: dict[str, float] = {}  # path -> mtime
        self._self_write_window = 2.0  # 秒，自己写入后的冷却时间

    @staticmethod
    def _default_skills_dir() -> str:
        """默认 skills 目录"""
        base_dir = Path(__file__).parent.parent.parent
        return str(base_dir / "memory" / "skills")

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

    def scan_and_sync(self) -> tuple[int, int, int]:
        """
        扫描目录，同步变化的 skills 到 LightRAG 知识图谱

        Returns:
            (added, updated, deleted) 计数
        """
        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return 0, 0, 0

        # 首次扫描时，从 LightRAG 加载已有 skill 状态，避免重复 "Added"
        with self._lock:
            if not self._last_scan:
                self._load_existing_skills()

        current: dict[str, float] = {}
        added, updated, deleted = 0, 0, 0
        synced_names: set[str] = set()  # 本轮已同步的 skill 名称

        # 扫描所有 .md 文件
        for skill_file in self.skills_dir.glob("*.md"):
            name = skill_file.stem
            mtime = skill_file.stat().st_mtime
            current[name] = mtime

            # 新增或修改（需要锁保护读取）
            with self._lock:
                last_scan = self._last_scan.copy()

            if name not in last_scan:
                try:
                    self._sync_skill(name, skill_file)
                    synced_names.add(name)
                    added += 1
                    logger.info(f"[SkillSync] Added skill: {name}")
                except Exception as e:
                    logger.error(f"[SkillSync] Failed to add skill {name}: {e}")
            elif mtime > last_scan[name]:
                try:
                    self._sync_skill(name, skill_file)
                    synced_names.add(name)
                    updated += 1
                    logger.info(f"[SkillSync] Updated skill: {name}")
                except Exception as e:
                    logger.error(f"[SkillSync] Failed to update skill {name}: {e}")

        # Scan notes
        try:
            note_added, note_updated = self._scan_notes()
            added += note_added
            updated += note_updated
        except Exception as e:
            logger.error(f"[SkillSync] Notes scan failed: {e}")

        # 检测删除
        with self._lock:
            last_scan = self._last_scan.copy()

        for name in last_scan:
            if name not in current:
                try:
                    self._delete_skill(name)
                    deleted += 1
                    logger.info(f"[SkillSync] Deleted skill: {name}")
                except Exception as e:
                    logger.error(f"[SkillSync] Failed to delete skill {name}: {e}")

        # 检测 LightRAG 中 skill 被外部删除（需要回写）
        # 只检查 last_scan 中已有但 LightRAG 缺失的 skill（跳过本轮新增的）
        if last_scan:
            try:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
                result = adapter.list_entities(entity_type="skill", limit=500)
                if result.get("status") != "ok":
                    logger.debug(f"[SkillSync] list_entities unavailable: {result.get('message', '')}")
                else:
                    existing_names = set()
                    for entity in result.get("data", []):
                        ename = entity.get("id", "")
                        if ename.startswith("skill:"):
                            existing_names.add(ename[6:])

                    for name in last_scan:
                        if name not in synced_names and name in current and name not in existing_names:
                            skill_file = self.skills_dir / f"{name}.md"
                            if skill_file.exists():
                                self._sync_skill(name, skill_file)
                                added += 1
                                logger.info(f"[SkillSync] Re-added missing skill: {name}")
            except Exception as e:
                logger.debug(f"[SkillSync] Failed to check missing skills in LightRAG: {e}")

        # 更新状态（需要锁保护写入）
        with self._lock:
            self._last_scan = current

        return added, updated, deleted

    def _load_existing_skills(self):
        """从 LightRAG 加载已有 skill，使用磁盘文件的实际 mtime"""
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
            result = adapter.list_entities(entity_type="skill", limit=500)
            if result.get("status") != "ok":
                logger.debug(f"[SkillSync] list_entities unavailable: {result.get('message', '')}")
                return
            for entity in result.get("data", []):
                ename = entity.get("id", "")
                if ename.startswith("skill:"):
                    name = ename[6:]
                    skill_file = self.skills_dir / f"{name}.md"
                    if skill_file.exists():
                        self._last_scan[name] = skill_file.stat().st_mtime
                    else:
                        # 文件已删除但 LightRAG 中还有，用 inf 标记以便下次检测到删除
                        self._last_scan[name] = float('inf')
            if self._last_scan:
                logger.info(f"[SkillSync] Loaded {len(self._last_scan)} existing skills from LightRAG")
        except Exception as e:
            logger.debug(f"[SkillSync] Failed to load existing skills from LightRAG: {e}")

    def _sync_skill(self, name: str, skill_file: Path):
        """同步单个 skill 到 LightRAG 知识图谱"""
        try:
            content = skill_file.read_text(encoding="utf-8")
            fm = self._parse_yaml_frontmatter(content)
            triggers = self._extract_triggers(content, fm)
            description = self._extract_description(content, fm)
            tags = self._extract_tags(content, fm, triggers)

            if description:
                self._inject_skill_to_lightrag(name, description, tags, triggers)

        except Exception as e:
            logger.error(f"[SkillSync] Failed to sync skill {name}: {e}")

    def _inject_skill_to_lightrag(
        self,
        name: str,
        description: str,
        tags: list[str],
        triggers: list[str],
    ):
        """Inject a skill entity into the LightRAG knowledge graph.

        Wrapped in try/except so LightRAG failures never break skill sync.
        The skill entity uses entity_type="skill" so that
        LightRAGAdapter.search_skills() can find it.
        """
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester

            ingester = LightRAGIngester()

            # Build a richer description from triggers + tags
            extra_parts = []
            if triggers:
                extra_parts.append(f"触发词: {', '.join(triggers)}")
            if tags:
                extra_parts.append(f"标签: {', '.join(tags)}")
            full_description = description
            if extra_parts:
                full_description += " | " + "; ".join(extra_parts)

            result = ingester.inject_entity(
                name=f"skill:{name}",
                entity_type="skill",
                description=full_description,
                source_id=f"skill:{name}",
                chunk_content=full_description,
                file_path=f"skill://{name}",
            )
            if result.get("status") == "ok":
                logger.info(f"[SkillSync] Injected skill '{name}' into LightRAG")
            else:
                logger.warning(f"[SkillSync] LightRAG inject failed for '{name}': {result.get('message', '')}")
        except Exception as e:
            # LightRAG not available or inject failed — non-fatal but visible
            logger.warning(f"[SkillSync] LightRAG skill inject failed for '{name}': {e}")

    def _delete_skill(self, name: str):
        """从 LightRAG 知识图谱删除 skill"""
        # Clean up internal state to prevent redundant delete attempts
        with self._lock:
            self._last_scan.pop(name, None)

        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
            result = adapter.delete_entity(f"skill:{name}")
            if result.get("status") == "ok":
                logger.debug(f"[SkillSync] Deleted skill '{name}' from LightRAG")
            else:
                logger.warning(f"[SkillSync] LightRAG delete failed for '{name}': {result.get('message', '')}")
        except Exception as e:
            logger.warning(f"[SkillSync] LightRAG skill delete failed for '{name}': {e}")

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
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SkillSync] Failed to read notes.json: {e}")
            return 0, 0

        added, updated = 0, 0
        current_ids: set[str] = set()

        for note in notes:
            note_id = note.get("id", "")
            if not note_id:
                continue
            current_ids.add(note_id)

            content_hash = hash(note.get("content", ""))
            last_hash = self._last_notes_scan.get(note_id)

            if last_hash is None:
                self._inject_note_to_lightrag(note_id, note.get("content", ""), note.get("tags", []))
                added += 1
                logger.info(f"[SkillSync] Added note: {note_id}")
            elif last_hash != content_hash:
                self._inject_note_to_lightrag(note_id, note.get("content", ""), note.get("tags", []))
                updated += 1
                logger.info(f"[SkillSync] Updated note: {note_id}")

            self._last_notes_scan[note_id] = content_hash

        # Detect deleted notes
        for note_id in list(self._last_notes_scan.keys()):
            if note_id not in current_ids:
                try:
                    from niu_api.internal.lightrag_adapter import LightRAGAdapter
                    adapter = LightRAGAdapter()
                    adapter.delete_entity(f"note:{note_id}")
                    logger.info(f"[SkillSync] Deleted note: {note_id}")
                except Exception as e:
                    logger.warning(f"[SkillSync] Failed to delete note {note_id}: {e}")
                self._last_notes_scan.pop(note_id, None)

        return added, updated

    def _inject_note_to_lightrag(self, note_id: str, content: str, tags: list[str]):
        """注入便签到 LightRAG 知识图谱"""
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester

            ingester = LightRAGIngester()
            description = content
            if tags:
                description += f" | 标签: {', '.join(tags)}"

            result = ingester.inject_entity(
                name=f"note:{note_id}",
                entity_type="knowledge",
                description=description,
                source_id=f"note:{note_id}",
                chunk_content=description,
                file_path=f"note://{note_id}",
            )
            if result.get("status") != "ok":
                logger.warning(f"[SkillSync] Note inject failed for {note_id}: {result.get('message', '')}")
        except Exception as e:
            logger.warning(f"[SkillSync] Note inject failed for {note_id}: {e}")

    def _parse_yaml_frontmatter(self, content: str) -> dict:
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
                result = yaml.safe_load(yaml_text)
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

    def _extract_triggers(self, content: str, fm: dict | None = None) -> list[str]:
        """从 skill 内容提取触发词

        优先级：YAML frontmatter triggers > Markdown 触发关键词 > triggers: []
        """
        triggers = []

        # 优先级 1: YAML frontmatter triggers
        if fm is None:
            fm = self._parse_yaml_frontmatter(content)
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

    def _extract_description(self, content: str, fm: dict | None = None) -> str:
        """从 skill 内容提取描述

        优先级：
        1. YAML frontmatter description（"Use when..." 格式，CSO 最佳实践）
        2. 第一个 # 标题（降级）
        3. 默认：拒绝同步
        """
        # 优先级 1: YAML frontmatter description
        if fm is None:
            fm = self._parse_yaml_frontmatter(content)
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

    def _extract_tags(self, content: str, fm: dict | None = None, triggers: list[str] | None = None) -> list[str]:
        """从 skill 内容提取标签

        优先级：YAML frontmatter tags > tags: [] > 标签：xxx
        """
        tags = []

        # 优先级 1: YAML frontmatter tags
        if fm is None:
            fm = self._parse_yaml_frontmatter(content)
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
            triggers = self._extract_triggers(content, fm)
        for t in triggers:
            if t not in tags:
                tags.append(t)

        return list(set(tags))[:10]

    def _start_watchdog(self):
        """启动 watchdog 监控"""
        if not self.use_watchdog or self._observer:
            return

        if not self.skills_dir.exists():
            logger.warning(f"[SkillSync] Skills directory not found: {self.skills_dir}")
            return

        try:
            handler = SkillFileHandler(self, debounce=1.0)
            self._observer = Observer()
            self._observer.schedule(handler, str(self.skills_dir), recursive=False)
            self._observer.start()
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
        while not self._stop_event.is_set():
            try:
                self.scan_and_sync()
            except Exception as e:
                logger.error(f"[SkillSync] scan_and_sync failed: {e}", exc_info=True)
            self._stop_event.wait(self.scan_interval)


# 全局实例
_skill_sync: Optional[SkillSync] = None
_skill_sync_lock = threading.Lock()


def get_skill_sync(skills_dir: str = None, auto_start: bool = True) -> SkillSync:
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
