#!/usr/bin/env python3
"""跨平台打包驱动。

    python3 build/build.py exe      # PyInstaller 单文件可执行（**必须在目标平台上跑**）
    python3 build/build.py zipapp   # 零依赖 .pyz（目标机只需有 Python 3.10+）
    python3 build/build.py all      # 两个都做

注意：PyInstaller **不支持交叉编译**。要 Windows 的 exe，就得在 Windows 上执行本脚本。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipapp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "dist")


def build_exe(include_schemas: bool = True) -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✘ 没装 PyInstaller。先执行：  python -m pip install pyinstaller")
        print("  提示：建议用 Python 3.11–3.13 构建；3.14 可能还没有官方支持。")
        return 1
    os.makedirs(OUT, exist_ok=True)
    # 注意：给了 .spec 文件之后不能再传 --specpath（PyInstaller 会直接报错退出）
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", OUT, "--workpath", os.path.join(ROOT, "build", "_work"),
           os.path.join(HERE, "ooxml-doctor.spec")]
    print("$", " ".join(cmd))
    env = dict(os.environ)
    if not include_schemas:
        env["OOXML_DOCTOR_NO_SCHEMAS"] = "1"
        print("!! 不含内置 XSD（M1.20 的 XSD 校验层关闭）")
    rc = subprocess.call(cmd, env=env)
    if rc == 0:
        name = "ooxml-doctor.exe" if os.name == "nt" else "ooxml-doctor"
        p = os.path.join(OUT, name)
        print(f"✔ {p}  ({os.path.getsize(p)/1048576:.1f} MB)" if os.path.exists(p)
              else "✘ 构建完成但没找到产物")
    return rc


def build_zipapp(include_schemas: bool = True) -> int:
    """打一个单文件 .pyz。目标机需要有 Python（3.10+）与 lxml。

    好处：零构建工具、纯标准库、跨平台；坏处：目标机得有 Python。
    """
    os.makedirs(OUT, exist_ok=True)
    stage = os.path.join(ROOT, "build", "_stage")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    ignore = ["__pycache__", "*.pyc"]
    if not include_schemas:
        ignore.append("schemas")
    shutil.copytree(os.path.join(SRC, "ooxml_doctor"), os.path.join(stage, "ooxml_doctor"),
                    ignore=shutil.ignore_patterns(*ignore))
    shutil.copy(os.path.join(HERE, "entry.py"), os.path.join(stage, "__main__.py"))
    target = os.path.join(OUT, "ooxml-doctor.pyz")
    zipapp.create_archive(stage, target, interpreter="/usr/bin/env python3")
    print(f"✔ {target}  ({os.path.getsize(target)/1048576:.1f} MB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ooxml-doctor 打包")
    ap.add_argument("what", nargs="?", default="all",
                    choices=["exe", "zipapp", "all"])
    ap.add_argument("--no-schemas", action="store_true",
                    help="不带内置 XSD 构建（见 THIRD-PARTY-NOTICES.md §1）")
    args = ap.parse_args()
    inc = not args.no_schemas
    rc = 0
    if args.what in ("exe", "all"):
        rc |= build_exe(inc)
    if args.what in ("zipapp", "all"):
        rc |= build_zipapp(inc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
