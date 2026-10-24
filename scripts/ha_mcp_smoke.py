"""ha-server MCP 回归 harness（spec AC-5 / §9.2，plan §2 Task C、§3.3）。

对一次性 HA 容器跑 22 条命名断言 + 4 条附加断言，逐条打印
`CHECK <name> PASS|FAIL|SKIP`；**SKIP 计入失败**，有任何 FAIL/SKIP ⇒ 退出码非零。

前置 1 — 一次性 HA 容器（config 挂临时目录）：

    mkdir -p tmp-ha-config && cd tmp-ha-config
    printf '[]\\n' > automations.yaml && printf '{}\\n' > scripts.yaml && printf '[]\\n' > scenes.yaml
    cd ..
    docker run --rm --name ha-smoke -p 8127:8123 \\
        -v "$PWD/tmp-ha-config:/config" \\
        ghcr.io/home-assistant/home-assistant:2026.10.2   # 或 :stable（=2026.6.4）

  tmp-ha-config/configuration.yaml（可粘贴，spec §9.2）：

    default_config:
    automation: !include automations.yaml
    script: !include scripts.yaml
    scene: !include scenes.yaml
    input_boolean:
      mcp_test:
        name: MCP Test

  三个 !include 目标文件必须存在（automations.yaml=[] / scripts.yaml={} / scenes.yaml=[]）。
  断言依赖：CHECK control_turn_on 需要 input_boolean.mcp_test（--target-entity 可换）；
  CHECK integrate_flow_form 固定 handler=mqtt —— 两版本镜像的 mqtt 首步都返回 form（spec §7）。

前置 2 — 取 token：

  一次性容器（onboarding：/api/onboarding/users → /auth/token）：

    curl -s -X POST http://127.0.0.1:8127/api/onboarding/users \\
         -H 'Content-Type: application/json' \\
         -d '{"client_id":"http://127.0.0.1:8127","client_name":"smoke","type":"normal","language":"zh"}'
    # → {"id":1,"owner":true,"refresh_token":"<R>"}
    curl -s -X POST http://127.0.0.1:8127/auth/token \\
         -H 'Content-Type: application/x-www-form-urlencoded' \\
         -d 'grant_type=refresh_token&refresh_token=<R>'
    # → {"access_token":"<T>",...}  ← 用这个 <T>

  已有实例：HA Web UI → 用户头像 → 安全 → 长期访问令牌。

运行：

    cd <仓库根目录>
    python/bin/python scripts/ha_mcp_smoke.py --url http://127.0.0.1:8127 --token <T>
    # 中文名用例（plan §3.3.4）：同容器再跑一遍并加 --name-prefix "冒烟测试"
    # 期望：TOTAL 26 PASS 26 FAIL 0 SKIP 0，退出码 0

断言顺序：22 条按 setup → status → control → automation → scene → script → subscribe →
config flow → delete 排列；4 条附加断言插在依赖资源仍在的位置（scene_no_duplicate 紧跟
scene_activate、script_no_duplicate 紧跟 script_list_contains、status_filter_no_supplement
在删除段之前、scene_create_entity_exists 自建第二个场景）。

隔离：脚本启动即把 ha.CONFIG_PATH / ha.SERVICES_CACHE_PATH 指向 tempfile.mkdtemp()
（--config-dir 可覆盖），全程不读写真实 ~/.niu/ha-config.json。CHECK scene_delete 正是读这份
临时 config 校验 scene_name_map 已移除该 name，并由再次 ha_scene list 双重确认。

实体存在性断言（scene_list_contains / scene_create_entity_exists）读 /api/states 时轮询最多
~8s（0.5s 间隔）——HA 接受 config 写入后重载出新实体本身就有延迟，单次读取会偶发 FAIL。
niu_api.internal.ha_watcher 在导入阶段就被替换成 no-op 模块，ha_setup 不会拉起监视进程。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp-servers" / "ha-server" / "src"))

import niu_ha_server as ha  # noqa: E402, I001


# niu_ha_server 在 ha_setup / ha_status 里会 try: from niu_api.internal.ha_watcher import
# check_and_start —— 能否成功取决于 cwd / sys.path，一旦成功就会读真实 ~/.niu 配置并拉起监视
# 子进程，破坏 harness 的隔离。先塞一个 no-op 模块：import 优先命中 sys.modules，父包不存在
# 也不会报错，于是那条 try 分支恒定走空实现。
_watcher_stub = types.ModuleType("niu_api.internal.ha_watcher")
_watcher_stub.check_and_start = lambda: None  # type: ignore[attr-defined]
sys.modules.setdefault("niu_api.internal.ha_watcher", _watcher_stub)


class Runner:
    """跑断言 + 记台账：FAIL 与 SKIP 都算失败（spec §9.2 要求 0 skip）。"""

    def __init__(self):
        self.ctx: dict = {}
        self.counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}

    def check(self, name: str, fn) -> bool:
        try:
            ok, detail = fn()
        except Exception as exc:  # 断言过程中抛异常也算 FAIL，且不中断后续断言
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.counts["PASS" if ok else "FAIL"] += 1
        print(f"CHECK {name} {'PASS' if ok else 'FAIL'}" + ("" if ok else f" — {detail}"), flush=True)
        return ok

    def skip(self, name: str, reason: str) -> None:
        """前置不满足 ⇒ SKIP（同样计入失败），比让后续断言连环 FAIL 更好定位。"""
        self.counts["SKIP"] += 1
        print(f"CHECK {name} SKIP — {reason}", flush=True)

    def totals_line(self) -> str:
        """TOTAL 行：成功路径与早退路径共用同一字段顺序，便于人肉/脚本对账。"""
        total = sum(self.counts.values())
        return (f"TOTAL {total} PASS {self.counts['PASS']} "
                f"FAIL {self.counts['FAIL']} SKIP {self.counts['SKIP']}")

    @property
    def failed(self) -> bool:
        return bool(self.counts["FAIL"] or self.counts["SKIP"])


def state_ids(url: str, headers: dict) -> set:
    """直读 /api/states 的真实 entity_id 集合（AC-2 的第三方证据）。"""
    resp = ha._requests.get(f"{url}/api/states", headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"/api/states HTTP {resp.status_code}")
    return {e.get("entity_id", "") for e in resp.json()}


def wait_for_state(url: str, headers: dict, entity_id: str,
                   timeout: float = 8.0, interval: float = 0.5) -> tuple:
    """轮询 /api/states 直到 entity_id 出现（HA 接受 config 写入后重载实体本身有延迟）。

    返回 (是否出现, 最后一轮的 id 集合)；读取失败直接抛异常，由 Runner.check 记成 FAIL。
    """
    deadline = time.monotonic() + timeout
    while True:
        ids = state_ids(url, headers)
        if entity_id in ids:
            return True, ids
        if time.monotonic() >= deadline:
            return False, ids
        time.sleep(interval)


def rows_named(rows, name: str) -> list:
    return [row for row in rows if row.get("name") == name]


def main() -> int:
    ap = argparse.ArgumentParser(description="ha-server MCP 容器回归 harness（22+4 断言）")
    ap.add_argument("--url", required=True, help="HA 地址，如 http://127.0.0.1:8127")
    ap.add_argument("--token", required=True, help="Long-Lived Access Token")
    ap.add_argument("--config-dir", default="", help="临时 ha-config 目录（默认 tempfile.mkdtemp()）")
    ap.add_argument("--target-entity", default="input_boolean.mcp_test",
                    help="control/订阅用的靶实体（容器 config 需预置）")
    ap.add_argument("--name-prefix", default="MCP Smoke",
                    help="资源名前缀；中文名用例传中文前缀（plan §3.3.4）")
    args = ap.parse_args()

    cfg_dir = Path(args.config_dir) if args.config_dir else Path(tempfile.mkdtemp(prefix="ha-mcp-smoke-"))
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # 必须早于任何 ha_* 调用：否则 _read_config/_write_config 会碰真实 ~/.niu
    ha.CONFIG_PATH = str(cfg_dir / "ha-config.json")
    ha.SERVICES_CACHE_PATH = str(cfg_dir / "ha-services.json")
    print(f"# config dir: {cfg_dir}")
    print(f"# url: {args.url}  target: {args.target_entity}  prefix: {args.name_prefix}")

    target = args.target_entity
    auto_name = f"{args.name_prefix} Auto"
    scene_name = f"{args.name_prefix} Scene"
    scene2_name = f"{args.name_prefix} Scene 2"
    script_name = f"{args.name_prefix} Script"
    auto_config = {
        "triggers": [{"platform": "state", "entity_id": target, "to": "on"}],
        "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": target}}],
        "mode": "single",
    }
    scene_config = {"entities": {target: {"state": "on"}}}
    script_config = {
        "mode": "single",
        "sequence": [{"action": "input_boolean.turn_off", "target": {"entity_id": target}}],
    }

    r = Runner()

    # --- 1..2 连接 ---
    r.check("setup_connected", lambda: _expect(ha.ha_setup(args.url, args.token), connected=True))
    url, headers, err = ha._get_ha_client(ha._read_config())  # 写入的临时 config 必须能被读回
    if err:
        print(f"CHECK status_connected FAIL — 临时 config 无法解析出客户端: {err}", flush=True)
        r.counts["FAIL"] += 1
        print(r.totals_line(), flush=True)
        return 1
    status = ha.ha_status()
    r.check("status_connected", lambda: _expect(status, connected=True))

    # 靶实体缺失 ⇒ 前置不满足（SKIP ⇒ 整体失败）
    try:
        target_present = target in state_ids(url, headers)
    except Exception as exc:
        target_present = False
        print(f"# /api/states 读取失败: {exc}", flush=True)

    # --- 3 control ---
    if not target_present:
        r.skip("control_turn_on", f"{target} 不在 /api/states —— 容器 config 缺 input_boolean.mcp_test？")
    else:
        r.check("control_turn_on",
                lambda: _expect(ha.ha_control(target, service="turn_on"), success=True))

    # --- 4..9 automation CRUD + trigger/enable/disable ---
    def _auto_create():
        out = ha.ha_automation("create", name=auto_name, config=auto_config)
        r.ctx["auto_create"] = out
        if out.get("success") is not True:
            return False, f"返回 {out}"
        if not out.get("config_key"):
            return False, f"未返回 config_key: {out}"
        return True, ""
    r.check("automation_create", _auto_create)

    r.check("automation_list_contains", lambda: _contains_exactly_once(
        ha.ha_automation("list").get("automations", []), auto_name, "automations"))
    r.check("automation_get_config",
            lambda: _get_config_matches(ha.ha_automation("get", name=auto_name), auto_name))
    r.check("automation_trigger", lambda: _expect(ha.ha_automation("trigger", name=auto_name), success=True))
    r.check("automation_disable", lambda: _expect(ha.ha_automation("disable", name=auto_name), success=True))
    r.check("automation_enable", lambda: _expect(ha.ha_automation("enable", name=auto_name), success=True))

    # --- 10..12 scene create / list / activate ---
    def _scene_create():
        out = ha.ha_scene("create", name=scene_name, config=scene_config)
        r.ctx["scene_create"] = out
        return _expect(out, success=True)
    r.check("scene_create", _scene_create)

    def _scene_list_contains():
        listed = ha.ha_scene("list").get("scenes", [])
        ok, detail = _contains_exactly_once(listed, scene_name, "scenes")
        if not ok:
            return ok, detail
        row = rows_named(listed, scene_name)[0]
        eid = row.get("entity_id")
        if not eid:
            return False, f"list 行的 entity_id 为 null: {row}"
        found, ids = wait_for_state(url, headers, eid)
        if not found:
            return False, (f"{eid} 不在 /api/states（轮询 ~8s 后仍缺，幽灵 id）"
                           f"；states 里的 scene.*: {sorted(x for x in ids if x.startswith('scene.'))[:8]}")
        return True, ""
    r.check("scene_list_contains", _scene_list_contains)
    r.check("scene_activate", lambda: _expect(ha.ha_scene("activate", name=scene_name), success=True))

    # 附加断言：同名场景在 ha_status.scenes 与 ha_scene list 里各只 1 条（AC-1）
    def _no_duplicate_scene():
        status = ha.ha_status()
        if status.get("connected") is not True:
            return False, f"ha_status 未连接: {status}"
        status_rows = rows_named(status.get("scenes", []), scene_name)
        plain_rows = rows_named(ha.ha_scene("list").get("scenes", []), scene_name)
        detail_rows = rows_named(ha.ha_scene("list", detail=True).get("scenes", []), scene_name)
        counts = (len(status_rows), len(plain_rows), len(detail_rows))
        if counts != (1, 1, 1):
            return False, f"ha_status/ha_scene list/detail 各出现 {counts} 次（应各 1）"
        if not status_rows[0].get("entity_id"):
            return False, f"ha_status 场景行 entity_id 为 null: {status_rows[0]}"
        return True, ""
    r.check("scene_no_duplicate", _no_duplicate_scene)

    # --- 13..15 script create / run / list ---
    r.check("script_create",
            lambda: _expect(ha.ha_script("create", name=script_name, config=script_config), success=True))
    r.check("script_run", lambda: _expect(ha.ha_script("run", name=script_name), success=True))
    r.check("script_list_contains", lambda: _contains_exactly_once(
        ha.ha_script("list").get("scripts", []), script_name, "scripts"))

    # 附加断言：ha_status.devices 里该脚本只 1 条（R3 的幽灵 idle 不复现）
    def _no_duplicate_script():
        status = ha.ha_status()
        if status.get("connected") is not True:
            return False, f"ha_status 未连接: {status}"
        rows = rows_named(status.get("devices", []), script_name)
        if len(rows) != 1:
            scripts = [d.get("name") for d in status.get("devices", [])
                       if str(d.get("entity_id") or "").startswith("script.")]
            return False, f"ha_status.devices 中 {script_name} 出现 {len(rows)} 次（应 1）；script.* 行: {scripts}"
        return True, ""
    r.check("script_no_duplicate", _no_duplicate_script)

    # --- 16..18 subscribe / list / unsubscribe ---
    def _subscribe_add():
        out = ha.ha_subscribe(entity_id=target, condition="state_change", description="ha-mcp-smoke")
        r.ctx["subscribe"] = out
        if out.get("success") is not True:
            return False, f"返回 {out}"
        if not out.get("trigger_id"):
            return False, f"未返回 trigger_id: {out}"
        return True, ""
    r.check("subscribe_add", _subscribe_add)

    def _known_trigger_id() -> tuple:
        tid = r.ctx.get("subscribe", {}).get("trigger_id", "")
        if not tid:
            return "", "subscribe_add 未拿到 trigger_id（见该断言的 FAIL）"
        return tid, ""

    def _subscribe_list():
        tid, why = _known_trigger_id()
        if not tid:
            return False, why
        ids = [t.get("id") for t in ha.ha_subscribe(operation="list").get("triggers", [])]
        return (True, "") if tid in ids else (False, f"{tid} 不在 {ids}")
    r.check("subscribe_list", _subscribe_list)

    def _unsubscribe():
        tid, why = _known_trigger_id()
        if not tid:
            return False, why
        return _expect(ha.ha_subscribe(operation="unsubscribe", trigger_id=tid), success=True)
    r.check("unsubscribe", _unsubscribe)

    # --- 19 配置流（handler 固定 mqtt，spec §9.2）---
    def _flow_form():
        out = ha.ha_integrate(handler="mqtt")
        if out.get("type") != "form":
            return False, f"type != form: {out}"
        if not out.get("fields"):
            return False, f"fields 为空: {out}"
        return True, ""
    r.check("integrate_flow_form", _flow_form)

    # 附加断言：ha_status(domain=light) 不得出现 scene/script 补录（映射还在时才有意义）
    def _status_filter_no_supplement():
        out = ha.ha_status(domain="light")
        if out.get("connected") is not True:
            return False, f"ha_status(domain=light) 返回 {out}"
        leaked = [s for s in out.get("scenes", []) if str(s.get("entity_id") or "").startswith("scene.")]
        leaked += [d for d in out.get("devices", []) if str(d.get("entity_id") or "").startswith("script.")]
        if leaked:
            return False, f"补录泄漏: {leaked}"
        return True, ""
    r.check("status_filter_no_supplement", _status_filter_no_supplement)

    # --- 20..22 删除 ---
    r.check("automation_delete",
            lambda: _expect(ha.ha_automation("delete", name=auto_name, confirm=True), success=True))

    def _scene_delete():
        out = ha.ha_scene("delete", name=scene_name, confirm=True)
        if out.get("success") is not True:
            return False, f"返回 {out}"
        written = json.loads(Path(ha.CONFIG_PATH).read_text(encoding="utf-8"))
        if scene_name in written.get("scene_name_map", {}):
            return False, f"{ha.CONFIG_PATH} 的 scene_name_map 仍含 {scene_name}: {written.get('scene_name_map')}"
        again = rows_named(ha.ha_scene("list").get("scenes", []), scene_name)
        if again:
            return False, f"再 ha_scene list 仍出现 {scene_name}: {again}"
        return True, ""
    r.check("scene_delete", _scene_delete)
    r.check("script_delete",
            lambda: _expect(ha.ha_script("delete", name=script_name, confirm=True), success=True))

    # --- 附加断言：create 返回的 entity_id 必须在 /api/states 里真实存在 ---
    def _scene_create_entity_exists():
        out = ha.ha_scene("create", name=scene2_name, config=scene_config)
        r.ctx["scene2"] = out
        if out.get("success") is not True:
            return False, f"第二场景 create 失败: {out}"
        eid = out.get("entity_id")
        if not eid:
            return False, f"create 返回 entity_id 为 null（warning={out.get('warning')}）"
        if eid == f"scene.{out.get('config_key')}":
            return False, f"create 返回了拼装 id {eid}"
        found, ids = wait_for_state(url, headers, eid)
        if not found:
            return False, (f"create 的 {eid} 不在 /api/states（轮询 ~8s 后仍缺）"
                           f"；states 里的 scene.*: {sorted(x for x in ids if x.startswith('scene.'))[:8]}")
        return True, ""
    r.check("scene_create_entity_exists", _scene_create_entity_exists)

    # --- 收尾：清掉本轮残留（不计断言），便于同容器复跑中文名用例 ---
    for name in (scene2_name, scene_name, script_name, auto_name):
        try:
            if name == auto_name:
                ha.ha_automation("delete", name=name, confirm=True)
            elif name == script_name:
                ha.ha_script("delete", name=name, confirm=True)
            else:
                ha.ha_scene("delete", name=name, confirm=True)
        except Exception as exc:
            print(f"# cleanup 失败（{name}）: {exc}", flush=True)

    print(r.totals_line(), flush=True)
    return 1 if r.failed else 0


def _expect(out: dict, **flags) -> tuple:
    """断言 out[key] is want（connected / success）。"""
    for key, want in flags.items():
        if out.get(key) is not want:
            return False, f"{key} != {want}: {out}"
    return True, ""


def _contains_exactly_once(rows, name: str, label: str) -> tuple:
    hit = rows_named(rows, name)
    if len(hit) != 1:
        return False, f"{label} 中 {name} 出现 {len(hit)} 次（应为 1）：{[x.get('name') for x in rows]}"
    return True, ""


def _get_config_matches(out: dict, name: str) -> tuple:
    cfg = out.get("config")
    if not isinstance(cfg, dict):
        return False, f"config 不是 dict: {out}"
    if cfg.get("alias", "") != name:
        return False, f"alias={cfg.get('alias')!r} 与 {name!r} 不匹配"
    return True, ""


if __name__ == "__main__":
    sys.exit(main())
