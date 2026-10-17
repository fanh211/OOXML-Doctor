# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规范（单文件可执行）。

关键点：**内置的 XSD 必须一起打进去**。
src/ooxml_doctor/schemas/*.xsd 是运行时 schema.py 通过 __file__ 相对路径读的，
不打进去的话工具一跑就报“内置 XSD 不可用”，XSD 校验整层失效。
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
SRC = os.path.join(ROOT, "src")

if os.environ.get("OOXML_DOCTOR_NO_SCHEMAS") == "1":
    # 不带内置 XSD 构建：M1.20 的 XSD 校验层会关闭，其余功能不受影响。
    # 用途见 THIRD-PARTY-NOTICES.md §1（ISO/IEC schema 的再分发需自行授权）。
    print("!! 本次构建 **不含** 内置 XSD：M1.20 的 XSD 校验层将关闭")
    datas = []
else:
    datas = [
        (os.path.join(SRC, "ooxml_doctor", "schemas"), "ooxml_doctor/schemas"),
        # 单文件 exe 分发出去只剩一个文件，许可证声明必须能随 exe 自述，
        # 否则 `--licenses` 读不到、就是不合规。
        (os.path.join(SRC, "ooxml_doctor", "NOTICE.txt"), "ooxml_doctor"),
    ]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=["lxml.etree", "lxml._elementpath", "lxml.isoschematron"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "email", "http",
              "xmlrpc", "pdb", "distutils"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ooxml-doctor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
