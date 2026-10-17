# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""L4 语义规则 + L5 来源/安全指纹。

L4 依据：ECMA-376 Part 4 "Additional Syntax Constraints" 专章
         （规范原文：这些约束无法用 XSD 表达，但属于 document conformance 要求）
L5 依据：无 —— 属非规范推断，一律 confidence < 1.0 且 normative=False
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from .diagnostics import Bag, Diagnostic, NormRef, Sev, make_snippet
from .opc import OpcPackage, R_NS, is_bad_external
from .xmlmodel import Node, XmlPart

OPC = "ECMA-376 Part 2 (OPC)"
P4 = "ECMA-376 Part 4 (Markup Language Reference)"
ASC = "P4-ASC"

SHAPE_TAGS = {"sp", "pic", "cxnSp", "graphicFrame", "grpSp"}
MEDIA_REL_SUFFIX = {"/audio": "audio", "/video": "video",
                    "/2007/relationships/media": "media"}


# --------------------------------------------------------------------------- #
def sniff_media(data: bytes) -> Optional[str]:
    if data[:3] == b"ID3":
        return "audio"
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio"
    if data[4:8] == b"ftyp":
        return "av"
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8" or data[:3] == b"GIF":
        return "image"
    return None


def _shape_info(part: XmlPart):
    """产生 (shape_node, cNvPr_id, nvPr_node) 列表。"""
    out = []
    assert part.root is not None
    for n in part.root.iter():
        if n.local not in SHAPE_TAGS or n.family != "p":
            continue
        cid = None
        nvpr = None
        for d in n.iter():
            if d is n:
                continue
            # 形状的非视觉属性在 PresentationML 里是 <p:cNvPr>，
            # 只有 DrawingML 内嵌对象才是 <a:cNvPr> —— 两者都要认。
            # 早期只认 a: 导致 cid 恒为 None，媒体规则整条是死代码。
            if d.local == "cNvPr" and d.family in ("p", "a") and cid is None:
                cid = d.attr("id")
            if d.local == "nvPr" and d.family == "p" and nvpr is None:
                nvpr = d
        out.append((n, cid, nvpr))
    return out


def _media_kind_of_rel(pkg: OpcPackage, rel) -> Optional[str]:
    for suffix, kind in MEDIA_REL_SUFFIX.items():
        if rel.rtype.endswith(suffix):
            if kind != "media":
                return kind
    if rel.is_external:
        return None
    resolved = pkg.resolve(rel.source_rels, rel.target)
    entry = pkg.parts.get(resolved)
    if entry is None:
        return None
    return sniff_media(pkg.zip.read(entry))


# --------------------------------------------------------------------------- #
def rule_relationship_usage(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag) -> None:
    """P4-ASC:ASC-PML-RELTARGET — 用了 r:id/embed/link 就必须能解析到关系。"""
    for name, part in parts.items():
        if part.root is None or name.endswith(".rels"):
            continue
        declared = {r.rid for r in pkg.rels_for(name)}
        for node in part.root.iter():
            for k, v in node.attrib.items():
                if node.attr_uri.get(k) != R_NS:
                    continue
                attr_local = k.split(":")[-1]
                if attr_local not in ("id", "embed", "link"):
                    continue
                if v in declared:
                    continue
                snip, caret, clen = _attr_span(part, node, k)
                if v == "":
                    bag.error(ASC, f"<{node.qname}> 的 {k} 为空字符串，无法解析到任何关系",
                              part=name, line=1, col=1, snippet=snip, caret=caret,
                              caret_len=clen, label=f"{k}=\"\" 是悬空引用",
                              ref=NormRef(None, P4, "Additional Syntax Constraints"),
                              notes=["空串不是合法的关系引用目标。",
                                     "原始语义（点击动作/媒体播放）已经丢失，",
                                     "PowerPoint 的“修复”也不会补回它。"],
                              helps=["删除该属性；若要恢复功能需手动重设动作或链接。"],
                              repair_trigger=False, fix_level="low",
                              data={"kind": "empty-rel-attr", "attr": k,
                                    "start": node.start, "tag_end": node.tag_end})
                else:
                    bag.error(ASC, f"<{node.qname}> 的 {k}=\"{v}\" 在关系部件中不存在",
                              part=name, line=1, col=1, snippet=snip, caret=caret,
                              caret_len=clen, label=f"未知的 rId: {v}",
                              ref=NormRef(None, P4, "Additional Syntax Constraints"),
                              repair_trigger=True, fix_level="high",
                              data={"kind": "dangling-rel-attr", "attr": k, "rid": v,
                                    "elem": node.local, "qname": node.qname,
                                    "start": node.start, "tag_end": node.tag_end})


def _attr_span(part: XmlPart, node: Node, attr_qname: str):
    from .diagnostics import make_snippet
    seg = part.data[node.start:node.tag_end]
    pat = re.compile(re.escape(attr_qname).encode() + rb'\s*=\s*"([^"]*)"')
    m = pat.search(seg)
    if not m:
        return make_snippet(part.data, node.start, node.tag_end)
    return make_snippet(part.data, node.start + m.start(), node.start + m.end())


# --------------------------------------------------------------------------- #
def rule_media(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag) -> None:
    """P4-ASC:ASC-PML-MEDIA — 媒体节点与实际媒体类型/数量的一致性。"""
    for name, part in parts.items():
        if part.root is None or not name.startswith("ppt/slides/"):
            continue
        assert part.root is not None
        nodes = [n for n in part.root.iter() if n.local in ("video", "audio")
                 and n.family == "p"]
        by_spid: dict[str, list[Node]] = defaultdict(list)
        for n in nodes:
            spid = None
            for d in n.iter():
                if d.local == "spTgt":
                    spid = d.attr("spid")
                    break
            if spid:
                by_spid[spid].append(n)

        shapes = {cid: (node, nvpr) for node, cid, nvpr in _shape_info(part) if cid}

        for spid, ns in by_spid.items():
            if len(ns) > 1:
                kinds = [n.local for n in ns]
                snip, caret, clen = _node_span(part, ns[1])
                bag.error(ASC, f"同一形状（spid={spid}）上挂了 {len(ns)} 个媒体节点：{kinds}",
                          part=name, line=1, col=1, snippet=snip, caret=caret,
                          caret_len=clen,
                          label="同一媒体对象被注册了两次",
                          ref=NormRef(None, P4, "Additional Syntax Constraints"),
                          notes=["同一个 spTgt 被多个 p:cMediaNode 指向，触发/停止行为不确定。",
                                 "这正是 PowerPoint 自动修复会造成的后果：它把 <p:video> ",
                                 "改写成 <p:audio> 而不去重。"],
                          helps=["只保留一个媒体节点（优先保留 <p:audio>）。"],
                          confidence=0.9, normative=False, fix_level="high",
                          data={"kind": "media-dup", "spid": spid,
                                "drop_starts": [n.start for n in ns[1:]],
                                "drop_ends": [n.end for n in ns[1:]]})

            shape = shapes.get(spid)
            if shape is None:
                continue
            node, nvpr = shape
            if nvpr is None:
                continue
            media_el = None
            media_kind = None
            for d in nvpr.iter():
                if d.local in ("audioFile", "videoFile", "quickTimeFile", "audioCd"):
                    media_el = d
                    break
            if media_el is not None:
                for k, v in media_el.attrib.items():
                    if media_el.attr_uri.get(k) != R_NS:
                        continue
                    for rel in pkg.rels_for(name):
                        if rel.rid == v:
                            media_kind = _media_kind_of_rel(pkg, rel)
            declared_kind = "video" if any(n.local == "video" for n in ns) else "audio"
            if media_kind in ("audio", "av") and declared_kind == "video":
                snip, caret, clen = _node_span(part, ns[0])
                bag.error(ASC, "媒体对象被声明为 <p:video>，但实际媒体是音频",
                          part=name, line=1, col=1, snippet=snip, caret=caret,
                          caret_len=clen,
                          label=f"实际媒体类型：{media_kind}",
                          ref=NormRef(None, P4, "Additional Syntax Constraints"),
                          notes=["元素类型与关系类型/文件实际类型不一致。",
                                 "常见于第三方转换库按“媒体对象”统一处理时丢失类型信息。"],
                          helps=["把该媒体节点改为 <p:audio>（repair --level high 会做）。"],
                          confidence=0.85, normative=False, fix_level="high",
                          data={"kind": "media-type-mismatch", "spid": spid,
                                "start": ns[0].start, "end": ns[0].end})

            # a:audioFile 既无 r:link 也无 r:embed
            for d in (nvpr.iter() if nvpr is not None else []):
                if d.local not in ("audioFile", "videoFile", "quickTimeFile"):
                    continue
                has_ref = any(
                    d.attr_uri.get(k) == R_NS and k.split(":")[-1] in ("link", "embed")
                    for k in d.attrib)
                if not has_ref:
                    snip, caret, clen = _node_span(part, d)
                    bag.error(ASC, f"<{d.qname}> 既没有 r:link 也没有 r:embed",
                              part=name, line=1, col=1, snippet=snip, caret=caret,
                              caret_len=clen, ref=NormRef(None, P4,
                                                          "Additional Syntax Constraints"),
                              notes=["该元素没有任何媒体引用，是空壳。"],
                              helps=["删除它（若 p14:media 扩展里已有嵌入副本，"
                                     "音频仍可播放）。"],
                              repair_trigger=False, fix_level="high",
                              data={"kind": "empty-media-el",
                                    "start": d.start, "end": d.end})


def _node_span(part: XmlPart, node: Node):
    from .diagnostics import make_snippet
    return make_snippet(part.data, node.start, max(node.tag_end, node.start + 1))


# --------------------------------------------------------------------------- #
def rule_duplicate_ids(parts: dict[str, XmlPart], bag: Bag) -> None:
    """P4-ASC:ASC-DML-CNVPID — 同一形状树内 cNvPr/@id 必须唯一。"""
    for name, part in parts.items():
        if part.root is None or name.endswith(".rels"):
            continue
        assert part.root is not None
        for tree in part.root.iter():
            if tree.local not in ("spTree", "grpSp") or tree.family != "p":
                continue
            seen: dict[str, object] = {}
            dups: dict[str, list] = {}
            for c in tree.iter():
                if c.local != "cNvPr" or c.family not in ("p", "a"):
                    continue
                i = c.attr("id")
                if not i:
                    continue
                if i in seen:
                    dups.setdefault(i, []).append(c)
                else:
                    seen[i] = c
            if not dups:
                continue
            numeric = [int(i) for i in seen if i.isdigit()]
            next_id = (max(numeric) + 1) if numeric else 2
            for k, nodes in dups.items():
                referenced = any(
                    n.local == "spTgt" and n.attr("spid") == k
                    for n in part.root.iter())
                spans = []
                for n in nodes:
                    seg = part.data[n.start:n.tag_end]
                    m = re.search(rb'\sid="' + re.escape(k).encode() + rb'"', seg)
                    if m:
                        spans.append((n.start + m.start(), n.start + m.end()))
                fixable = bool(spans) and not referenced
                bag.error(ASC, f"同一形状树内 cNvPr/@id={k} 出现 {len(nodes)+1} 次",
                          part=name, line=1, col=1,
                          ref=NormRef(None, P4, "Additional Syntax Constraints"),
                          notes=["形状 id 必须唯一，否则动画/媒体节点的 spTgt 指向不确定。",
                                 ("该 id 没有被任何 spTgt 引用，因此改 id 是安全的。"
                                  if not referenced else
                                  "该 id 被 spTgt 引用；改 id 会改变动画/媒体的目标，"
                                  "因此不自动修复。")],
                          helps=["repair --level high 会把重复项改成新的未占用 id。"]
                                if fixable else
                                ["需要人工决定保留哪一个形状的 id（有 spTgt 指向它）。"],
                          confidence=0.9, normative=False,
                          fix_level=("high" if fixable else None),
                          data={"kind": "dup-cnvpr-id", "id": k, "spans": spans,
                                "next_id": next_id, "referenced": referenced})


def rule_slide_ids(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag) -> None:
    """P4-ASC:ASC-PML-SLDID — sldIdLst 内 sldId/@id 必须唯一（规范样本条款）。"""
    pres = pkg.parts.get("ppt/presentation.xml")
    part = parts.get("ppt/presentation.xml")
    if pres is None or part is None or part.root is None:
        return
    assert part.root is not None
    lst = None
    for n in part.root.iter():
        if n.local == "sldIdLst" and n.family == "p":
            lst = n
            break
    if lst is None:
        return
    ids = [c.attr("id") for c in lst.children if c.local == "sldId"]
    dups = {k: v for k, v in Counter(ids).items() if v > 1}
    for k, v in dups.items():
        bag.error(ASC, f"<p:sldIdLst> 中 @id={k} 重复出现 {v} 次", part=part.name,
                  line=1, col=1,
                  ref=NormRef(None, P4, "Additional Syntax Constraints"),
                  notes=["规范在“附加语法约束”里把这条列为 document conformance 要求。"],
                  repair_trigger=True)
    n_slides = len([p for p in pkg.parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", p)])
    if ids and len(ids) != n_slides:
        bag.warning(ASC, f"sldIdLst 声明 {len(ids)} 张幻灯片，实际有 {n_slides} 个幻灯片部件",
                    part=part.name, line=1, col=1,
                    ref=NormRef(None, P4, "Additional Syntax Constraints"),
                    notes=["列表与实际部件不一致时，多出的幻灯片不会被放映。"],
                    confidence=0.8, normative=False)


# --------------------------------------------------------------------------- #
def rule_app_props(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag) -> None:
    """APP-APPXML-COUNTS（非规范）：app.xml 缓存计数与实际部件不符。"""
    EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
    part = parts.get("docProps/app.xml")
    if part is None or part.root is None:
        return
    assert part.root is not None
    want: dict[str, str] = {}
    app_name = app_ver = None
    for n in part.root.iter():
        if n.uri != EP:
            continue
        txt = n.text_content.strip()
        if n.local in ("Slides", "Notes", "MMClips", "HiddenSlides") and txt:
            want[n.local] = txt
        elif n.local == "Application":
            app_name = txt
        elif n.local == "AppVersion":
            app_ver = txt

    actual_slides = len([p for p in pkg.parts
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", p)])
    actual_notes = len([p for p in pkg.parts
                        if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", p)])
    actual_media = 0
    for p, e in pkg.parts.items():
        if p.startswith("ppt/media/"):
            if sniff_media(pkg.zip.read(e)) in ("audio", "video", "av"):
                actual_media += 1

    for key, act in (("Slides", actual_slides), ("Notes", actual_notes),
                     ("MMClips", actual_media)):
        if key not in want:
            continue
        try:
            got = int(want[key])
        except ValueError:
            continue
        if got != act:
            bag.note("APP-APPXML-COUNTS",
                     f"docProps/app.xml 记录 {key}={got}，实际为 {act}",
                     part="docProps/app.xml", line=1, col=1,
                     notes=["这是应用层缓存元数据，不影响能否打开；",
                            "但它能证明文件被中途改动过（例如备注页被删过）。"],
                     confidence=0.9, normative=False)
    bag.note("PROV-APPINFO",
             f"最后写入者标记：Application={app_name!r} AppVersion={app_ver!r}",
             part="docProps/app.xml", normative=False, confidence=0.9)


# --------------------------------------------------------------------------- #
PROV_PATTERNS = [
    (rb"KSO_WM_", "WPS 演示（KSO_WM_* 智能美化/单元标记）"),
    (rb'name="AS_UNIQUEID"', "Aspose.Slides（AS_UNIQUEID 对象标识标记）"),
    (rb'name="AS_VERSION"', "Aspose.Slides"),
]


def rule_provenance(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag) -> None:
    hits: dict[str, int] = Counter()
    asp_ver = None
    for name, part in parts.items():
        if not name.startswith("ppt/tags/"):
            continue
        for pat, label in PROV_PATTERNS:
            if pat in part.data:
                hits[label] += 1
        m = re.search(rb'name="AS_VERSION" val="([^"]*)"', part.data)
        if m:
            asp_ver = m.group(1).decode()

    for label, n in hits.items():
        extra = f"，版本 {asp_ver}" if asp_ver and "Aspose" in label else ""
        bag.note("PROV-WRITER", f"检测到写入者指纹：{label}{extra}（{n} 个部件）",
                 normative=False, confidence=0.95,
                 notes=["说明文件被该工具程序化重写过；",
                        "结构性缺陷通常就来自这类整包重写。"])

    stamps = {e.dos_datetime for e in pkg.zip.entries if not e.is_dir}
    if len(stamps) == 1:
        bag.note("PROV-ZIPTIME", "全部 ZIP 条目时间戳完全相同"
                 f"（{list(stamps)[0]}）", normative=False, confidence=0.9,
                 notes=["Microsoft OPC 打包器会把所有条目写成 1980-01-01 00:00；",
                        "其它时间戳说明整包是被第三方工具重打包的。"])


def rule_security(pkg: OpcPackage, bag: Bag) -> None:
    """PROV-SECURITY：宏 / OLE / ActiveX / 外部关系清单（回答“是不是病毒”）。"""
    macros = [p for p in pkg.parts if p.lower().endswith("vbaproject.bin")]
    ole = [p for p in pkg.parts if p.startswith("ppt/embeddings/")
           or p.lower().endswith((".bin", ".exe", ".dll"))]
    activex = [p for p in pkg.parts if "activex" in p.lower()]
    external = []
    for rels_name, rels in pkg.rels.items():
        for r in rels:
            if r.is_external:
                external.append((rels_name, r.rid, r.target))
    bits = []
    if macros:
        bits.append(f"宏 {len(macros)} 个")
    if ole:
        bits.append(f"嵌入对象 {len(ole)} 个")
    if activex:
        bits.append(f"ActiveX {len(activex)} 个")
    sev_ok = not (macros or activex)
    msg = "安全视图：" + ("、".join(bits) if bits else "无宏、无 ActiveX")
    d = bag.note("PROV-SECURITY", msg, normative=False, confidence=0.95) if sev_ok else \
        bag.warning("PROV-SECURITY", msg, normative=False, confidence=0.95)
    if macros:
        d.notes.append("含 VBA 宏，请确认来源可信后再启用。")
    if external:
        d.notes.append(f"外部关系 {len(external)} 条：")
        for rels_name, rid, target in external[:6]:
            d.notes.append(f"  {rels_name} [{rid}] → {target}")


def _shape_media_attr_is_rel(self, k: str) -> bool:  # pragma: no cover - 兼容占位
    return False


# --------------------------------------------------------------------------- #
_FACET_PAT = re.compile(r"\[facet 'pattern'\] The value '([^']*)' is not accepted "
                        r"by the pattern '([^']*)'")
_FACET_ENUM = re.compile(r"\[facet 'enumeration'\] The value '([^']*)' is not one of "
                         r"the enumerated values[^\[]*\[([^\]]*)\]")


def _zh_message(msg: str) -> str:
    """把 libxml2 的英文报错翻成一句人话；原文仍保留在 note 里。"""
    if "This element is not expected" in msg:
        return "元素出现在 schema 不允许的位置（多数是子元素顺序错误）"
    m = _FACET_PAT.search(msg)
    if m:
        return f"取值 {m.group(1)!r} 不符合 schema 规定的取值模式"
    m = _FACET_ENUM.search(msg)
    if m:
        return f"取值 {m.group(1)!r} 不在 schema 允许的枚举集合内"
    if "Missing child element" in msg:
        return "缺少 schema 要求的子元素"
    if "is not allowed" in msg:
        return "元素或属性在 schema 中不允许出现"
    return msg


def rule_xsd(parts: dict[str, XmlPart], bag: Bag, schema_set=None) -> None:
    """M1.20 —— 用**内置离线 XSD** 做真正的 schema 校验。

    严重性分级用真实样本校准过：
      结构性违规（元素不该出现/缺必需子元素）-> error，且会触发 Office 修复
      取值性违规（pattern/enumeration/类型 facet）-> warning，Office 会容忍
    """
    from .schema import SchemaSet, path_to_locals, schema_name_for, validate_part
    ss = schema_set or SchemaSet()
    if not ss.available:
        bag.note("XSD-MISSING", "内置 XSD 不可用，已跳过 XSD 校验", normative=False)
        return

    for name, part in sorted(parts.items()):
        if part.error is not None:          # 非良构，XSD 无意义
            continue
        for e in validate_part(name, part.data, ss):
            node = part.root.find_path(path_to_locals(e.xpath)) \
                if (part.root is not None and e.xpath) else None
            if node is not None:
                snip, caret, clen = make_snippet(part.data, node.start,
                                                 max(node.tag_end, node.start + 1))
            else:
                snip, caret, clen = None, None, 1

            schema_name = schema_name_for(name)
            common = dict(part=name, line=e.line or 1, col=e.column or 1,
                          snippet=snip, caret=caret, caret_len=clen,
                          ref=NormRef("M1.20", OPC, "8.1.4"))
            notes = [f"XSD：{schema_name}",
                     f"XPath：{e.xpath}" if e.xpath else None,
                     f"schema 原文：{e.message}"]
            notes = [n for n in notes if n]

            if e.ignorable:
                bag.note("M1.19", "XSD 报错落在 mc:Ignorable 声明的命名空间上，按 MCE 规则忽略",
                         label="MCE 可忽略内容", notes=notes, confidence=0.7, **common)
                continue

            label = "结构性违规：会让 Office 弹“需要修复”" if e.structural \
                else "取值性违规：Office 会容忍（实测官方修复不会处理）"
            d = Diagnostic("M1.20", Sev.ERROR if e.structural else Sev.WARNING,
                           _zh_message(e.message), label=label, notes=notes, **common)
            if e.structural:
                d.repair_trigger = True
                if "This element is not expected" in e.message:
                    d.fix_level = "low"
                    d.helps = ["repair --level low 会按 XSD 试算重排该元素的位置。"]
                else:
                    d.helps = ["需要人工处理：这不是位置问题，而是缺内容或类型不符。"]
            else:
                d.repair_trigger = False
                d.helps = ["取值类问题 Office 会自行容忍；如需规范可手工改正该属性值。"]
            bag.add(d)


def run_all(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag,
            schema_set=None) -> None:
    rule_xsd(parts, bag, schema_set)
    rule_relationship_usage(pkg, parts, bag)
    rule_media(pkg, parts, bag)
    rule_duplicate_ids(parts, bag)
    rule_slide_ids(pkg, parts, bag)
    rule_app_props(pkg, parts, bag)
    rule_provenance(pkg, parts, bag)
    rule_security(pkg, bag)
