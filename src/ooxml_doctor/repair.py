# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""两级修复引擎。

  --level low   最小修复：只做**语义不变**的规范化，目标是“保证能放映”。
                元素顺序对齐 XSD sequence、内容类型纠错、去掉空关系引用。
  --level high  激进修复：额外做**会改变行为**的处置，有概率加剧损坏。
                断外部坏链接、删空壳媒体元素、媒体节点去重/纠型、清理悬空关系。
                high 会自动先备份原文件。

所有改动都在**字节区间**上做外科手术式替换；输出新文件 + repair.json 清单。
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from .diagnostics import Bag
from .opc import (EXT_TO_MIME, OpcPackage, R_NS, guess_content_type,
                  is_bad_external, media_type_registry_smell, media_type_syntax)
from .schema import SchemaSet, path_to_locals, validate_part
from .xmlmodel import XmlPart, build_tree


@dataclass
class Edit:
    part: str
    start: int
    end: int
    new: bytes
    code: str
    level: str
    reason: str

    @property
    def kind(self) -> str:
        return "delete" if not self.new else "replace"


@dataclass
class RepairPlan:
    level: str
    edits: list[Edit] = field(default_factory=list)
    drop_entries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # 有意不改、交人工的项

    def by_part(self) -> dict[str, list[Edit]]:
        out: dict[str, list[Edit]] = {}
        for e in self.edits:
            out.setdefault(e.part, []).append(e)
        for v in out.values():
            v.sort(key=lambda x: x.start, reverse=True)
        return out


# --------------------------------------------------------------------------- #
# LOW
# --------------------------------------------------------------------------- #
def _plan_order(parts: dict[str, XmlPart], plan: RepairPlan,
                schemas: SchemaSet) -> None:
    """**由 XSD 试算驱动**的元素前移，不依赖任何手写内容模型。

    做法：对结构性 "element is not expected" 报错定位到出错的子元素，
    依次把它插到前面每一个兄弟之前，重新跑 XSD，取第一个能通过的方案。

    为什么把"前移"表达成 **删除原位置 + 在目标位置插入** 两个点编辑，
    而不是重建整段子元素区域：
      1. 点编辑彼此不相交，可以和其它修复（如删除空壳 <a:audioFile>）共存；
         早期用"重建区域"实现时，区域编辑会把区域内的其它编辑判为重叠并丢掉，
         结果 high 级修不掉那两条 Target="NULL" 的音频链接。
      2. 所有编辑都表达在**同一个坐标系**（原始字节）里，
         从后往前应用时偏移始终有效，不需要重算。
    每个部件只处理一处（多处会破坏坐标系一致性），其余留给复检报告。
    """
    if not schemas.available:
        return
    for name, part in parts.items():
        if part.root is None or part.error is not None or part.has_mc:
            continue
        errs = [e for e in validate_part(name, part.data, schemas)
                if e.structural and "This element is not expected" in e.message]
        if not errs:
            continue
        root, _err = build_tree(part.data)
        if root is None:
            continue
        moved = False
        for e in errs:
            node = root.find_path(path_to_locals(e.xpath)) if e.xpath else None
            if node is None or node.parent is None:
                continue
            parent = node.parent
            sibs = parent.children
            k = sibs.index(node)
            payload = part.data[node.start:node.end]
            shift = node.end - node.start
            rest = [c for i, c in enumerate(sibs) if i != k]
            # 试**所有**插入位置：既覆盖"排得太靠后"，也覆盖"排得太靠前"。
            # （早期版本只试 k 之前的位置，所以"元素需要往后移"的违规修不动。）
            for pos in range(len(rest) + 1):
                # 注意两个坐标系：
                #   trial_at —— 试算缓冲区里**已经删掉 node**，锚点若在 node 之后要前移 shift
                #   edit_at  —— 真正下发的编辑作用在**原始字节**上（node 仍在），锚点位置不变
                # 早期版本把 trial_at 直接当 edit_at 下发，导致"锚点在 node 之后"时插错位置。
                if pos < len(rest):
                    anchor = rest[pos]
                    edit_at = anchor.start
                    # 必须用 >=：相邻兄弟满足 anchor.start == node.end，
                    # 用 > 会把它当成"在 node 之前"，试算坐标与下发坐标错位，
                    # 编辑退化成空操作（曾经导致 low 级修不动顺序违规）。
                    trial_at = (anchor.start - shift if anchor.start >= node.end
                                else anchor.start)
                    where = "移到 <%s> 之前" % anchor.qname
                else:
                    anchor = rest[-1]
                    edit_at = anchor.end
                    trial_at = (anchor.end - shift if anchor.end >= node.end
                                else anchor.end)
                    where = "移到末尾"
                trial = part.data[:node.start] + part.data[node.end:]
                trial = trial[:trial_at] + payload + trial[trial_at:]
                if any(x.structural for x in validate_part(name, trial, schemas)):
                    continue
                at = edit_at
                plan.edits.append(Edit(
                    part=name, start=node.start, end=node.end, new=b"",
                    code="M1.20", level="low",
                    reason="把 <%s> 从原位置移走" % node.qname))
                plan.edits.append(Edit(
                    part=name, start=at, end=at, new=payload,
                    code="M1.20", level="low",
                    reason="把 <%s> %s（XSD 试算通过）" % (node.qname, where)))
                moved = True
                break
            if moved:
                break
            break        # 每部件只处理一处，避免坐标系错乱


def _plan_orphan_override(pkg: OpcPackage, bag: Bag, plan: RepairPlan) -> None:
    """低阶：删掉指向不存在部件的 Override（无损操作）。"""
    entry = pkg.zip.by_name("[Content_Types].xml")
    if entry is None:
        return
    data = pkg.zip.read(entry)
    for d in bag.items:
        if d.data.get("kind") != "orphan-override":
            continue
        pn = d.data["partname"]
        m = re.search(rb'<Override\s+PartName="' + re.escape(pn.encode()) + rb'"[^>]*/>', data)
        if m:
            plan.edits.append(Edit(
                part="[Content_Types].xml", start=m.start(), end=m.end(), new=b"",
                code="M1.2", level="low",
                reason=f"删除指向不存在部件的 Override（{pn}）"))


def _plan_missing_content_types(pkg: OpcPackage, bag: Bag, plan: RepairPlan) -> None:
    """高阶：给"没有内容类型"的部件补一条 Default。

    只有能**确定**类型时才补（按 OOXML 部件名规则、扩展名表或文件魔数）；
    猜不出来就留着交人工 —— 不杜撰。
    """
    missing = [d.data["part"] for d in bag.items
               if d.data.get("kind") == "ct-missing" and d.data.get("part")]
    if not missing:
        return
    entry = pkg.zip.by_name("[Content_Types].xml")
    if entry is None:
        return
    ct_bytes = pkg.zip.read(entry)
    ins = ct_bytes.rindex(b"</Types>")

    add: dict[str, str] = {}          # ext -> content type
    still: list[str] = []
    for part in missing:
        e = pkg.parts.get(part)
        ct = guess_content_type(part, pkg.zip.read(e) if e is not None else b"")
        ext = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        if ct and ext and ext not in pkg.defaults:
            add.setdefault(ext, ct)
        else:
            still.append(part)
    if add:
        blob = b"".join(b'<Default Extension="%s" ContentType="%s"/>'
                        % (e.encode(), c.encode()) for e, c in sorted(add.items()))
        plan.edits.append(Edit(
            part="[Content_Types].xml", start=ins, end=ins, new=blob,
            code="M1.2", level="high",
            reason="补内容类型 Default：" + "、".join(f"{e}→{c}" for e, c in sorted(add.items()))))
    for part in still:
        plan.skipped.append(f"{part}: 无法确定内容类型（扩展名/魔数都不认识），"
                            f"保留原样，需要人工补 [Content_Types].xml 声明")


def _plan_dup_cnvpr_id(bag: Bag, plan: RepairPlan) -> None:
    """高阶：把**没有被 spTgt 引用**的重复形状 id 改成新的未占用 id。"""
    for d in bag.items:
        if d.data.get("kind") != "dup-cnvpr-id":
            continue
        if d.data.get("referenced") or not d.data.get("spans"):
            plan.skipped.append(
                f"{d.part}: cNvPr/@id={d.data['id']} 重复且被 spTgt 引用，"
                f"改 id 会改变动画/媒体目标，保留原样，需要人工决定")
            continue
        nid = d.data["next_id"]
        for (a, b) in d.data["spans"]:
            plan.edits.append(Edit(
                part=d.part or "", start=a, end=b, new=b' id="%d"' % nid,
                code="P4-ASC", level="high",
                reason=f"把重复的 cNvPr/@id={d.data['id']} 改为未占用的 {nid}"))
            nid += 1


def _plan_empty_rel_attr(bag: Bag, plan: RepairPlan) -> None:
    for d in bag.items:
        if d.data.get("kind") != "empty-rel-attr":
            continue
        plan.edits.append(Edit(
            part=d.part or "", start=d.data["start"], end=d.data["tag_end"], new=b"",
            code=d.code, level="low",
            reason=f"删除空引用属性 {d.data['attr']}=\"\""))


def _plan_xml_decl(bag: Bag, plan: RepairPlan) -> None:
    """安全的两类文本级修复：改写错误的编码声明、删除 DTD 声明。"""
    for d in bag.items:
        k = d.data.get("kind")
        if k == "bad-encoding-decl":
            plan.edits.append(Edit(
                part=d.part or "", start=d.data["start"], end=d.data["end"],
                new=b"UTF-8", code="M1.17", level="low",
                reason="把 XML 编码声明改写为 UTF-8（字节本身即为 UTF-8）"))
        elif k == "doctype":
            plan.edits.append(Edit(
                part=d.part or "", start=d.data["start"], end=d.data["end"],
                new=b"", code="M1.18", level="low",
                reason="删除 DTD 声明（OOXML 不使用 DTD）"))


def _plan_content_types(pkg: OpcPackage, plan: RepairPlan) -> None:
    """在**原始字节**上单趟扫描，每个坏标签产出一条编辑。

    编辑按 start 降序应用，因此长度变化不会让更早的偏移失效。
    """
    entry = pkg.zip.by_name("[Content_Types].xml")
    if entry is None:
        return
    data = pkg.zip.read(entry)
    for m in re.compile(rb"<(Default|Override)\b[^>]*?/>").finditer(data):
        tag = m.group(0)
        ext = re.search(rb'Extension="([^"]*)"', tag)
        pn = re.search(rb'PartName="([^"]*)"', tag)
        ct = re.search(rb'ContentType="([^"]*)"', tag)
        if not ct:
            continue
        value = ct.group(1).decode("utf-8", "replace")
        ok, _ = media_type_syntax(value)
        fix = None
        if not ok:
            if ext:
                fix = EXT_TO_MIME.get(ext.group(1).decode().lower())
            if fix is None:
                fix = media_type_registry_smell(value)
        else:
            stripped = value.strip()
            if stripped != value:
                fix = stripped
            else:
                fix = media_type_registry_smell(value)
        if not fix or fix == value:
            continue
        newtag = tag[:ct.start(1)] + fix.encode() + tag[ct.end(1):]
        scope = ext.group(1).decode() if ext else (pn.group(1).decode() if pn else "?")
        plan.edits.append(Edit(part="[Content_Types].xml", start=m.start(), end=m.end(),
                               new=newtag, code="M1.13", level="low",
                               reason=f"内容类型 {value!r} → {fix!r}（{scope}）"))


# --------------------------------------------------------------------------- #
# HIGH
# --------------------------------------------------------------------------- #
# 只有这些元素上的关系引用，摘掉之后"不会改变文档结构"。
# 绝不能用"删除整个元素"来处理 <p:sldId> / <p:sldLayoutId> / <p:sldMasterId> 这类
# 结构性引用 —— 那会把整张幻灯片从演示文稿里删掉。
_SAFE_DETACH_ELEMENTS = {
    "blip":        ("link",),                     # 只摘 r:link，保留 r:embed
    "hlinkClick":  ("id",),                       # 死链接，整段删掉无副作用
    "audioFile":   ("link", "embed"),
    "videoFile":   ("link", "embed"),
    "quickTimeFile": ("link", "embed"),
    "audioCd":     ("id",),
    # <p:tags> 只承载第三方元数据（WPS/学科网/动画标记），删掉不影响任何可见内容
    "tags":        ("id",),
}


def _plan_dangling_rel_attr(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag,
                            plan: RepairPlan) -> None:
    """高阶：摘掉**指向不存在关系**的引用属性 —— 但只在"不影响文档结构"时。

    安全边界（吃过一次教训）：
      * ``a:blip`` 只摘 ``r:link``（保留 ``r:embed`` 的嵌入副本），不能整段删
      * ``a:hlinkClick`` / ``a:audioFile`` / ``a:videoFile`` / ``a:quickTimeFile``
        这类"引用没了就等于没有"的元素，整段删
      * **其余一律不动**。特别是 ``<p:sldId>`` / ``<p:sldLayoutId>`` /
        ``<p:sldMasterId>`` 这类结构性引用 —— 早期版本一律"删整个元素"，
        会把整张幻灯片从演示文稿里删掉。
    """
    drop_element = {"hlinkClick", "audioFile", "videoFile", "quickTimeFile",
                    "audioCd", "tags"}
    attr_only = {"blip": {"link"}}
    for d in bag.items:
        if d.data.get("kind") != "dangling-rel-attr":
            continue
        elem = d.data.get("elem")
        attr = d.data.get("attr")
        qname = d.data.get("qname", elem)
        start, tag_end = d.data["start"], d.data["tag_end"]
        part = d.part or ""

        if elem in attr_only and attr in attr_only[elem]:
            pdata = parts[part].data if part in parts else b""
            seg = pdata[start:tag_end]
            m = re.search(rb"\s" + re.escape(attr.encode()) + rb'="[^"]*"', seg)
            if not m:
                continue
            plan.edits.append(Edit(
                part=part, start=start + m.start(), end=start + m.end(), new=b"",
                code=d.code, level="high",
                reason="摘掉 <{}> 的 {}={!r}（该关系不存在；保留 r:embed 的嵌入副本）".format(
                    qname, attr, d.data["rid"])))
        elif elem in drop_element:
            if tag_end <= start:
                continue
            plan.edits.append(Edit(
                part=part, start=start, end=tag_end, new=b"",
                code=d.code, level="high",
                reason="删除 <{}>（其 {}={!r} 指向不存在的关系，"
                       "引用没了等于该元素没有意义）".format(
                           qname, attr, d.data["rid"])))
        else:
            plan.skipped.append(
                "{}: <{}> 的 {}={!r} 指向不存在的关系；"
                "这属于文档结构引用（删掉会改变内容），已保留原样，需要人工修复".format(
                    part, qname, attr, d.data["rid"]))


def _plan_media(bag: Bag, plan: RepairPlan) -> None:
    for d in bag.items:
        k = d.data.get("kind")
        if k == "media-dup":
            for s, e in zip(d.data["drop_starts"], d.data["drop_ends"]):
                plan.edits.append(Edit(part=d.part or "", start=s, end=e, new=b"",
                                       code=d.code, level="high",
                                       reason=f"删除 spid={d.data['spid']} 上重复的媒体节点"))
        elif k == "empty-media-el":
            plan.edits.append(Edit(part=d.part or "", start=d.data["start"],
                                   end=d.data["end"], new=b"",
                                   code=d.code, level="high",
                                   reason="删除既无 r:link 也无 r:embed 的空壳媒体元素"))
        elif k == "media-type-mismatch":
            s, e = d.data["start"], d.data["end"]
            plan.edits.append(Edit(part=d.part or "", start=s, end=e, new=b"",
                                   code=d.code, level="high",
                                   reason="删除与实际媒体类型不符的媒体节点"))


def _plan_external_links(pkg: OpcPackage, parts: dict[str, XmlPart],
                         plan: RepairPlan) -> None:
    """断开指向坏外部目标的 r:link（保留 r:embed 嵌入副本）。"""
    for name, part in parts.items():
        if part.root is None or name.endswith(".rels"):
            continue
        rels = {r.rid: r for r in pkg.rels_for(name)}
        for node in part.root.iter():
            for k, v in list(node.attrib.items()):
                if node.attr_uri.get(k) != R_NS or k.split(":")[-1] != "link":
                    continue
                rel = rels.get(v)
                broken = False
                if rel is None:
                    broken = True
                elif rel.is_external:
                    if is_bad_external(rel.target):
                        broken = True
                if not broken:
                    continue
                seg = part.data[node.start:node.tag_end]
                m = re.search(re.escape(k).encode() + rb'\s*=\s*"[^"]*"', seg)
                if not m:
                    continue
                has_other_ref = any(
                    node.attr_uri.get(kk) == R_NS and kk.split(":")[-1] in ("link", "embed")
                    and kk != k for kk in node.attrib)
                if node.local in ("audioFile", "videoFile", "quickTimeFile") \
                        and not has_other_ref:
                    plan.edits.append(Edit(part=name, start=node.start, end=node.end,
                                           new=b"", code="P4-ASC", level="high",
                                           reason=f"删除空壳 <{node.qname}>（其 "
                                                  f"r:link 指向不可用目标 {v}）"))
                else:
                    s = node.start + m.start()
                    e = node.start + m.end()
                    plan.edits.append(Edit(part=name, start=s, end=e, new=b"",
                                           code="M1.28", level="high",
                                           reason=f"断开 <{node.qname}> 的坏外部链接 "
                                                  f"r:link=\"{v}\""))


# --------------------------------------------------------------------------- #
def build_plan(pkg: OpcPackage, parts: dict[str, XmlPart], bag: Bag,
               level: str, schemas: SchemaSet | None = None) -> RepairPlan:
    plan = RepairPlan(level=level)
    _plan_order(parts, plan, schemas or SchemaSet())
    _plan_xml_decl(bag, plan)
    _plan_empty_rel_attr(bag, plan)
    _plan_content_types(pkg, plan)
    _plan_orphan_override(pkg, bag, plan)
    # 冲突消解：同一 part 上区间重叠的编辑只保留优先级最高的一条
    if level == "high":
        _plan_dangling_rel_attr(pkg, parts, bag, plan)
        _plan_media(bag, plan)
        _plan_external_links(pkg, parts, plan)
        _plan_missing_content_types(pkg, bag, plan)
        _plan_dup_cnvpr_id(bag, plan)
    plan.edits = _dedupe_overlaps(plan.edits)
    return plan


def _dedupe_overlaps(edits: list[Edit]) -> list[Edit]:
    """同一区间只保留最后产生的那条（重排是置换，长度恒定，偏移不漂移）。"""
    out: list[Edit] = []
    for part in {e.part for e in edits}:
        group = [e for e in edits if e.part == part]
        kept: list[Edit] = []
        for e in group:
            replaced = False
            for i, k in enumerate(kept):
                # 零长度插入（move 的目标位置）不算与任何区间重叠
                if e.start == e.end or k.start == k.end:
                    continue
                if not (e.end <= k.start or e.start >= k.end):
                    if (e.start, e.end) == (k.start, k.end):
                        kept[i] = e          # 同区间：后一条覆盖前一条
                    replaced = True
                    break
            if not replaced:
                kept.append(e)
        out.extend(kept)
    return out


# --------------------------------------------------------------------------- #
def _apply_part_edits(data: bytes, edits: list[Edit]) -> bytes:
    for e in sorted(edits, key=lambda x: x.start, reverse=True):
        data = data[:e.start] + e.new + data[e.end:]
    return data


_REL_EL = re.compile(rb"<Relationship\b[^>]*/>")


def _cleanup_rels(pkg: OpcPackage, new_parts: dict[str, bytes], plan: RepairPlan) -> None:
    """HIGH：删除既没被引用、目标又坏掉的关系条目。"""
    for rels_name, rels in pkg.rels.items():
        part_name = posixpath.join(posixpath.dirname(posixpath.dirname(rels_name)),
                                   posixpath.basename(rels_name)[:-5])
        part_data = new_parts.get(part_name)
        entry = pkg.zip.by_name(rels_name)
        if entry is None:
            continue
        used: set[str] = set()
        if part_data:
            used = {m.group(1).decode() for m in
                    re.finditer(rb'r:(?:id|embed|link)="([^"]*)"', part_data)}
        rels_bytes = pkg.zip.read(entry)
        drop_ids = set()
        for r in rels:
            if r.rid in used:
                continue
            bad = False
            if r.is_external:
                if is_bad_external(r.target):
                    bad = True
            else:
                if pkg.resolve(rels_name, r.target) not in pkg.parts:
                    bad = True
            if bad:
                drop_ids.add(r.rid)
        if not drop_ids:
            continue
        out = rels_bytes
        for m in reversed(list(_REL_EL.finditer(rels_bytes))):
            tag = m.group(0)
            rid = re.search(rb'Id="([^"]*)"', tag)
            if rid and rid.group(1).decode() in drop_ids:
                out = out[:m.start()] + out[m.end():]
                plan.edits.append(Edit(part=rels_name, start=m.start(), end=m.end(),
                                       new=b"", code="M1.28", level="high",
                                       reason=f"删除未被引用且目标不可用的关系 {rid.group(1).decode()}"))


# --------------------------------------------------------------------------- #
def _verify_edits(pkg: OpcPackage, new_data: dict[str, bytes],
                  plan: RepairPlan) -> list[str]:
    """后置条件检查：计划里的每一处编辑，都必须真的体现在产物字节里。

    做法是"重放"：把该部件的全部编辑重新施加到原始字节上，
    结果必须与真正写出的字节逐一相等。任何一处没落地都会被这里抓到。
    """
    bad: list[str] = []
    for name, edits in plan.by_part().items():
        entry = pkg.zip.by_name(name)
        if entry is None:
            bad.append(f"{name}: 部件不存在于原包")
            continue
        expected = _apply_part_edits(pkg.zip.read(entry), edits)
        if new_data.get(name) != expected:
            bad.append(f"{name}: {len(edits)} 处编辑未按计划落地")
    return bad


def apply_repair(src: str, out_path: str, pkg: OpcPackage, parts: dict[str, XmlPart],
                 bag: Bag, plan: RepairPlan) -> dict:
    # 注意：必须遍历 **ZIP 条目** 而不是 pkg.parts —— 后者按 OPC 语义排除了
    # [Content_Types].xml，用它遍历会让内容类型修复静默丢失（真的踩过一次）。
    by_part = plan.by_part()
    new_data: dict[str, bytes] = {}
    for entry in pkg.zip.entries:
        if entry.is_dir:
            continue
        raw = pkg.zip.read(entry)
        if entry.name in by_part:
            raw = _apply_part_edits(raw, by_part[entry.name])
        new_data[entry.name] = raw
    if plan.level == "high":
        _cleanup_rels(pkg, new_data, plan)

    # 合并 rels 清理产生的新编辑
    by_part = plan.by_part()
    for name in list(by_part):
        if name.endswith(".rels") and name in new_data:
            new_data[name] = _apply_part_edits(pkg.zip.read(pkg.zip.by_name(name)),
                                               by_part[name])

    # 后置条件：计划里的每一处编辑都必须真的落在产物字节上
    unverified = _verify_edits(pkg, new_data, plan)

    seen: set[str] = set()
    zout = zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED)
    try:
        for entry in pkg.zip.entries:
            if entry.is_dir:
                continue
            if entry.name in seen:
                plan.drop_entries.append(entry.name)
                continue
            seen.add(entry.name)
            zi = zipfile.ZipInfo(entry.name, date_time=entry.dos_datetime)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o600 << 16
            zout.writestr(zi, new_data.get(entry.name, pkg.zip.read(entry)))
    finally:
        zout.close()

    manifest = {
        "tool": "ooxml-doctor",
        "level": plan.level,
        "source": os.path.abspath(src),
        "output": os.path.abspath(out_path),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "edits": [
            {"code": e.code, "level": e.level, "part": e.part, "kind": e.kind,
             "byte_range": [e.start, e.end], "reason": e.reason}
            for e in sorted(plan.edits, key=lambda x: (x.part, x.start))
        ],
        "dropped_duplicate_entries": plan.drop_entries,
        "skipped_needs_human": plan.skipped,
        "postcondition_ok": not unverified,
        "unverified_edits": unverified,
        "status": ("needs_human" if plan.skipped else "fixed"),
        "reversible": True,
        "note": "所有改动均为字节区间替换，原文件未被修改，可随时回退。",
    }
    return manifest
