#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步出厂技能种子清单 memory/skills/seed-manifest.json。

哈希算法：FNV-1a 64（与 Rust 启动器内联实现对齐，零新依赖、不联网）。
清单结构：{"schema": 1, "hash": "fnv1a64", "skills": {
    "<name>.md": {"version": N, "hash": "<16 位十六进制>", "history": [...]}}}

用法（在仓库根目录或任意位置运行均可）：
    python scripts/update_skills_seed_manifest.py
        普通模式：扫描 memory/skills/*.md 算 FNV-1a 64，与清单对比；
        不同 ⇒ 旧 hash 推入 history、version += 1、写入新 hash；相同 ⇒ 不动；
        清单里没有的新增 .md ⇒ 按 version 1 / 空 history 补登记；只写清单。
    python scripts/update_skills_seed_manifest.py --vectors
        只读打印：已知向量（""、"a"、"abc"、"hello world" 的 UTF-8 字节）+
        当前种子文件「路径 → 哈希」列表，供与 Rust 端对齐校验。
    python scripts/update_skills_seed_manifest.py --rebuild --history-from <rev>
        一次性重建：对每个 memory/skills/*.md，当前文件哈希为 v_new，
        git show <rev>:memory/skills/<file> 取改动前字节（直接读 git 输出字节，
        不做行尾转换）算 v_old；v_old != v_new ⇒ version=2, history=[v_old]，
        否则 version=1, history=[]。写回清单并打印每文件结果。
        <rev> 中不存在的文件 ⇒ 打印警告并按 version=1 处理，不 crash。
        重建后清单只含磁盘上实际存在的 .md 文件（旧条目随之剔除）。

输出：每个文件一行（vN→vM / 未变 / 新增 / 文件缺失 / 重建结果）。
退出码：0 成功（含零变动）；1 参数或清单错误。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "memory" / "skills"
MANIFEST = SKILLS_DIR / "seed-manifest.json"
SCHEMA = 1
HASH_ALGO = "fnv1a64"

FNV_OFFSET_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = 0xFFFFFFFFFFFFFFFF


def fnv1a64(data: bytes) -> str:
    h = FNV_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * FNV_PRIME) & MASK64
    return format(h, "016x")


def fnv1a64_hex(path: Path) -> str:
    return fnv1a64(path.read_bytes())


def git_show_bytes(rev: str, repo_rel: str) -> bytes | None:
    """git show <rev>:<repo_rel> 的输出字节；文件在 <rev> 中不存在 ⇒ None。"""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{rev}:{repo_rel}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if out.returncode != 0:
        return None
    return out.stdout


def load_manifest() -> dict:
    if not MANIFEST.is_file():
        print(f"错误：清单不存在：{MANIFEST}", file=sys.stderr)
        sys.exit(1)
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"错误：清单不可读：{exc}", file=sys.stderr)
        sys.exit(1)
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("hash") != HASH_ALGO
        or not isinstance(manifest.get("skills"), dict)
    ):
        print(
            f"错误：清单 schema 不符（期望 schema={SCHEMA}, hash={HASH_ALGO}）",
            file=sys.stderr,
        )
        sys.exit(1)
    return manifest


def write_manifest(manifest: dict) -> None:
    manifest["skills"] = dict(sorted(manifest["skills"].items()))
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_vectors() -> int:
    print("已知向量（FNV-1a 64，对 UTF-8 字节；供与 Rust 启动器对齐）：")
    for s in ("", "a", "abc", "hello world"):
        print(f"  {s!r}: {fnv1a64(s.encode('utf-8'))}")
    print("当前种子文件：")
    for md in sorted(SKILLS_DIR.glob("*.md")):
        print(f"  {md.relative_to(REPO_ROOT)}: {fnv1a64_hex(md)}")
    return 0


def run_rebuild(rev: str) -> int:
    manifest: dict = {"schema": SCHEMA, "hash": HASH_ALGO, "skills": {}}
    for md in sorted(SKILLS_DIR.glob("*.md")):
        repo_rel = md.relative_to(REPO_ROOT).as_posix()
        v_new = fnv1a64_hex(md)
        v_old = git_show_bytes(rev, repo_rel)
        if v_old is None:
            print(f"警告：<rev={rev}> 中不存在 {repo_rel}，按 version=1 处理")
            manifest["skills"][md.name] = {"version": 1, "hash": v_new, "history": []}
            continue
        if fnv1a64(v_old) == v_new:
            manifest["skills"][md.name] = {"version": 1, "hash": v_new, "history": []}
            print(f"{md.name}: v1（与 <rev={rev}> 一致）")
        else:
            manifest["skills"][md.name] = {
                "version": 2,
                "hash": v_new,
                "history": [fnv1a64(v_old)],
            }
            print(f"{md.name}: v2（与 <rev={rev}> 不同，history=1）")
    write_manifest(manifest)
    print(f"已重建清单：{MANIFEST}")
    return 0


def run_sync() -> int:
    manifest = load_manifest()
    skills = manifest["skills"]

    changed = 0
    for md in sorted(SKILLS_DIR.glob("*.md")):
        digest = fnv1a64_hex(md)
        entry = skills.get(md.name)
        if entry is None:
            skills[md.name] = {"version": 1, "hash": digest, "history": []}
            print(f"{md.name}: v1 (新增)")
            changed += 1
            continue
        if digest == entry["hash"]:
            print(f"{md.name}: v{entry['version']} 未变")
            continue
        entry.setdefault("history", []).append(entry["hash"])
        entry["version"] = int(entry.get("version", 1)) + 1
        entry["hash"] = digest
        print(f"{md.name}: v{entry['version'] - 1}→v{entry['version']}")
        changed += 1

    for name, entry in skills.items():
        if not (SKILLS_DIR / name).is_file():
            print(f"{name}: 文件缺失（清单保留不动）")

    if changed:
        write_manifest(manifest)
        print(f"已更新清单：{MANIFEST}（{changed} 个文件变动）")
    else:
        print("清单与磁盘一致，未写任何文件。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--vectors", action="store_true", help="只读打印已知向量与当前种子文件哈希"
    )
    group.add_argument("--rebuild", action="store_true", help="一次性重建清单")
    parser.add_argument(
        "--history-from",
        metavar="REV",
        default=None,
        help="与 --rebuild 联用：改动前的基线 git revision",
    )
    args = parser.parse_args()

    if args.rebuild and not args.history_from:
        print("错误：--rebuild 需要 --history-from <rev>", file=sys.stderr)
        return 1
    if args.vectors:
        return run_vectors()
    if args.rebuild:
        return run_rebuild(args.history_from)
    return run_sync()


if __name__ == "__main__":
    sys.exit(main())
