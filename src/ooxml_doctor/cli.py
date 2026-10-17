# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""命令行入口。

    ooxml-doctor scan   文件.pptx              # 扫描并给出 Rust 风格报告
    ooxml-doctor repair 文件.pptx --level low  # 最小修复，保证能放映
    ooxml-doctor repair 文件.pptx --level high # 激进修复（自动备份）
    ooxml-doctor catalog 目录                  # 导出 VS Code / lemminx 的 XSD 绑定配置
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Optional

from . import __version__, SPEC_NAME
from .diagnostics import Bag
from .opc import load as load_opc
from .repair import apply_repair, build_plan
from .report import render, render_json
from .xmlmodel import parse_all
from .zipraw import read_zip

EXIT_OK = 0
EXIT_ISSUES = 3
EXIT_USAGE = 2
EXIT_NEEDS_HUMAN = 4


def _schemas(arg):
    from .schema import SchemaSet, configured
    return SchemaSet(configured(arg))


def do_scan(path: str, *, verify_crc: bool = True, read_only: bool = True,
            schema_set=None):
    bag = Bag()
    if not os.path.exists(path):
        bag.error("IO", f"文件不存在：{path}")
        bag.stop()
        return bag, None, {}
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        bag.error("IO", f"无法读取文件：{exc}")
        bag.stop()
        return bag, None, {}
    archive = read_zip(data, bag, verify_crc=verify_crc)
    if not archive.usable and not archive.entries:
        bag.stop()
        return bag, None, {}
    pkg = load_opc(path, archive, bag)
    parts = parse_all(pkg, bag) if pkg.parts else {}
    from . import rules as _rules
    if pkg.parts:
        _rules.run_all(pkg, parts, bag, schema_set or _schemas(None))
    bag.stop()
    return bag, pkg, parts


def cmd_scan(args: argparse.Namespace) -> int:
    bag, _pkg, _parts = do_scan(args.file, verify_crc=not args.fast,
                                schema_set=_schemas(args.schemas))
    if args.format == "json":
        print(render_json(bag, path=args.file))
    else:
        print(render(bag, path=args.file, color=not args.no_color, verbose=args.verbose))
    return EXIT_ISSUES if bag.will_repair else EXIT_OK


def cmd_repair(args: argparse.Namespace) -> int:
    level = args.level
    ss = _schemas(args.schemas)
    bag, pkg, parts = do_scan(args.file, verify_crc=not args.fast, schema_set=ss)
    if pkg is None or not pkg.parts:
        print(render(bag, path=args.file, color=not args.no_color), file=sys.stderr)
        print("无法进入修复流程：包结构不可用。", file=sys.stderr)
        return EXIT_USAGE

    plan = build_plan(pkg, parts, bag, level, schemas=ss)
    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.file)),
        os.path.splitext(os.path.basename(args.file))[0] + "_fixed.pptx")

    if not plan.edits and not plan.drop_entries:
        print(render(bag, path=args.file, color=not args.no_color))
        print(f"\n没有 {level} 级别可自动修复的问题，未生成新文件。")
        return EXIT_OK

    if args.dry_run:
        print(f"修复计划（--level {level}，dry-run，未写入任何文件）")
        print("-" * 68)
        by_part: dict[str, list] = {}
        for e in plan.edits:
            by_part.setdefault(e.part, []).append(e)
        for part in sorted(by_part):
            print(f"  {part}")
            for e in sorted(by_part[part], key=lambda x: x.start):
                print(f"    [{e.level}] {e.code:12s} {e.reason}")
        if plan.drop_entries:
            print(f"  重复 ZIP 条目将被丢弃：{plan.drop_entries}")
        print("-" * 68)
        print(f"共 {len(plan.edits)} 处字节级修改。")
        return EXIT_OK

    backup = None
    if level == "high":
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{args.file}.backup-{ts}"
        shutil.copy2(args.file, backup)
        print("⚠  HIGH 级修复会改变行为，可能加剧损坏。")
        print(f"   已自动备份原文件 → {backup}")

    manifest = apply_repair(args.file, out, pkg, parts, bag, plan)

    print(f"✔ 修复完成（--level {level}）")
    print(f"  修改 {len(plan.edits)} 处，涉及 "
          f"{len({e.part for e in plan.edits})} 个部件")
    if manifest.get("postcondition_ok"):
        print(f"  ✔ 后置条件：{len(plan.edits)} 处编辑全部已落地到产物字节")
    else:
        print("  ✘ 后置条件未通过，以下编辑没有落地：")
        for u in manifest.get("unverified_edits", []):
            print(f"      - {u}")
    if plan.drop_entries:
        print(f"  丢弃重复 ZIP 条目 {len(plan.drop_entries)} 个")
    print(f"  输出 → {out}")
    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        print(f"  清单 → {args.manifest}")
    if backup:
        print(f"  回退：copy \"{backup}\" \"{args.file}\"")

    print()
    print("─" * 68)
    print("等价性自检（证明没有改坏原文）：")
    from .oracle import verify as _verify
    eq = _verify(args.file, out, render=False,
                 allow_external_rel_drop=(level == "high"))
    for name, r in eq["checks"].items():
        mark = {True: "✔", False: "✘", None: "－"}.get(r.get("ok"))
        extra = ""
        if name == "rel_graph" and r.get("note"):
            extra = f"  （{r['note']}）"
        if name == "parts":
            extra = f"  {r['count_before']} 个部件，无增无减"
        if name == "media":
            extra = f"  {r['files_before']} 个媒体文件内容一致"
        if name == "text":
            extra = f"  {r['parts_compared']} 个部件文本一致"
        print(f"  {mark} {name}{extra}")
    if not eq["ok"]:
        print("  ⚠ 等价性未通过，请检查 diff；原文件未被修改，可回退。")

    print()
    print("─" * 68)
    print("复检输出文件：")
    after_bag, _p, _q = do_scan(out, verify_crc=not args.fast, schema_set=ss)
    print(f"  修复前：error {bag.n_error}  warning {bag.n_warning}"
          f"   （会触发“需要修复”：{'是' if bag.will_repair else '否'}）")
    print(f"  修复后：error {after_bag.n_error}  warning {after_bag.n_warning}"
          f"   （会触发“需要修复”：{'是' if after_bag.will_repair else '否'}）")
    # ---- 诚实结论：修不掉就明确说"需要人工介入"，不硬撑 ----
    blockers = [d for d in after_bag.items
                if d.repair_trigger or (d.severity.name == "ERROR" and not d.fix_level)]
    seen = set()
    uniq = []
    for d in blockers:
        key = (d.code, d.part, d.message)
        if key not in seen:
            seen.add(key)
            uniq.append(d)

    if uniq or plan.skipped:
        print()
        print("━" * 68)
        print("⚠  需要人工介入")
        print("━" * 68)
        if plan.skipped:
            print(f"  修复阶段主动跳过的 {len(plan.skipped)} 项（改了就影响文件内容）：")
            for x in plan.skipped[:8]:
                print(f"    · {x}")
        if uniq:
            print(f"  修复后仍存在的 {len(uniq)} 项缺陷（自动修复无法处理）：")
            for d in uniq[:8]:
                print(f"    · [{d.code}] {d.message}")
                print(f"        位置: {d.part or '(包)'}")
                if d.helps:
                    print(f"        怎么办: {d.helps[0]}")
            if len(uniq) > 8:
                print(f"    …另有 {len(uniq) - 8} 项")
        print()
        print("  自动修复到此为止。上面的项需要人工判断，工具不会替你猜。")
        if level == "low":
            print("  （可先试 --level high，它会多做一批不影响文件的激进处置）")
        return EXIT_NEEDS_HUMAN

    print()
    print("✔ 修复后复检：已无会让 Office 要求修复的项，也没有需要人工介入的残留。")
    return EXIT_OK


CATALOG_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!-- 由 ooxml-doctor 生成：把 OOXML 部件绑定到对应的 XSD。
     配合 VS Code 的 redhat.vscode-xml (lemminx) 使用：
       "xml.catalogs": ["./catalog.xml"] -->
<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">
{entries}
</catalog>
"""


def cmd_catalog(args: argparse.Namespace) -> int:
    target = args.directory
    os.makedirs(target, exist_ok=True)
    maps = [
        ("http://schemas.openxmlformats.org/presentationml/2006/main",
         "schemas/pml.xsd"),
        ("http://schemas.openxmlformats.org/drawingml/2006/main",
         "schemas/dml-main.xsd"),
    ]
    entries = "\n".join(
        f'  <uri name="{uri}" uri="{xsd}"/>' for uri, xsd in maps)
    with open(os.path.join(target, "catalog.xml"), "w", encoding="utf-8") as fh:
        fh.write(CATALOG_TEMPLATE.format(entries=entries))
    with open(os.path.join(target, ".vscode-settings.snippet.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"xml.catalogs": ["./catalog.xml"]}, fh, indent=2)
    print(f"已写出 {target}/catalog.xml 与 .vscode-settings.snippet.json")
    print("注意：还需要把 ECMA-376 Part 4 Annex A 的 XSD 放到 schemas/ 下。")
    print("      XSD 已随包内置在 ooxml_doctor/schemas/，无需联网。")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    from .oracle import verify as _verify
    res = _verify(args.before, args.after, render=args.render,
                  allow_external_rel_drop=args.allow_external_rel_drop)
    for name, r in res["checks"].items():
        mark = {True: "✔", False: "✘", None: "－"}.get(r.get("ok"))
        print(f"  {mark} {name:10s} {r}")
    print()
    print("等价性：" + ("通过 ✅" if res["ok"] else "不通过 ❌"))
    return EXIT_OK if res["ok"] else EXIT_ISSUES


SELFTEST_SLIDE_BAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cSld><p:custDataLst/><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
    '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld></p:sld>'
)
SELFTEST_SLIDE_OK = SELFTEST_SLIDE_BAD.replace(
    "<p:cSld><p:custDataLst/><p:spTree>", "<p:cSld><p:spTree>").replace(
    "</p:spTree></p:cSld>", "</p:spTree><p:custDataLst/></p:cSld>")


def cmd_selftest(args: argparse.Namespace) -> int:
    """打包后自检：确认内置 XSD 真的被打进去了，且 XSD 校验确实在工作。

    典型翻车方式：PyInstaller 忘了把 schemas/ 放进 datas，
    结果 exe 里 XSD 层整层失效，而工具“看起来还能跑”。
    """
    from .schema import SchemaSet, schema_dir, validate_part

    print(f"ooxml-doctor {__version__} 自检")
    print(f"  规范基线: {SPEC_NAME}")
    ok = True

    ss = SchemaSet()
    print(f"\n[1/3] 内置 XSD 目录: {schema_dir()}")
    files = ss.files
    print(f"      可用={ss.available}  文件数={len(files)}")
    if not files:
        print("      ✘ 内置 XSD 缺失或读不到 —— 打包时漏了 datas，或压缩包形态未做资源解包！")
        return EXIT_USAGE
    print(f"      {', '.join(files[:6])}{' …' if len(files) > 6 else ''}")

    print("\n[2/3] 编译 XSD")
    for name in ("pml.xsd", "dml-main.xsd", "shared-documentPropertiesExtended.xsd"):
        sch = ss.get(name)
        print(f"      {'✔' if sch is not None else '✘'} {name}")
        ok &= sch is not None

    print("\n[3/3] XSD 校验是否真的在判对错")
    bad = validate_part("ppt/slides/slide1.xml", SELFTEST_SLIDE_BAD.encode(), ss)
    good = validate_part("ppt/slides/slide1.xml", SELFTEST_SLIDE_OK.encode(), ss)
    print(f"      故意写错的部件: 报出 {len(bad)} 条"
          f"（{'✔ 检出' if bad else '✘ 漏检'}）")
    print(f"      顺序正确的部件: 报出 {len(good)} 条"
          f"（{'✔ 无误报' if not good else '✘ 误报'}）")
    ok &= bool(bad) and not good

    print("\n" + ("✔ 自检通过，可以分发。" if ok else "✘ 自检未通过，别拿去用。"))
    return EXIT_OK if ok else EXIT_USAGE


def cmd_licenses(args: argparse.Namespace) -> int:
    """打印许可与第三方声明（LICENSE + THIRD-PARTY-NOTICES 合并的 NOTICE.txt）。

    内嵌进包的原因：单文件 exe 分发出去只剩一个文件，
    许可证声明必须能随 exe 一起自述，否则就是不合规。
    """
    from importlib import resources

    try:
        text = resources.files("ooxml_doctor").joinpath("NOTICE.txt").read_text(
            encoding="utf-8")
    except Exception:
        print("NOTICE.txt 未随包分发，请参见仓库根目录的 "
              "LICENSE 与 THIRD-PARTY-NOTICES.md。")
        return EXIT_USAGE
    print(text)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ooxml-doctor",
        description=f"PPTX 损坏诊断与修复（规范基线：{SPEC_NAME}）",
    )
    ap.add_argument("--version", action="version", version=f"ooxml-doctor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--schemas", metavar="DIR", default=None,
                        help="用外部 XSD 目录替代内置副本（也可设 "
                             "OOXML_DOCTOR_SCHEMAS）；不给则用内置")

    s = sub.add_parser("scan", help="扫描并报告问题", parents=[common])
    s.add_argument("file")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--no-color", action="store_true")
    s.add_argument("--verbose", "-v", action="store_true",
                   help="连 PROV-* 来源指纹也展开显示")
    s.add_argument("--fast", action="store_true", help="跳过 CRC 实测（更快）")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("repair", help="修复", parents=[common])
    r.add_argument("file")
    r.add_argument("--level", choices=["low", "high"], default="low",
                   help="low=最小修复保证能放映；high=激进修复（自动备份）")
    r.add_argument("-o", "--output")
    r.add_argument("--dry-run", action="store_true", help="只打印修复计划")
    r.add_argument("--manifest", help="把修复清单写到指定 JSON")
    r.add_argument("--no-color", action="store_true")
    r.add_argument("--fast", action="store_true")
    r.set_defaults(func=cmd_repair)

    c = sub.add_parser("catalog", help="导出 XSD 绑定配置（给 VS Code/lemminx）")
    c.add_argument("directory")
    c.set_defaults(func=cmd_catalog)

    v = sub.add_parser("verify", help="验证修复前后是否等价（文本/媒体/关系图/渲染）")
    v.add_argument("before")
    v.add_argument("after")
    v.add_argument("--render", action="store_true", help="额外用 LibreOffice 渲染比对")
    v.add_argument("--allow-external-rel-drop", action="store_true",
                   help="把“按设计断开的外部链接”视为可接受差异")
    v.set_defaults(func=cmd_verify)

    lc = sub.add_parser("licenses", help="打印许可与第三方声明")
    lc.set_defaults(func=cmd_licenses)

    st = sub.add_parser("selftest", help="打包后自检：内置 XSD 是否可用")

    st.set_defaults(func=cmd_selftest)


    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
