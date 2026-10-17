"""自我攻防：对 origin.pptx 做变异测试（mutation testing）。

思路：往真实样本里注入**已知的、可枚举的**缺陷，然后检验工具的五项能力：

  1. 检出率（recall）—— 注入的缺陷是否被报出来，且报的是**预期的那个诊断码**
  2. 精确率（precision）—— 除了注入项，有没有冒出不相关的新 error（delta 检查）
  3. 分级正确性 —— 结构性违规必须算作"会触发 Office 修复"，取值性违规不能算
  4. 修复能力 —— low/high 是否真能消掉注入项，且不产生连带损伤（等价性）
  5. 抗损坏能力 —— 对空文件/非 zip/截断/损坏条目/非 OOXML 包不能崩

变异都放在**基线没有缺陷的位置**（原件的缺陷固定在 slide4/7/8/9、母版、
[Content_Types].xml、slideMaster2），因此用"相对基线的差集"来衡量检出。

用法:  python3 tests/adversarial.py [--render] [--seed N]
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from ooxml_doctor.cli import do_scan                      # noqa: E402
from ooxml_doctor.repair import apply_repair, build_plan  # noqa: E402
from ooxml_doctor.oracle import render_text, verify      # noqa: E402

# 真实样本**不随仓库分发**（第三方课件，见 THIRD-PARTY-NOTICES.md §5）。
# 用 --origin 或环境变量 OOXML_DOCTOR_FIXTURE 指定；缺省指向本机的案例目录。
ORIGIN = os.environ.get("OOXML_DOCTOR_FIXTURE",
                        "/home/trixie/ooxml-doctor-case/origin.pptx")
base_path_global = ORIGIN   # 由 main() 覆盖为重打包后的基准
WORK = "/tmp/adversarial"


# --------------------------------------------------------------------------- #
# 包读写
# --------------------------------------------------------------------------- #
Pkg = dict          # entry name -> bytes


def load(path: str) -> Pkg:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i.filename)
                for i in z.infolist() if not i.filename.endswith("/")}


FIXED_DT = (2020, 1, 1, 0, 0, 0)


def save(pkg: Pkg, path: str, order: list[str] | None = None) -> str:
    """写包时**固定条目时间戳**。

    zipfile.writestr(name, data) 默认用"当前时间"，会让基线包与每个变异体的
    PROV-ZIPTIME 消息各不相同（时间戳写在消息里），从而污染差集、产生假"旁及"。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in (order or list(pkg)):
            if name in pkg:
                z.writestr(zipfile.ZipInfo(name, date_time=FIXED_DT), pkg[name])
    return path


def text_of(pkg: Pkg, name: str) -> str:
    return pkg[name].decode("utf-8-sig")


def put(pkg: Pkg, name: str, s: str) -> None:
    pkg[name] = s.encode("utf-8")


# --------------------------------------------------------------------------- #
# 变异算子
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Mutation:
    key: str
    cls: str            # structural | value | app | container
    expect: str         # 预期诊断码
    trigger: bool       # 预期是否算"会触发 Office 修复"
    fix_low: bool       # 预期 low 能修掉
    fix_high: bool      # 预期 high 能修掉
    slot: str           # 互斥槽位：同槽位的变异不能组合
    fn: object
    note: str = ""


def _re_sub1(pattern, repl, s, flags=re.S):
    out, n = re.subn(pattern, repl, s, count=1, flags=flags)
    assert n == 1, f"变异未命中: {pattern[:60]}"
    return out


# ---- 结构性（XSD sequence / 必需属性）→ M1.20，error，算触发项，low 可修 ----
def m_xsd_csld_order(pkg):
    """slide2: 交换 <p:cSld> 的两个**直接子元素** spTree 与紧随其后的 custDataLst。

    注意必须锁定"紧跟在 spTree 之后"的那一个 —— 最初版本用非贪婪正则抓
    "第一个 custDataLst"，结果抓到的是 spTree **内部**某个形状的嵌套
    custDataLst，把它搬到 cSld 下变成两个 custDataLst 并存，
    那已经不是"顺序错"而是"多余元素"，任何位置调整都修不好。
    """
    s = text_of(pkg, "ppt/slides/slide2.xml")
    m = re.search(r"<p:spTree>.*?</p:spTree>(<p:custDataLst>.*?</p:custDataLst>)", s, re.S)
    assert m, "slide2 的 cSld 里没有紧邻 spTree 的 custDataLst"
    cdl_start = m.start(1)
    cdl_end = m.end(1)
    tree_start = m.start()
    tree_end = s.index("</p:spTree>", tree_start) + len("</p:spTree>")
    tree, cdl = s[tree_start:tree_end], s[cdl_start:cdl_end]
    put(pkg, "ppt/slides/slide2.xml",
        s[:tree_start] + cdl + tree + s[cdl_end:])


def m_xsd_sp_order(pkg):
    """slide2: 在第一个 <p:sp> 内把 <p:txBody> 挪到 <p:spPr> 之前。"""
    s = text_of(pkg, "ppt/slides/slide2.xml")
    if "<p:txBody>" not in s:
        raise AssertionError("slide2 没有 txBody")
    sp_start = s.index("<p:sp>")
    open_end = sp_start + len("<p:sp>")          # 开标签之后
    sp_end = s.index("</p:sp>", sp_start)        # 闭标签之前
    tx_start = s.index("<p:txBody>", sp_start)
    tx_end = s.index("</p:txBody>", tx_start) + len("</p:txBody>")
    assert open_end <= tx_start < tx_end <= sp_end
    txbody = s[tx_start:tx_end]
    others = s[open_end:tx_start] + s[tx_end:sp_end]
    put(pkg, "ppt/slides/slide2.xml",
        s[:open_end] + txbody + others + s[sp_end:])


def m_xsd_missing_attr(pkg):
    """presentation.xml: 删掉 <p:sldSz> 的必需属性 cx。"""
    s = text_of(pkg, "ppt/presentation.xml")
    put(pkg, "ppt/presentation.xml",
        _re_sub1(r'<p:sldSz cx="\d+"', "<p:sldSz", s))


# ---- 取值性（XSD facet）→ M1.20，warning，**不算**触发项 ----
def m_xsd_bad_type(pkg):
    """presentation.xml: <p:sldSz cx="abc">（类型错误 → facet/type → value 类）。"""
    s = text_of(pkg, "ppt/presentation.xml")
    put(pkg, "ppt/presentation.xml",
        _re_sub1(r'<p:sldSz cx="\d+"', '<p:sldSz cx="abc"', s))


def m_xsd_zero_coord(pkg):
    """presentation.xml: <p:notesSz cy="-1">（坐标不允许负值）。
    注意：cy="0" 是**合法**的，第一版用 0 导致该用例根本没注入缺陷。"""
    s = text_of(pkg, "ppt/presentation.xml")
    put(pkg, "ppt/presentation.xml",
        _re_sub1(r'<p:notesSz cx="(\d+)" cy="\d+"', r'<p:notesSz cx="\1" cy="-1"', s))


# ---- 内容类型 / OPC ----
def m_opc_mime_syntax(pkg):
    """[Content_Types].xml: png 的 ContentType 改成没有 subtype 的 'image'。"""
    s = text_of(pkg, "[Content_Types].xml")
    put(pkg, "[Content_Types].xml",
        _re_sub1(r'<Default Extension="png" ContentType="[^"]*"/>',
                 '<Default Extension="png" ContentType="image"/>', s))


def m_opc_ct_missing(pkg):
    """[Content_Types].xml: 删掉 png 的 Default（部件失去内容类型）。"""
    s = text_of(pkg, "[Content_Types].xml")
    put(pkg, "[Content_Types].xml",
        _re_sub1(r'<Default Extension="png" ContentType="[^"]*"/>', "", s))


def m_opc_ct_orphan(pkg):
    """[Content_Types].xml: 加一条指向不存在部件的 Override。"""
    s = text_of(pkg, "[Content_Types].xml")
    put(pkg, "[Content_Types].xml",
        s.replace("</Types>",
                  '<Override PartName="/ppt/ghost.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.'
                  'presentationml.slide+xml"/></Types>'))


def m_opc_rid_empty(pkg):
    """slide1: 把一个 r:id 置空（悬空引用）。"""
    s = text_of(pkg, "ppt/slides/slide1.xml")
    m = re.search(r'r:id="rId\d+"', s)
    put(pkg, "ppt/slides/slide1.xml", s[:m.start()] + 'r:id=""' + s[m.end():])


def m_opc_rel_dangle(pkg):
    """slide3 的关系部件里删掉一条**被引用**的关系 → 引用悬空。"""
    rels = text_of(pkg, "ppt/slides/_rels/slide3.xml.rels")
    slide = text_of(pkg, "ppt/slides/slide3.xml")
    used = re.findall(r'r:id="(rId\d+)"', slide)
    target = used[0]
    put(pkg, "ppt/slides/_rels/slide3.xml.rels",
        _re_sub1(r'<Relationship Id="%s"[^>]*/>' % target, "", rels))


def m_opc_rel_target(pkg):
    """slide3 的内部关系目标改成一个不存在的部件。"""
    rels = text_of(pkg, "ppt/slides/_rels/slide3.xml.rels")
    put(pkg, "ppt/slides/_rels/slide3.xml.rels",
        _re_sub1(r'(<Relationship [^>]*Type="[^"]*/slideLayout"[^>]*Target=")[^"]*(")',
                 r'\1../slideLayouts/ghost.xml\2', rels))


def m_opc_rel_null(pkg):
    """slide5 加一条 Target="NULL" 的外部关系，并让 slide5 引用它。"""
    rels = text_of(pkg, "ppt/slides/_rels/slide5.xml.rels")
    rels = rels.replace("</Relationships>",
                        '<Relationship Id="rIdGhost" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        'relationships/audio" Target="NULL" TargetMode="External"/>'
                        "</Relationships>")
    put(pkg, "ppt/slides/_rels/slide5.xml.rels", rels)
    slide = text_of(pkg, "ppt/slides/slide5.xml")
    put(pkg, "ppt/slides/slide5.xml",
        _re_sub1(r"(<a:endParaRPr[^>]*)/>",
                 r'\1><a:hlinkClick r:id="rIdGhost" action="ppaction://media"/></a:endParaRPr>',
                 slide) if False else slide.replace(
            "<p:nvPr>", '<p:nvPr><a:audioFile r:link="rIdGhost"/>', 1))


def m_opc_rel_localpath(pkg):
    """slide6 加一条指向本机绝对路径的外部图片关系并被引用。"""
    rels = text_of(pkg, "ppt/slides/_rels/slide6.xml.rels")
    rels = rels.replace("</Relationships>",
                        '<Relationship Id="rIdLocal" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        r'relationships/image" Target="D:\qq\tmp\a.png" '
                        'TargetMode="External"/></Relationships>')
    put(pkg, "ppt/slides/_rels/slide6.xml.rels", rels)
    slide = text_of(pkg, "ppt/slides/slide6.xml")
    put(pkg, "ppt/slides/slide6.xml",
        _re_sub1(r'(<a:blip r:embed="rId\d+")', r'\1 r:link="rIdLocal"', slide))


# ---- XML 层 ----
def m_xml_doctype(pkg):
    """slide10 里注入 DTD 声明（M1.18）。"""
    s = text_of(pkg, "ppt/slides/slide10.xml")
    i = s.index("?>") + 2
    put(pkg, "ppt/slides/slide10.xml", s[:i] + "<!DOCTYPE p:sld [<!ENTITY x 'y'>]>" + s[i:])


def m_xml_encoding(pkg):
    """slide10 的 XML 声明改成非 UTF-8/UTF-16 的编码名（M1.17）。"""
    s = text_of(pkg, "ppt/slides/slide10.xml")
    put(pkg, "ppt/slides/slide10.xml",
        s.replace('encoding="UTF-8"', 'encoding="gb2312"', 1))


def m_xml_not_wellformed(pkg):
    """slide11 删掉一个叶子元素的闭合标签（非良构）。"""
    s = text_of(pkg, "ppt/slides/slide11.xml")
    m = re.search(r"<a:t>[^<]*</a:t>", s)
    put(pkg, "ppt/slides/slide11.xml", s[:m.end() - len("</a:t>")] + s[m.end():])


# ---- 语义（Part 4 附加语法约束）----
def m_sem_cnvpr_dup(pkg):
    """slide12: 把两个 <p:cNvPr> 的 id 改成相同（形状树内 id 必须唯一）。"""
    s = text_of(pkg, "ppt/slides/slide12.xml")
    ids = re.findall(r'<p:cNvPr id="(\d+)"', s)
    assert len(ids) >= 2
    put(pkg, "ppt/slides/slide12.xml",
        s.replace(f'id="{ids[1]}"', f'id="{ids[0]}"', 1))


def m_app_count(pkg):
    """docProps/app.xml: Slides 计数改成 99（应用层缓存不一致）。"""
    s = text_of(pkg, "docProps/app.xml")
    put(pkg, "docProps/app.xml",
        _re_sub1(r"<Slides>\d+</Slides>", "<Slides>99</Slides>", s))


# ---- 容器 ----
def m_zip_duplicate(pkg):
    """复制一个 ZIP 条目的名字（M3.3 条目名必须唯一）。"""
    return None      # 特殊：由 build_mutant 处理（需要控制写入顺序）


MUTATIONS: list[Mutation] = [
    # 结构性：算触发项，low 应当能修
    Mutation("xsd-csld-order", "structural", "M1.20", True, True, True,
             "slide2", m_xsd_csld_order, "cSld 子元素顺序违反 XSD sequence"),
    Mutation("xsd-sp-order", "structural", "M1.20", True, True, True,
             "slide2", m_xsd_sp_order, "p:sp 内 txBody 排到 spPr 之前"),
    Mutation("xsd-missing-attr", "structural", "M1.20", True, False, False,
             "presentation", m_xsd_missing_attr, "p:sldSz 缺必需属性 cx"),
    # 取值性：**不应**算触发项
    Mutation("xsd-bad-type", "value", "M1.20", False, False, False,
             "presentation", m_xsd_bad_type, "cx=\"abc\" 类型错误"),
    Mutation("xsd-zero-coord", "value", "M1.20", False, False, False,
             "presentation", m_xsd_zero_coord, "notesSz cy=0 违反下界"),
    # 内容类型 / OPC
    Mutation("opc-mime-syntax", "structural", "M1.13", True, True, True,
             "[Content_Types].xml", m_opc_mime_syntax, "ContentType=\"image\" 语法非法"),
    Mutation("opc-ct-missing", "structural", "M1.2", True, False, True,
             "[Content_Types].xml", m_opc_ct_missing, "部件失去内容类型"),
    Mutation("opc-ct-orphan", "structural", "M1.2", True, True, True,
             "[Content_Types].xml", m_opc_ct_orphan, "Override 指向不存在的部件"),
    Mutation("opc-rid-empty", "structural", "P4-ASC", False, True, True,
             "slide1", m_opc_rid_empty, "r:id 置空"),
    Mutation("opc-rel-dangle", "structural", "P4-ASC", True, False, True,
             "slide3", m_opc_rel_dangle, "引用的关系被删除"),
    Mutation("opc-rel-target", "structural", "M1.29", True, False, False,
             "slide3", m_opc_rel_target, "内部关系目标不存在"),
    Mutation("opc-rel-null", "app", "APP-REL-PLACEHOLDER", False, False, True,
             "slide5", m_opc_rel_null, "Target=\"NULL\" 占位符"),
    Mutation("opc-rel-localpath", "app", "APP-REL-LOCALPATH", False, False, True,
             "slide6", m_opc_rel_localpath, "外部关系指向本机绝对路径"),
    # XML 层
    Mutation("xml-doctype", "structural", "M1.18", True, True, True,
             "slide10", m_xml_doctype, "注入 DTD 声明"),
    Mutation("xml-encoding", "structural", "M1.17", True, True, True,
             "slide10", m_xml_encoding, "编码声明非 UTF-8/16"),
    Mutation("xml-not-wellformed", "structural", "M1.20", True, False, False,
             "slide11", m_xml_not_wellformed, "非良构 XML"),
    # 语义
    Mutation("sem-cnvpr-dup", "app", "P4-ASC", False, False, True,
             "slide12", m_sem_cnvpr_dup, "同一形状树内 cNvPr/@id 重复"),
    Mutation("app-count", "app", "APP-APPXML-COUNTS", False, False, False,
             "app", m_app_count, "app.xml 计数不一致"),
    # 容器
    Mutation("zip-duplicate", "container", "M3.3", True, True, True,
             "zip", m_zip_duplicate, "ZIP 条目名重复"),
]

BY_KEY = {m.key: m for m in MUTATIONS}


def build_mutant(base: Pkg, keys: list[str], out: str) -> str:
    pkg = dict(base)
    for k in keys:
        m = BY_KEY[k]
        if m.key == "zip-duplicate":
            continue
        m.fn(pkg)
    order = list(base)
    if "zip-duplicate" in keys:
        # 在写入时多写一份同名条目
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in order:
                z.writestr(zipfile.ZipInfo(name, date_time=FIXED_DT), pkg[name])
            z.writestr(zipfile.ZipInfo("ppt/slides/slide1.xml", date_time=FIXED_DT),
                       pkg["ppt/slides/slide1.xml"])
        return out
    return save(pkg, out, order)


# --------------------------------------------------------------------------- #
def diag_keys(path: str) -> collections.Counter:
    """(诊断码, 严重性, 部件) 的多重集。

    用差集而不是"码是否存在"来判定，否则基线里已有的 M1.20 会把
    "取值类不应算触发项"这类断言全部污染掉（第一版就踩了这个坑）。
    """
    bag, _pkg, _parts = do_scan(path, verify_crc=True)
    return collections.Counter(
        (d.code, d.severity.name, d.part or "") for d in bag.items)


def instance_keys(path: str) -> collections.Counter:
    """(诊断码, 部件, 消息) 的多重集 —— 用来判定"**这一条**注入的缺陷还在不在"。

    只按诊断码判定是不够的：基线里本来就有 4 条 P4-ASC，
    修好注入的那一条之后码还在，会被误判成"没修好"。
    """
    bag, _pkg, _parts = do_scan(path, verify_crc=True)
    return collections.Counter((d.code, d.part or "", d.message) for d in bag.items)


def trigger_keys(path: str) -> set:
    bag, _pkg, _parts = do_scan(path, verify_crc=True)
    return {(d.code, d.part or "") for d in bag.repair_triggers}


def codes_only(path: str) -> set:
    bag, _pkg, _parts = do_scan(path, verify_crc=True)
    return {d.code for d in bag.items if d.severity.name == "ERROR"}


def run_repair(src: str, level: str, out: str) -> dict:
    bag, pkg, parts = do_scan(src, verify_crc=True)
    plan = build_plan(pkg, parts, bag, level)
    manifest = apply_repair(src, out, pkg, parts, bag, plan)
    return manifest


# --------------------------------------------------------------------------- #
def _mut_touched(m) -> set:
    return {m.slot}


def phase_single(base, base_keys, base_trig, td, report):
    print("\n=== 阶段 A：单缺陷变异（能力验证）===")
    print(f"  {'变异':<20}{'类别':<11}{'预期码':<22}{'检出':<6}{'新增':<6}"
          f"{'分级':<7}{'low修':<7}{'high修':<8}{'旁及'}")
    rows = []
    for m in MUTATIONS:
        path = build_mutant(base, [m.key], os.path.join(td, f"A_{m.key}.pptx"))
        base_i = instance_keys(base_path_global)
        delta = instance_keys(path) - base_i
        injected = {k for k in delta if k[0] == m.expect}
        got = bool(injected)
        extra = {k: v for k, v in delta.items() if k[0] != m.expect}
        collateral = {k[0] for k in extra}

        new_trig = trigger_keys(path) - base_trig
        trig_codes = {c for (c, _p) in new_trig}
        trig_verdict = "—"
        if got:
            if m.trigger:
                trig_verdict = "✔" if m.expect in trig_codes else "✘漏判"
            else:
                trig_verdict = "✔" if m.expect not in trig_codes else "✘误判"

        low_ok = high_ok = None
        if got and (m.fix_low or m.fix_high):
            lp = os.path.join(td, f"A_{m.key}_low.pptx")
            run_repair(path, "low", lp)
            low_ok = injected.isdisjoint(instance_keys(lp))
            hp = os.path.join(td, f"A_{m.key}_high.pptx")
            run_repair(path, "high", hp)
            high_ok = injected.isdisjoint(instance_keys(hp))

        def mark(expect, actual):
            return "—" if actual is None else ("✔" if actual == expect else "✘")

        print(f"  {m.key:<20}{m.cls:<11}{m.expect:<22}{'✔' if got else '✘':<6}"
              f"{sum(delta.values()):<6}{trig_verdict:<7}"
              f"{mark(m.fix_low, low_ok):<7}{mark(m.fix_high, high_ok):<8}"
              f"{','.join(sorted(collateral)) or '-'}")
        rows.append(dict(key=m.key, cls=m.cls, expect=m.expect, detected=got,
                         delta={f"{k[0]}|{k[1]}|{k[2][:40]}": v for k, v in delta.items()},
                         collateral=sorted(collateral), trigger_verdict=trig_verdict,
                         low_fixed=low_ok, high_fixed=high_ok,
                         fix_low_expected=m.fix_low, fix_high_expected=m.fix_high,
                         note=m.note))
    report["single"] = rows
    miss = [r for r in rows if not r["detected"]]
    bad_grade = [r for r in rows if r["trigger_verdict"] not in ("✔", "—")]
    bad_fix = [r for r in rows
               if (r["low_fixed"] is not None and r["low_fixed"] != r["fix_low_expected"])
               or (r["high_fixed"] is not None and r["high_fixed"] != r["fix_high_expected"])]
    print(f"\n  检出 {len(rows)-len(miss)}/{len(rows)}；分级错误 {len(bad_grade)}；"
          f"修复档位不符 {len(bad_fix)}")
    if miss: print("  漏检:", [r["key"] for r in miss])
    if bad_grade: print("  分级错误:", [r["key"] for r in bad_grade])
    if bad_fix: print("  档位不符:", [r["key"] for r in bad_fix])
    return rows


def phase_mixed(base, base_keys, td, report, lo=2, hi=6, n=10, seed=1, render=False):
    print(f"\n=== 阶段 B：{lo}–{hi} 个混合缺陷（{n} 组）===")
    rng = random.Random(seed)
    usable = [m for m in MUTATIONS]
    rows = []
    print(f"  {'组合':<48}{'注入':<6}{'检出':<6}{'delta':<7}{'多余':<6}{'low后残留':<10}{'high后残留'}")
    for i in range(n):
        k = rng.randint(lo, hi)
        picked, slots = [], set()
        pool = usable[:]
        rng.shuffle(pool)
        for m in pool:
            if len(picked) >= k:
                break
            if m.slot in slots:
                continue
            slots.add(m.slot)
            picked.append(m)
        keys = [m.key for m in picked]
        path = build_mutant(base, keys, os.path.join(td, f"B{i}.pptx"))
        base_i = instance_keys(base_path_global)
        delta = instance_keys(path) - base_i
        exp = {m.expect for m in picked}
        detected = {f"{m.key}": any(k[0] == m.expect for k in delta) for m in picked}
        injected = {k for k in delta if k[0] in exp}
        extra = {k: v for k, v in delta.items() if k[0] not in exp}
        lp = os.path.join(td, f"B{i}_low.pptx")
        run_repair(path, "low", lp)
        lres = injected & set(instance_keys(lp))
        hp = os.path.join(td, f"B{i}_high.pptx")
        run_repair(path, "high", hp)
        hres = injected & set(instance_keys(hp))
        eq = None
        print(f"  {','.join(keys)[:46]:<48}{len(picked):<6}"
              f"{sum(detected.values()):<6}{sum(delta.values()):<7}"
              f"{sum(extra.values()):<6}{len(lres):<10}{len(hres)}")
        rows.append(dict(keys=keys, injected=len(picked),
                         detected=sum(detected.values()),
                         detect_detail=detected,
                         extra={f"{k[0]}|{k[1]}|{k[2][:30]}": v for k, v in extra.items()},
                         low_residual=len(lres), high_residual=len(hres),
                         equivalence=(eq["ok"] if eq else None),
                         render_ok=None))
    report["mixed"] = rows
    tot = sum(r["injected"] for r in rows)
    det = sum(r["detected"] for r in rows)
    print(f"\n  合计注入 {tot}，检出 {det}（{det/tot*100:.1f}%）")
    return rows


def phase_heavy(base, base_keys, td, report, seed=7, render=True, base_path=None):
    print("\n=== 阶段 C：6+ 混合缺陷 + 修复 + 等价性 ===")
    base_render = render_text(None, base_path) if (render and base_path) else None
    if render:
        print(f"  干净基线渲染: " + (f"{base_render[1]} 页 / {len(base_render[0])} 字符"
                                   if base_render else "（LibreOffice 不可用）"))
    report["heavy"] = []
    for idx, size in enumerate((7, 9, 12)):
        rng = random.Random(seed + idx)
        pool = MUTATIONS[:]
        rng.shuffle(pool)
        picked, slots = [], set()
        for m in pool:
            if len(picked) >= min(size, len({x.slot for x in MUTATIONS})):
                break
            if m.slot in slots:
                continue
            slots.add(m.slot)
            picked.append(m)
        keys = [m.key for m in picked]
        path = build_mutant(base, keys, os.path.join(td, f"C{idx}.pptx"))
        base_i = instance_keys(base_path_global)
        delta = instance_keys(path) - base_i
        exp = {m.expect for m in picked}
        injected = {k for k in delta if k[0] in exp}
        exp_low = {m.expect for m in picked if m.fix_low}
        exp_high = {m.expect for m in picked if m.fix_high}
        print(f"  C{idx}: 注入 {len(picked)} 项（其中声明可被 low 修 {len(exp_low)} 类、"
              f"high 修 {len(exp_high)} 类）")
        lp = os.path.join(td, f"C{idx}_low.pptx")
        ml = run_repair(path, "low", lp)
        lc = injected & set(instance_keys(lp))
        hp = os.path.join(td, f"C{idx}_high.pptx")
        mh = run_repair(path, "high", hp)
        hc = injected & set(instance_keys(hp))
        eq = verify(path, hp, allow_external_rel_drop=True)
        # 渲染检查要拿"修复后"和**干净基线**比 —— 变异体本身可能已被破坏到
        # LibreOffice 都打不开，拿它当基准什么都验证不了。
        rep_render = render_text(None, hp) if (render and base_render) else None
        mut_render = render_text(None, path) if (render and base_render) else None
        if base_render is None:
            rcheck = {"ok": None, "note": "LibreOffice 不可用"}
        elif mut_render is None:
            # 变异体已经被破坏到 LibreOffice 都打不开 —— 此时**无法**做等价比对。
            # 把它判成"不一致"是错怪工具；真正该看的是"修复后还有没有内容丢失"。
            rcheck = {"ok": None,
                      "note": "变异体本身不可渲染，无法做等价比对",
                      "mutant_renderable": False,
                      "repaired_renderable": rep_render is not None,
                      "repaired_pages": rep_render[1] if rep_render else None,
                      "repaired_chars": len(rep_render[0]) if rep_render else None,
                      "base_chars": len(base_render[0]),
                      "unfixed_content_loss": bool(
                          rep_render is not None
                          and len(rep_render[0]) < len(base_render[0]))}
        else:
            rcheck = {"ok": mut_render == rep_render, "mutant_renderable": True,
                      "mutant_chars": len(mut_render[0]),
                      "repaired_chars": len(rep_render[0]) if rep_render else None}
        eq["checks"]["render"] = rcheck
        print(f"       注入后新增诊断 {sum(delta.values())} 条，期望码 {sorted(exp)}")
        bad_low = {k for k in lc if k[0] in exp_low}
        bad_high = {k for k in hc if k[0] in exp_high}
        print(f"       low  后注入项残留 {len(lc)}/{len(injected)}"
              f"（其中『应修未修』{len(bad_low)}）  postcondition={ml['postcondition_ok']}")
        print(f"       high 后注入项残留 {len(hc)}/{len(injected)}"
              f"（其中『应修未修』{len(bad_high)}）  postcondition={mh['postcondition_ok']}")
        print("       等价性: " + "  ".join(
            f"{k}={'✔' if v.get('ok') else ('✘' if v.get('ok') is False else '—')}"
            for k, v in eq["checks"].items()) + f"  → {'通过' if eq['ok'] else '不通过'}")
        if render and base_render:
            rr = eq["checks"]["render"]
            if rr.get("mutant_renderable") is False:
                print(f"       渲染: 变异体不可渲染 → 等价性无法判定；"
                      f"修复后 {'可渲染 ' + str(rr['repaired_pages']) + ' 页/' + str(rr['repaired_chars']) + ' 字符' if rr['repaired_renderable'] else '仍不可渲染'}"
                      f"（基线 {base_render[1]} 页/{len(base_render[0])} 字符）"
                      + ("  ⚠ 仍有未修复的内容丢失" if rr.get("unfixed_content_loss") else ""))
            elif rr.get("ok") is not None:
                print(f"       渲染: 变异体 {rr['mutant_chars']} 字符 vs 修复后 "
                      f"{rr['repaired_chars']} 字符 → {'一致 ✔' if rr['ok'] else '不一致 ✘'}")
        report["heavy"].append(dict(keys=keys, injected=len(injected),
                                    low_residual=len(lc), high_residual=len(hc),
                                    low_missed=[k[0] for k in bad_low],
                                    high_missed=[k[0] for k in bad_high],
                                    low_postcondition=ml["postcondition_ok"],
                                    high_postcondition=mh["postcondition_ok"],
                                    equivalence=(eq["ok"] if eq else None),
                         render_ok=None))


def phase_garbage(td, report):
    print("\n=== 阶段 D：抗损坏能力（不能崩）===")
    cases = []
    # 空文件
    p = os.path.join(td, "D_empty.pptx")
    open(p, "wb").close()
    cases.append(("空文件", p))
    # 非 zip 随机字节
    p = os.path.join(td, "D_random.pptx")
    open(p, "wb").write(os.urandom(4096))
    cases.append(("随机字节", p))
    # 截断
    raw = open(ORIGIN, "rb").read()
    p = os.path.join(td, "D_truncated.pptx")
    open(p, "wb").write(raw[: len(raw) // 2])
    cases.append(("截断一半", p))
    # 只有一个普通 zip（非 OOXML）
    p = os.path.join(td, "D_notooxml.pptx")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "hi")
    cases.append(("非 OOXML 的 zip", p))
    # 缺 [Content_Types].xml
    pkg = load(ORIGIN)
    pkg.pop("[Content_Types].xml", None)
    p = save(pkg, os.path.join(td, "D_noct.pptx"))
    cases.append(("缺内容类型流", p))
    # 条目数据损坏（CRC 不符）
    p = os.path.join(td, "D_badcrc.pptx")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in load(ORIGIN).items():
            if name == "ppt/slides/slide1.xml":
                data = data[: len(data) // 2]      # 声明长度与实际不符
            z.writestr(name, data)
    cases.append(("条目数据被截断", p))

    rows = []
    print(f"  {'用例':<18}{'结果':<10}{'error 码':<28}{'耗时'}")
    for label, path in cases:
        t0 = time.time()
        try:
            bag, _pkg, _parts = do_scan(path, verify_crc=True)
            errs = sorted({d.code for d in bag.items if d.severity.name == "ERROR"})
            status = "正常报错" if errs else "无 error"
            print(f"  {label:<18}{status:<10}{','.join(errs)[:26]:<28}{time.time()-t0:.2f}s")
            rows.append(dict(case=label, crashed=False, error_codes=errs))
        except Exception as exc:                     # noqa: BLE001
            print(f"  {label:<18}{'崩溃 ✘':<10}{type(exc).__name__+': '+str(exc)[:40]:<28}")
            rows.append(dict(case=label, crashed=True, exception=repr(exc)))
    report["garbage"] = rows
    return rows


# --------------------------------------------------------------------------- #
def main() -> int:
    global ORIGIN                      # 必须在任何读到 ORIGIN 的语句之前声明
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="6+ 阶段做 LibreOffice 渲染等价")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--origin", default=ORIGIN, help="真实样本 pptx 路径")
    ap.add_argument("--out", default="adversarial-out", help="明细 JSON 输出目录")
    args = ap.parse_args()

    ORIGIN = args.origin
    if not os.path.isfile(ORIGIN):
        print(f"✘ 找不到样本：{ORIGIN}")
        print("  用 --origin 指定，或设置 OOXML_DOCTOR_FIXTURE。")
        print("  真实课件不随仓库分发（第三方版权），请自行准备一个损坏的 pptx。")
        return 2
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    base = load(ORIGIN)
    base_path = os.path.join(WORK, "base.pptx")
    save(base, base_path)
    global base_path_global
    base_path_global = base_path
    base_keys = diag_keys(base_path)
    base_trig = trigger_keys(base_path)
    print(f"基线：{ORIGIN}")
    print(f"  诊断分布：{ {f'{k[0]}/{k[1]}': v for k, v in base_keys.items()} }")
    print(f"  触发项：{sorted(base_trig)}")

    report = {"origin": ORIGIN,
              "baseline": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in base_keys.items()},
              "baseline_triggers": sorted(f"{c}@{p}" for c, p in base_trig),
              "seed": args.seed, "render": args.render}
    t0 = time.time()
    phase_single(base, base_keys, base_trig, WORK, report)
    phase_mixed(base, base_keys, WORK, report, 2, 6, 10, args.seed, False)
    phase_heavy(base, base_keys, WORK, report, args.seed + 7, args.render, base_path)
    phase_garbage(WORK, report)
    report["elapsed_sec"] = round(time.time() - t0, 1)

    with open(os.path.join(args.out, "adversarial.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\n总耗时 {report['elapsed_sec']}s；明细写入 {args.out}/adversarial.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
