"""T1：llama-server 条件加载 mmproj + 媒体编码入口（方案 Task 1 Step 5 的四个子情形）。

全 mock、禁真实进程 / 禁真实 HTTP（绝不碰用户正在用的 19878）：
  (a) 开关关 → argv 含 `--no-mmproj`（元素级成员判断，`--no-mmproj` 自身带
      `--mmproj` 子串，子串写法会自伤）、不含 `--mmproj`（I-1：必须主动禁，
      服务端不给参数时可能自附共置 mmproj）。
  (b) 开关开+产物在 → argv 含 `--mmproj`（值=家目录 mmproj_path）+ `--image-max-tokens`
      （N 严格 < -ub；且 N × 0.09 < 媒体超时，0.09 s/token 是文档化
      最坏吞吐常数，测试只引用、不现场测）。
  (c) 开关开+产物缺 → start() 在 spawn 之前抛错、报文含产物路径。🔴 必须
      monkeypatch `_health_ok` → False（否则走幂等复用直接返回，前置检查执行不到，
      用例因错因恒红）；且必须 patch 产物面（mmproj_path → 不存在的临时路径 +
      describe_missing_media → 空串），把"真实产物是否就位"从测试前提里彻底拿掉
      （P2②：靠真 describe_missing_media 在本机产物已就位时返回空串兜底，在产物
      缺失的机器上会假红，且锅指向产品代码）。
  (d) 产物在 + 运行实例无 mmproj + 开关热翻转为开 → encode_media() 必须在前置
      检查处抛错（发送点未被调用），绝不静默按文本模式继续。precondition 用旗标
      `_mmproj_loaded = False` 直接构造（不走 start()）；发送点 patch 掉，绝不把
      media 编码 POST 到真实 19878（那是用户活体的纯文本实例，会 400 或污染）。

v2（llama_config 一条完整命令行）后的注入点纪律：argv 不再由本模块拼装，而取自
`llama_config.argv()`（生效行分词）——因此：
  - `media_ingest.enabled` 开关分支必须 patch **源模块**
    `niu_api.internal.media_ingest_config.media_ingest_enabled`（llama_config 的
    default_command() 是函数级 from-import，调用时从源模块重新解析属性；patch
    llama_server 命名空间对 default_command 无效）；start() 自身的 I-2/复用分支
    检查仍走 llama_server 命名空间，故 start() 级用例两处都 patch；
  - 生效行由注入 `niu_api.config.CONFIG_PATH`（空配置 ⇒ `command: null` ⇒ 内置
    缺省行）或显式 `llama_cpp.command` 行决定，用例从此不依赖本机真实配置内容；
  - 超时常量改由 `llama_config` 导出函数现读（startup_timeout / media_http_timeout）。

测试运行中出现的 `[llama-server]` 行与 loguru teardown 时的输出是 loguru 的
stderr 输出（所有用例全 mock、绝未拉起真实服务），属已知无关噪声，非测试失败信号。
"""
import json
import pathlib
import types

import pytest

from niu_api.internal import llama_config
from niu_api.internal import llama_server


class _FakeProc:
    """鸭子类型子进程：poll() 返回 None（活着），stderr=None（drain 线程直接结束）。"""

    stderr = None
    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed += 1


class _DeadProc(_FakeProc):
    """自行崩溃的实例（未走 stop）—— B7 反向静默降级的触发形态。"""

    def poll(self):
        return 1


def _patch_popen(monkeypatch, on_popen):
    """把 llama_server 命名空间里的 subprocess 整个换成桩（Popen 进 on_popen，
    其余属性指向真模块，_stop_locked 的 TimeoutExpired 分支不受影响）。"""
    import subprocess as real_subprocess

    fake_sp = types.SimpleNamespace(
        Popen=on_popen,
        DEVNULL=real_subprocess.DEVNULL,
        PIPE=real_subprocess.PIPE,
        TimeoutExpired=real_subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(llama_server, "subprocess", fake_sp)


def _set_command_line(monkeypatch, tmp_path, command):
    """注入一个隔离的 user-config.json 并钉住生效行：`command` 为 None ⇒ 内置缺省行，
    字符串 ⇒ 原样执行该行。用例从此不依赖本机真实配置内容（v2 注入点）。"""
    cfg = tmp_path / "cfg" / "user-config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"llama_cpp": {"command": command}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(cfg))


def _patch_media_switch(monkeypatch, value):
    """同时 patch 源模块（llama_config.default_command 的函数级 import 生效面）与
    llama_server 命名空间（start() 自身 I-2/复用分支检查的生效面）。"""
    monkeypatch.setattr("niu_api.internal.media_ingest_config.media_ingest_enabled", lambda: value)
    monkeypatch.setattr(llama_server, "media_ingest_enabled", lambda: value)


# ============== 子情形 (a)/(b)：_build_argv 的返回值（v2：＝llama_config.argv()）=============


class TestBuildArgv:
    def test_disabled_appends_no_mmproj(self, monkeypatch, tmp_path):
        """I-1：开关关 → 主动追加 --no-mmproj，且无 --mmproj。"""
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, False)
        argv = llama_server._build_argv()
        assert "--no-mmproj" in argv
        assert "--mmproj" not in argv

    def test_disabled_with_real_empty_config(self, monkeypatch, tmp_path):
        """走真实配置读取面（CONFIG_PATH 注入空配置）——验证接线，不只信被 patch 的假开关。"""
        _set_command_line(monkeypatch, tmp_path, None)   # command:null ⇒ 缺省行按开关分支走
        argv = llama_server._build_argv()
        assert "--no-mmproj" in argv
        assert "--mmproj" not in argv

    def test_enabled_appends_mmproj_and_image_max_tokens(self, monkeypatch, tmp_path):
        """I-11 + 审查 A8/B7/A9：--mmproj 的值必须是家目录产物；--image-max-tokens
        的 N 严格 < -ub（留出模板预算），且 N × 0.09 < 媒体超时（时间维不反噬）。"""
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, True)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
        (fake_home / ".niu" / "models").mkdir(parents=True)
        mmproj = fake_home / ".niu" / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
        mmproj.write_bytes(b"x")

        argv = llama_server._build_argv()
        assert "--no-mmproj" not in argv
        i = argv.index("--mmproj")
        assert argv[i + 1] == str(mmproj), "--mmproj 的值必须是家目录产物（I-11）"
        j = argv.index("--image-max-tokens")
        n = int(argv[j + 1])
        ub = int(argv[argv.index("-ub") + 1])
        assert 0 < n < ub, "N 必须严格小于 -ub（单输入总长预算）"
        assert n * 0.09 < llama_config.media_http_timeout(), (
            "最坏编码时长（N × 0.09 s/token，C4 实测上界）必须小于媒体超时，"
            "否则大图必然客户端超时降级（审查 B8）"
        )


class TestStartPreflight:
    def test_enabled_missing_artifact_raises_before_spawn(self, monkeypatch, tmp_path):
        """I-2：开关开而产物缺 ⇒ 前置检查先于 spawn 抛错，报文含产物路径。

        P2②：patch describe_missing_media → 空串，强制走模块级兜底报文——
        "真实产物是否就位"从此与用例红绿无关（在任何机器上都恒绿）。
        v2：生效行写成首词=临时目录里真实存在的假可执行体（布局外 ⇒ 布局产物
        Fatal 被跳过，patch describe_missing 为"被调用即炸"以钉死这一点）。"""
        missing = tmp_path / "home" / ".niu" / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
        exe = tmp_path / "build" / "llama-server"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ-fake")
        monkeypatch.setattr(llama_server, "_health_ok", lambda: False)  # 不走复用短路
        _set_command_line(monkeypatch, tmp_path, f"{exe} --embedding --port 19878 --no-mmproj")
        _patch_media_switch(monkeypatch, True)
        monkeypatch.setattr(llama_server, "mmproj_path", lambda: missing)
        monkeypatch.setattr(llama_server, "media_artifacts_ready", lambda: False)

        def _boom():
            raise AssertionError("布局外首词不得触碰布局产物检查（F5）")
        monkeypatch.setattr(llama_server, "describe_missing", _boom)
        monkeypatch.setattr(llama_server, "describe_missing_media", lambda: "")  # P2②：切断本机产物状态依赖
        popped = []

        def _fake_popen(argv, **kw):
            popped.append(argv)
            raise AssertionError("Popen 不得被调用（前置检查必须先抛错）")

        _patch_popen(monkeypatch, _fake_popen)
        with pytest.raises(RuntimeError) as ei:
            llama_server.start()
        assert str(missing) in str(ei.value), "报文必须指名缺失的产物路径"
        assert "重启" in str(ei.value), "fail-loud 报文必须指向重启处置（spec §5.4）"
        assert not popped, "spawn 之前必须已抛错"

    def test_reuse_branch_switch_on_flag_false_raises(self, monkeypatch, tmp_path):
        """spec §5.4：开关开而活体实例未加载 mmproj（旗标假）⇒ 复用分支也 fail-loud，
        绝不静默按文本模式继续。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", False)
        with pytest.raises(RuntimeError):
            llama_server.start()

    def test_reuse_branch_switch_on_flag_true_returns(self, monkeypatch, tmp_path):
        """旗标真（本模块以 --mmproj 拉起的实例）⇒ 复用不报错，旗标保持不动。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        llama_server.start()
        assert llama_server._mmproj_loaded is True

    def test_reuse_branch_switch_off_flag_false_returns(self, monkeypatch, tmp_path):
        """开关关 ⇒ 幂等复用直接返回，旗标不动（Interfaces：复用路径不置位不复位）。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, False)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", False)
        llama_server.start()
        assert llama_server._mmproj_loaded is False

    def test_reuse_dead_module_instance_switch_on_raises(self, monkeypatch, tmp_path):
        """P2①：本模块拉起的 --mmproj 实例自行崩溃（未走 stop ⇒ 旗标残留真）+
        外部纯文本实例占用本行 --port 端口（健康真）⇒ start() 复用分支必须先判本模块实例
        死活、复位旗标再报错——否则 media_ready() 假真，data: URI 会被 POST 到
        纯文本实例（反向静默降级）。必须真经 start() 的复用分支（不得直调 _stop_locked）。"""
        _set_command_line(monkeypatch, tmp_path, None)
        monkeypatch.setattr(llama_server, "_proc", _DeadProc())
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)   # 陈旧旗标
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)  # 外部实例健康
        _patch_media_switch(monkeypatch, True)
        with pytest.raises(RuntimeError):
            llama_server.start()
        assert llama_server._mmproj_loaded is False, "复用分支必须先复位陈旧旗标"
        assert llama_server.media_ready() is False, "假真即反向静默降级"

    def test_reuse_dead_module_instance_switch_off_resets_stale_flag(self, monkeypatch, tmp_path):
        """开关关 + 本实例已死（陈旧旗标真）+ 外部实例健康 ⇒ 幂等复用正常返回，
        但陈旧旗标必须被复位（media_ready() 随之为假，旗标不再说谎）。"""
        _set_command_line(monkeypatch, tmp_path, None)
        monkeypatch.setattr(llama_server, "_proc", _DeadProc())
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        _patch_media_switch(monkeypatch, False)
        llama_server.start()
        assert llama_server._mmproj_loaded is False
        assert llama_server.media_ready() is False

    def test_cold_start_switch_on_artifact_present(self, monkeypatch, tmp_path):
        """开关开+产物在+冷启动 ⇒ 新实例 argv 含 --mmproj，start() 成功后旗位置真。

        v2：布局产物面由 NIU_MODELS_PATH 指到空临时目录后**真建**布局二进制文件
        （缺省行首词＝布局二进制，其存在性检查在任何机器上恒绿）；布局派生路径
        取自真实 embedding_artifacts 模块（llama_config 的惰性 import 生效面）。"""
        import niu_api.internal.embedding_artifacts as ea

        fake_home = tmp_path / "home"
        (fake_home / ".niu" / "models").mkdir(parents=True)
        mmproj = fake_home / ".niu" / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
        mmproj.write_bytes(b"x")
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
        binary = ea.llama_server_path()
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"x")   # 缺省行首词（布局二进制）必须存在，用例与机器状态无关
        ea.gguf_path().write_bytes(b"x")   # -m 指向的 GGUF 同理（F5 ② 顺带检查）
        _set_command_line(monkeypatch, tmp_path, None)   # command:null ⇒ 内置缺省行
        _patch_media_switch(monkeypatch, True)

        states = {"ready": False}

        def _health():
            return states["ready"]

        monkeypatch.setattr(llama_server, "_health_ok", _health)
        monkeypatch.setattr(llama_server, "mmproj_path", lambda: mmproj)
        monkeypatch.setattr(llama_server, "media_artifacts_ready", lambda: True)
        monkeypatch.setattr(llama_server, "describe_missing", lambda: "")
        monkeypatch.setattr(llama_config, "startup_timeout", lambda: 0.2)
        # 测试隔离（P3-4）：atexit 打桩——spawn 路径会 atexit.register(stop)，
        # 不打桩会在全局 atexit 注册表留一条测试遗留的钩子；_stderr_thread 预先
        # 钉 None，monkeypatch 测试后自动还原，桩进程（stderr=None，drain 线程
        # 即刻自然结束）的引用不残留在模块全局。
        monkeypatch.setattr(
            llama_server, "atexit", types.SimpleNamespace(register=lambda *a, **k: None)
        )
        monkeypatch.setattr(llama_server, "_stderr_thread", None)
        spawned = []

        def _fake_popen(argv, **kw):
            spawned.append(argv)
            states["ready"] = True  # 首个 _health_ok 为假（走 spawn 路径），之后为真（就绪）
            return _FakeProc()

        _patch_popen(monkeypatch, _fake_popen)
        llama_server.start()
        assert len(spawned) == 1
        argv = spawned[0]
        assert "--mmproj" in argv and "--no-mmproj" not in argv
        assert argv[argv.index("--mmproj") + 1] == str(mmproj)
        assert "--image-max-tokens" in argv
        assert llama_server._mmproj_loaded is True, "新实例路径必须按生效行置位旗标"


# ============== 三态：None（未声明）绝不按 True 处理（§5'.2）==============


class TestMmprojTriState:
    def test_undeclared_none_never_truthy_for_media_ready(self, monkeypatch):
        """None＝行内既无 --mmproj 也无 --no-mmproj（未声明）：media_ready() 必须
        返回严格的 False（不得是 None，不得按 True 处理）。"""
        monkeypatch.setattr(llama_server, "_mmproj_loaded", None)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        assert llama_server.media_ready() is False

    def test_undeclared_none_encode_media_raises_with_distinguishable_reason(self, monkeypatch, tmp_path):
        """未声明（None）与明确关闭（False）的状态文案必须可辨（§5'.2 原话）。"""
        p = tmp_path / "photo.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", None)
        sent = []
        monkeypatch.setattr(
            llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768
        )
        with pytest.raises(RuntimeError) as ei:
            llama_server.encode_media(str(p))
        assert not sent, "前置检查必须先于任何发送"
        assert "未声明" in str(ei.value), "None 的文案必须可辨为『未声明』而非『明确关闭』"

    def test_reuse_branch_undeclared_raises_with_distinguishable_reason(self, monkeypatch, tmp_path):
        """复用分支：开关开 + 实例未声明多模态（旗标 None）⇒ fail-loud 且文案可辨。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        _set_command_line(monkeypatch, tmp_path, None)
        _patch_media_switch(monkeypatch, True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", None)
        with pytest.raises(RuntimeError) as ei:
            llama_server.start()
        assert "未声明" in str(ei.value)


# ============== F5：预检跟随 command 首词——布局外不得因布局产物缺失而拒启 ========


class TestPreflightLayout:
    def test_outside_layout_missing_layout_artifacts_do_not_veto(self, monkeypatch, tmp_path):
        """F5 回归①：command 首词＝临时目录里一个**存在**的假可执行体（用户自备
        GPU/AMD 构建放在别处），布局产物**故意缺失** ⇒ 预检不得抛（跳过布局产物
        Fatal），且 describe_missing 根本不得被调用。"""
        exe = tmp_path / "my-gpu-build" / "llama-server"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ-fake")
        # 布局故意缺失：模型目录指到空临时目录（二进制/GGUF 都不在）
        models_dir = tmp_path / "empty-models"
        models_dir.mkdir()
        monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
        _set_command_line(
            monkeypatch, tmp_path,
            f"{exe} --embedding --port 19878 --host 127.0.0.1 --no-mmproj",
        )
        _patch_media_switch(monkeypatch, False)   # I-2 分支与本用例无关，关掉

        def _boom():
            raise AssertionError("布局外首词不得触碰布局产物检查（F5）")
        monkeypatch.setattr(llama_server, "describe_missing", _boom)

        argv = llama_server._build_argv()
        assert llama_server._inside_layout(argv[0]) is False
        llama_server._preflight_check(argv, False)   # 不得抛

    def test_inside_layout_missing_layout_artifacts_still_fatal_first_line_unchanged(self, monkeypatch, tmp_path):
        """F5 回归②：首词在布局内（内置缺省行 ⇒ 首词＝布局二进制）且布局产物缺失 ⇒
        仍抛，且文案**首行字节级不变**（与 launcher 的 MISSING_ARTIFACTS_HANDLE 同步）。"""
        import niu_api.internal.embedding_artifacts as ea

        models_dir = tmp_path / "models"   # 空布局目录 ⇒ 二进制与 GGUF 全缺
        models_dir.mkdir()
        monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
        _set_command_line(monkeypatch, tmp_path, None)   # command:null ⇒ 缺省行 ⇒ 首词＝布局二进制
        _patch_media_switch(monkeypatch, False)

        argv = llama_server._build_argv()
        assert llama_server._inside_layout(argv[0]) is True
        with pytest.raises(RuntimeError) as ei:
            llama_server._preflight_check(argv, True)
        msg = str(ei.value)
        assert msg.splitlines()[0] == "向量模型产物缺失，程序无法启动。缺少："
        assert str(ea.llama_server_path()) in msg
        assert str(ea.gguf_path()) in msg

    def test_outside_layout_missing_executable_reports_its_path(self, monkeypatch, tmp_path):
        """布局外 + 首词不存在 ⇒ 如实报**该路径**（不是布局产物文案）。"""
        models_dir = tmp_path / "empty-models"
        models_dir.mkdir()
        monkeypatch.setenv("NIU_MODELS_PATH", str(models_dir))
        ghost = tmp_path / "nowhere" / "llama-server"
        _set_command_line(monkeypatch, tmp_path, f"{ghost} --embedding --port 19878 --no-mmproj")
        _patch_media_switch(monkeypatch, False)

        def _boom():
            raise AssertionError("布局外首词不得触碰布局产物检查（F5）")
        monkeypatch.setattr(llama_server, "describe_missing", _boom)

        with pytest.raises(RuntimeError) as ei:
            llama_server._preflight_check(llama_server._build_argv(), False)
        assert str(ghost) in str(ei.value)


# ============== media_ready：旗标 ∧ 健康（不是"开关开 ∧ 健康"）==============


class TestMediaReady:
    def test_flag_true_and_healthy(self, monkeypatch):
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        assert llama_server.media_ready() is True

    def test_flag_true_but_unhealthy(self, monkeypatch):
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: False)
        assert llama_server.media_ready() is False

    def test_healthy_but_flag_false_is_not_ready(self, monkeypatch):
        """审查 P2 鉴别器：纯文本实例健康 ≠ 能编图 —— "开关开 ∧ 健康"的写法在此翻车。"""
        monkeypatch.setattr(llama_server, "_mmproj_loaded", False)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        assert llama_server.media_ready() is False


# ============== 子情形 (d)：实例无 mmproj ⇒ encode_media 前置抛错、不发送 ========


class TestEncodeMedia:
    def _jpg(self, tmp_path):
        p = tmp_path / "photo.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        return p

    def test_d_switch_on_instance_without_mmproj_raises_before_send(self, monkeypatch, tmp_path):
        """I-2：旗标假（实例是纯文本形态）⇒ 抛错且发送点未被调用，绝不按文本模式硬编码。"""
        jpg = self._jpg(tmp_path)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", False)
        sent = []
        monkeypatch.setattr(
            llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768
        )
        with pytest.raises(RuntimeError) as ei:
            llama_server.encode_media(str(jpg))
        assert not sent, "前置检查必须先于任何发送"
        assert "mmproj" in str(ei.value)

    def test_unhealthy_raises_before_send(self, monkeypatch, tmp_path):
        jpg = self._jpg(tmp_path)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: False)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []
        monkeypatch.setattr(
            llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768
        )
        with pytest.raises(RuntimeError):
            llama_server.encode_media(str(jpg))
        assert not sent

    def test_missing_file_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []
        monkeypatch.setattr(
            llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768
        )
        with pytest.raises(RuntimeError):
            llama_server.encode_media(str(tmp_path / "nope.jpg"))
        assert not sent

    def test_success_sends_jpeg_data_uri(self, monkeypatch, tmp_path):
        """审查 A7：MIME 前缀按落盘文件真实扩展名取（.jpg → image/jpeg）。"""
        jpg = self._jpg(tmp_path)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []

        def _fake_post(uri):
            sent.append(uri)
            return [0.5] * 768

        monkeypatch.setattr(llama_server, "_post_media", _fake_post)
        out = llama_server.encode_media(str(jpg))
        assert len(sent) == 1
        assert sent[0].startswith("data:image/jpeg;base64,")
        assert out == [0.5] * 768

    def test_success_uses_png_mime_for_png(self, monkeypatch, tmp_path):
        png = tmp_path / "pic.png"
        png.write_bytes(b"\x89PNGfake")
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []
        monkeypatch.setattr(
            llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768
        )
        llama_server.encode_media(str(png))
        assert sent[0].startswith("data:image/png;base64,")

    def test_post_media_sends_three_level_nesting(self, monkeypatch, tmp_path):
        """结构断言：/v1/embeddings 的载荷必须是三层嵌套
        input[0].content[0].image_url.url = data: URI（前缀保留）。

        为什么加这条（2026-10-13 真机验收才拓出）：旧测试只断言"我们自己发出的
        形状"（URI 前缀/MIME），永远捉不住"服务端不接受我们发出的形状"——旧载荷
        `{"input": [data_uri]}` 的 input 字符串槽是文本专用，裸 data: URI 被整串
        当文本分词（数十 MB base64 ⇒ 超 -ub 8192 ⇒ HTTP 500，真机入库 saved:false
        即此）。服务端只认 input[].content[].image_url.url 三层嵌套（与
        gemma2-sandbox mm_test.py 的 img_emb() 同型）；少一层 content 会被 400 拒。
        本用例把发送层 patch 到 urlopen（不再 mock 掉 _post_media），直接捕获
        实际发出的 JSON body 并钉死结构：若日后有人把嵌套"简化"回裸字符串，本用
        例立即翻红——URI 前缀断言对此毫无抵抗力（简化后 URI 仍会照发）。
        """
        jpg = self._jpg(tmp_path)
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        captured = {}

        class _FakeResp:
            def read(self):
                return json.dumps({"data": [{"embedding": [0.5] * 768}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            captured["timeout"] = timeout
            return _FakeResp()

        # patch 到 urlopen 层（而非 mock 掉 _post_media）：才能看到真正发出的 body
        monkeypatch.setattr(llama_server.urllib.request, "urlopen", _fake_urlopen)
        out = llama_server.encode_media(str(jpg))
        assert len(out) == 768 and out[0] == 0.5
        body = captured["body"]
        assert body.get("encoding_format") == "float"
        assert len(body["input"]) == 1
        content = body["input"][0]["content"][0]
        assert content["type"] == "image_url"
        assert content["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_encode_media_bytes_success_sends_jpeg_uri(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []

        def _fake_post(uri):
            sent.append(uri)
            return [0.5] * 768

        monkeypatch.setattr(llama_server, "_post_media", _fake_post)
        out = llama_server.encode_media_bytes(b"\xff\xd8\xff\xdbfake", "image/jpeg")
        assert len(sent) == 1 and sent[0].startswith("data:image/jpeg;base64,")
        assert len(out) == 768

    def test_encode_media_bytes_without_mmproj_raises_without_sending(self, monkeypatch):
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", False)
        sent = []
        monkeypatch.setattr(llama_server, "_post_media", lambda uri: sent.append(uri) or [0.5] * 768)
        with pytest.raises(RuntimeError, match="mmproj"):
            llama_server.encode_media_bytes(b"x")
        assert not sent  # 前置检查必须先于任何发送（与 encode_media 同契约）

    def test_encode_media_bytes_unhealthy_raises_before_send(self, monkeypatch):
        """`encode_media` 健康闸用例（`test_unhealthy_raises_before_send`）的 bytes 入口镜像：
        `_health_ok()==False` ⇒ 抛可辨错误且**零发送**（健康闸先于任何发送，bytes 入口与 path 入口同契约）。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: False)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        sent = []
        monkeypatch.setattr(llama_server, "_post_media", lambda uri: sent.append(uri) or [0.0] * 768)
        with pytest.raises(RuntimeError):
            llama_server.encode_media_bytes(b"\xff\xd8fake")
        assert not sent


# ============== B7：旗标复位必须在 _stop_locked 的提前返回之前 =================


class TestStopResetsFlag:
    def test_crashed_proc_flag_reset_before_early_return(self, monkeypatch):
        """mmproj 实例自行崩溃（未走 stop）后，_stop_locked 的提前返回**不得**跳过复位——
        否则旗标残留 True ⇒ 外部纯文本实例占用本行 --port 端口时 media_ready() 假真 ⇒
        反向静默降级。本用例即"复位行若放函数尾部"时翻红的红用例。"""
        monkeypatch.setattr(llama_server, "_proc", _DeadProc())
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        llama_server._stop_locked()
        assert llama_server._mmproj_loaded is False

    def test_live_proc_stopped_and_flag_reset(self, monkeypatch):
        fake = _FakeProc()
        monkeypatch.setattr(llama_server, "_proc", fake)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        llama_server._stop_locked()
        assert fake.terminated == 1
        assert llama_server._mmproj_loaded is False
