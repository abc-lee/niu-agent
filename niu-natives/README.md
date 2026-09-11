# niu-natives

Niu 桌面采集/输入 native 绑定（Rust + PyO3，maturin 构建 wheel 装入 `python/`）。

## 出处声明

本 crate 的 `src/desktop/` 源码移植自 [oh-my-pi](https://github.com/can1357/oh-my-pi)
（MIT License）的 `crates/pi-natives/src/desktop` 模块：macOS/Windows 屏幕采集、
窗口枚举、坐标映射与输入注入。版权与许可见本目录 `LICENSE`（含 Niu 修改声明）。

## 构建

本 crate 不在 PyPI，产物 `.so`/`.pyd` 被 `.gitignore` 排除（不进 git）——新 clone / 换机器必须从仓内源码构建 wheel 并装进 `python/` 自包含运行时；Windows 的 `.pyd` 只能在 Windows 上编译（不可交叉编译）。前置：Rust 工具链 + `maturin`（来自根目录 `requirements-dev.txt`）。以下命令在**仓库根目录**执行。

macOS：

```bash
python/bin/pip install -r requirements-dev.txt
rm -rf niu-natives/target/wheels
python/bin/maturin build --release --manifest-path niu-natives/Cargo.toml -i python/bin/python
python/bin/pip install --force-reinstall niu-natives/target/wheels/niu_natives-*.whl
```

Windows（cmd）：

```cmd
python\Scripts\pip.exe install -r requirements-dev.txt
if exist niu-natives\target\wheels rmdir /s /q niu-natives\target\wheels
python\Scripts\maturin.exe build --release --manifest-path niu-natives\Cargo.toml -i python\Scripts\python.exe
for %f in (niu-natives\target\wheels\niu_natives-*.whl) do python\Scripts\pip.exe install --force-reinstall "%f"
```

验证：

- macOS：`python/bin/python -c "import niu_natives; print(niu_natives.DesktopSession)"`
- Windows：`python\Scripts\python.exe -c "import niu_natives; print(niu_natives.DesktopSession)"`

打包流程（macOS `launcher/build.sh`、Windows 根目录 `pack.bat`）已内嵌同步骤，打包时无需手动构建。详见根 README「编译 niu-natives」章。
