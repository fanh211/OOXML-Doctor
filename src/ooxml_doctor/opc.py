# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""L1 — OPC 包模型层。

规范依据：ECMA-376 Part 2 (OPC) 主题 1 "Package Model"（M1.x / O1.x）
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

from lxml import etree

from .diagnostics import Bag, NormRef
from .zipraw import ZipArchive, ZipEntry

OPC = "ECMA-376 Part 2 (OPC)"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
RELS_CT = "application/vnd.openxmlformats-package.relationships+xml"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

CONTENT_TYPES_STREAM = "[Content_Types].xml"

# RFC 2616 §3.7
_SEPARATORS = set('()<>@,;:\\"/[]?={} \t')
_TOKEN_RE = re.compile(r"^[^()<>@,;:\\\"/\[\]?={}\x00-\x20\x7f]+$")
# RFC 3986 pchar = unreserved / pct-encoded / sub-delims / ":" / "@"
_PCHAR = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
             "!$&'()*+,;=:@")
_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# 已知「语法合法但不是注册媒体类型」的写法 —— 非规范推断
MIME_SUSPECT = {
    "image/.jpg": "image/jpeg",
    "image/.jpeg": "image/jpeg",
    "image/.png": "image/png",
    "image/jpg": "image/jpeg",
    "image/x-png": "image/png",
    "image/x-jpeg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/wav": "audio/wav",
    "application/xml ": "application/xml",
}
# 扩展名 -> 规范媒体类型（用于修复无扩展名/错误声明）
EXT_TO_MIME = {
    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
    "gif": "image/gif", "tiff": "image/tiff", "bmp": "image/bmp",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
    "mp4": "video/mp4", "wmv": "video/x-ms-wmv", "avi": "video/avi",
    "xml": "application/xml", "rels": RELS_CT,
    "emf": "image/x-emf", "wmf": "image/x-wmf",
}


# --------------------------------------------------------------------------- #
# RFC 2616 §3.7 媒体类型
# --------------------------------------------------------------------------- #
def media_type_syntax(ct: str) -> tuple[bool, str]:
    """按 RFC 2616 §3.7 的 *语法* 判定（不含注册表判定）。"""
    if ct != ct.strip():
        return False, "首尾有空白"
    if any(c in ct for c in "\r\n\t"):
        return False, "含线性空白"
    main = ct.split(";", 1)[0]
    if main.count("/") != 1:
        return False, "缺少或不唯一的 type/subtype 分隔符 '/'"
    t, s = main.split("/", 1)
    if not _TOKEN_RE.match(t):
        return False, f"type {t!r} 不是合法 token"
    if not _TOKEN_RE.match(s):
        return False, f"subtype {s!r} 不是合法 token"
    for param in ct.split(";")[1:]:
        if "=" not in param:
            return False, f"参数 {param!r} 缺少 '='"
        a, v = param.split("=", 1)
        if not _TOKEN_RE.match(a.strip()) or not v.strip():
            return False, f"参数 {param!r} 非法"
    return True, ""


def media_type_registry_smell(ct: str) -> Optional[str]:
    """非规范检查：语法合法但明显不是注册媒体类型。返回建议值或 None。"""
    if ct in MIME_SUSPECT:
        return MIME_SUSPECT[ct]
    main = ct.split(";", 1)[0].strip()
    if "/" not in main:
        return None
    _, sub = main.split("/", 1)
    if sub.startswith("."):
        return EXT_TO_MIME.get(sub.lstrip("."), None)
    if sub.endswith(".") or ".." in sub:
        return None
    return None


OOXML_PART_CT = [
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), "slide"),
    (re.compile(r"^ppt/slideLayouts/slideLayout\d+\.xml$"), "slideLayout"),
    (re.compile(r"^ppt/slideMasters/slideMaster\d+\.xml$"), "slideMaster"),
    (re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$"), "notesSlide"),
    (re.compile(r"^ppt/notesMasters/notesMaster\d+\.xml$"), "notesMaster"),
    (re.compile(r"^ppt/tags/tag\d+\.xml$"), "tags"),
]
_CT_FMT = "application/vnd.openxmlformats-officedocument.presentationml.%s+xml"


def guess_content_type(part_name: str, data: bytes = b"") -> Optional[str]:
    """为**没有内容类型声明的部件**猜一个正确的内容类型。

    仅用于修复阶段；猜不出来就返回 None（交人工，不杜撰）。
    """
    if part_name == "[Content_Types].xml":
        return "application/vnd.openxmlformats-package.content-types+xml"
    if part_name.endswith(".rels"):
        return RELS_CT
    for pat, kind in OOXML_PART_CT:
        if pat.match(part_name):
            return _CT_FMT % kind
    if part_name == "ppt/presentation.xml":
        return _CT_FMT % "presentation.main"
    if part_name in ("ppt/presProps.xml", "ppt/viewProps.xml", "ppt/tableStyles.xml",
                     "ppt/commentAuthors.xml", "ppt/notesMaster.xml"):
        stem = posixpath.basename(part_name)[:-4]
        return _CT_FMT % stem
    if part_name.startswith("ppt/theme/"):
        return "application/vnd.openxmlformats-officedocument.theme+xml"
    ext = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
    if ext in EXT_TO_MIME:
        return EXT_TO_MIME[ext]
    sniffed = _sniff_mime(data)
    return sniffed


def _sniff_mime(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return None


# --------------------------------------------------------------------------- #
# 部件名文法
# --------------------------------------------------------------------------- #
def part_name_problems(item_name: str) -> list[tuple[str, str]]:
    """返回 [(条款ID, 说明)]。item_name 是 ZIP 条目名（相对路径，无前导 /）。"""
    out: list[tuple[str, str]] = []
    name = item_name
    if name.endswith("/"):
        name = name[:-1]
    if name == "":
        return out
    if "//" in name:
        out.append(("M1.3", "部件名含空段（连续两个 '/'）"))
    for seg in name.split("/"):
        if seg == "":
            continue
        if seg.endswith("."):
            out.append(("M1.9", f"段 {seg!r} 以 '.' 结尾"))
        if set(seg) == {"."}:
            out.append(("M1.10", f"段 {seg!r} 只由 '.' 组成"))
        for ch in seg:
            if ch == "%":
                continue
            if ch not in _PCHAR:
                out.append(("M1.6", f"段 {seg!r} 含非 pchar 字符 {ch!r}"))
                break
        low = seg.lower()
        if "%2f" in low:
            out.append(("M1.7", f"段 {seg!r} 含百分号编码的 '/'"))
        if "%5c" in low:
            out.append(("M1.7", f"段 {seg!r} 含百分号编码的 '\\'"))
        for m in re.finditer(r"%([0-9A-Fa-f]{2})", seg):
            try:
                ch = chr(int(m.group(1), 16))
            except ValueError:
                continue
            if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~":
                out.append(("M1.8", f"段 {seg!r} 对未保留字符 {ch!r} 做了百分号编码"))
                break
    out.append(("M1.11", "")) if False else None
    return out


# --------------------------------------------------------------------------- #
# 关系
# --------------------------------------------------------------------------- #
@dataclass
class Relationship:
    rid: str
    rtype: str
    target: str
    mode: Optional[str]
    source_rels: str
    index: int = 0

    @property
    def is_external(self) -> bool:
        return (self.mode or "").lower() == "external"


PLACEHOLDER_TARGETS = {"null", "none", "n/a", "na", "(null)", "undefined"}

# 本机绝对路径：Windows 盘符（**大小写都要认**）、UNC、file:// URI
_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC = re.compile(r"^\\\\")
_FILE_URI = re.compile(r"^file:(//)?", re.I)


def is_bad_external(target: str) -> Optional[str]:
    """外部关系目标是否"在这个世界上找不到"。返回原因或 None。

    唯一定义处 —— 诊断、修复、清理三处都调它，避免逻辑漂移
    （早期版本在三处各写一份，结果只有一处漏了大写盘符）。
    """
    if not target:
        return "空目标"
    low = target.strip().lower()
    if low in PLACEHOLDER_TARGETS:
        return "占位符字面量"
    if _FILE_URI.match(target) or _WIN_DRIVE.match(target) or _WIN_UNC.match(target):
        return "本机绝对路径"
    return None


@dataclass
class OpcPackage:
    path: str
    zip: ZipArchive
    defaults: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    parts: dict[str, ZipEntry] = field(default_factory=dict)
    rels: dict[str, list[Relationship]] = field(default_factory=dict)
    ct_problems: list[str] = field(default_factory=list)

    # ---- 内容类型 ----
    def content_type_of(self, part_name: str) -> Optional[str]:
        key = "/" + part_name
        if key in self.overrides:
            return self.overrides[key]
        ext = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
        return self.defaults.get(ext)

    def rels_part_for(self, part_name: str) -> str:
        d, b = posixpath.split(part_name)
        return (d + "/" if d else "") + "_rels/" + b + ".rels"

    def rels_for(self, part_name: str) -> list[Relationship]:
        return self.rels.get(self.rels_part_for(part_name), [])

    def relationships_of_rels_part(self, rels_part: str) -> list[Relationship]:
        return self.rels.get(rels_part, [])

    def resolve(self, rels_part: str, target: str) -> str:
        base = posixpath.dirname(posixpath.dirname(rels_part))
        return posixpath.normpath(posixpath.join(base, target)).lstrip("/")


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def load(path: str, z: ZipArchive, bag: Bag) -> OpcPackage:
    pkg = OpcPackage(path=path, zip=z)

    # -- 内容类型流 --
    ct_entry = z.by_name(CONTENT_TYPES_STREAM)
    if ct_entry is None:
        for e in z.entries:
            if e.name.lower() == CONTENT_TYPES_STREAM.lower():
                ct_entry = e
                bag.error("M3.10", f"内容类型流名称大小写不正确：{e.name!r}",
                          part=e.name, ref=NormRef("M3.10", OPC, "9.2.6"),
                          notes=[f"必须精确为 {CONTENT_TYPES_STREAM!r}。"],
                          repair_trigger=True)
                break
    if ct_entry is None:
        bag.error("M3.10", "包内缺少内容类型流 [Content_Types].xml", part="(包)",
                  ref=NormRef("M3.10", OPC, "9.2.6"),
                  helps=["整包不可用；需要从备份恢复或重新导出。"],
                  repair_trigger=True)
        pkg.ct_problems.append("missing")
        return pkg

    raw = z.read(ct_entry)
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError as exc:
        bag.error("M1.20", f"[Content_Types].xml 不是良构 XML：{exc.msg}",
                  part=CONTENT_TYPES_STREAM, line=exc.lineno, col=exc.offset,
                  ref=NormRef("M1.20", OPC, "8.1.4"), repair_trigger=True)
        pkg.ct_problems.append("malformed")
        return pkg

    for el in root:
        tag = etree.QName(el).localname
        if tag == "Default":
            ext = (el.get("Extension") or "")
            ct = (el.get("ContentType") or "")
            if not ext:
                bag.error("M1.2", "Default 缺少 Extension 属性", part=CONTENT_TYPES_STREAM,
                          ref=NormRef("M1.2", OPC, "8.1"), repair_trigger=True)
                continue
            if ext.lower() in pkg.defaults and pkg.defaults[ext.lower()] != ct:
                bag.warning("M1.2", f"扩展名 {ext!r} 有重复且冲突的 Default 声明",
                            part=CONTENT_TYPES_STREAM, ref=NormRef("M1.2", OPC, "8.1"))
            pkg.defaults[ext.lower()] = ct
            _check_content_type(ct, CONTENT_TYPES_STREAM, bag, f'Extension="{ext}"', ext_hint=ext)
        elif tag == "Override":
            pn = (el.get("PartName") or "")
            ct = (el.get("ContentType") or "")
            if not pn.startswith("/"):
                bag.error("M1.2", f"Override PartName 必须以 '/' 开头：{pn!r}",
                          part=CONTENT_TYPES_STREAM, ref=NormRef("M1.2", OPC, "8.1"),
                          repair_trigger=True)
            if pn in pkg.overrides and pkg.overrides[pn] != ct:
                bag.warning("M1.2", f"PartName {pn!r} 有重复且冲突的 Override",
                            part=CONTENT_TYPES_STREAM, ref=NormRef("M1.2", OPC, "8.1"))
            pkg.overrides[pn] = ct
            _check_content_type(ct, CONTENT_TYPES_STREAM, bag, f'PartName="{pn}"')

    # -- 部件表 --
    for e in z.entries:
        if e.is_dir or e.name == CONTENT_TYPES_STREAM:
            continue
        pkg.parts[e.name] = e
        for code, msg in part_name_problems(e.name):
            bag.error(code, f"{msg}（条目 {e.name!r}）", part=e.name,
                      ref=NormRef(code, OPC, "8.1.1.1"), repair_trigger=True)

    _check_coverage(pkg, bag)
    _load_rels(pkg, z, bag)
    return pkg


def _check_content_type(ct: str, where: str, bag: Bag, ctx: str,
                        ext_hint: str | None = None) -> None:
    def derive() -> str | None:
        if ext_hint and ext_hint.lower() in EXT_TO_MIME:
            return EXT_TO_MIME[ext_hint.lower()]
        return media_type_registry_smell(ct)

    ok, why = media_type_syntax(ct)
    if not ok:
        fix = derive()
        bag.error("M1.13", f"内容类型不符合 RFC 2616 §3.7：{ct!r}（{why}）",
                  part=where, ref=NormRef("M1.13", OPC, "8.1.2"),
                  notes=[f"位置：{ctx}",
                         "RFC 2616 §3.7 定义 media-type = type \"/\" subtype *( \";\" parameter )。"],
                  helps=[f"改成 {fix!r}。" if fix else "改成对应的注册媒体类型，例如 image/jpeg。"],
                  repair_trigger=True, fix_level=("low" if fix else None),
                  data={"ctx": ctx, "value": ct, "kind": "syntax", "suggest": fix})
        return
    if ct != ct.strip() or " " in ct.split(";", 1)[0]:
        bag.error("M1.14", f"内容类型含线性空白：{ct!r}", part=where,
                  ref=NormRef("M1.14", OPC, "8.1.2"), repair_trigger=True,
                  data={"ctx": ctx, "value": ct, "kind": "ws"})
    fix = media_type_registry_smell(ct)
    if fix:
        bag.warning("APP-MIME-REG", f"内容类型 {ct!r} 不是注册媒体类型", part=where,
                    ref=NormRef("M1.13", OPC, "8.1.2"),
                    notes=["注意：该字符串**满足** RFC 2616 §3.7 的语法，因此 M1.13 本身不判它失败。",
                           "这是一条非规范推断规则，靠的是注册表常识。",
                           "它通常意味着写包的第三方工具/media-type 表有问题。"],
                    helps=[f"改成 {fix!r}。"], confidence=0.8, normative=False,
                    fix_level="low",
                    data={"ctx": ctx, "value": ct, "kind": "registry", "suggest": fix})


def _check_coverage(pkg: OpcPackage, bag: Bag) -> None:
    used_ext: set[str] = set()
    for name in pkg.parts:
        if name.endswith(".rels"):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        used_ext.add(ext)
        if pkg.content_type_of(name) is None:
            bag.error("M1.2", f"部件没有任何内容类型覆盖：{name}", part=name,
                      ref=NormRef("M1.2", OPC, "8.1"),
                      notes=["既没有匹配的 Default，也没有 Override。",
                             "读入方无法判断该怎么解析这个部件。"],
                      helps=["repair --level high 会按扩展名/魔数补一条 Default；",
                             "补不出来的需要人工补内容类型声明。"],
                      repair_trigger=True, fix_level=None,
                      data={"kind": "ct-missing", "part": name})
    for pn in pkg.overrides:
        if pn.lstrip("/") not in pkg.parts:
            bag.error("M1.2", f"Override 指向不存在的部件：{pn}", part=CONTENT_TYPES_STREAM,
                      ref=NormRef("M1.2", OPC, "8.1"), repair_trigger=True,
                      helps=["该 Override 指向的东西不存在，删掉它是无损操作。"],
                      fix_level="low",
                      data={"kind": "orphan-override", "partname": pn})
    for ext in pkg.defaults:
        if ext and ext not in used_ext:
            bag.note("APP-CT-UNUSED", f"Default 扩展名 {ext!r} 在包内没有任何对应部件",
                     part=CONTENT_TYPES_STREAM, confidence=0.9, normative=False,
                     notes=["不是违规，但说明内容类型表是从别处抄来/生成的，"
                            "常见的第三方重写痕迹。"],
                     data={"ext": ext})


def _load_rels(pkg: OpcPackage, z: ZipArchive, bag: Bag) -> None:
    for name, entry in pkg.parts.items():
        if not name.endswith(".rels"):
            continue
        ct = pkg.content_type_of(name)
        base = posixpath.basename(name)
        ok_name = (base == ".rels" and ("_rels/" in name)) or (
            "_rels/" in name and base.endswith(".rels"))
        if not ok_name:
            bag.error("M1.30", f"关系部件命名不符合约定：{name}", part=name,
                      ref=NormRef("M1.30", OPC, "8.3.4"), repair_trigger=True)
        if ct and ct != RELS_CT:
            bag.error("M1.30", f"关系部件的内容类型不是 Relationships 类型：{ct!r}",
                      part=name, ref=NormRef("M1.30", OPC, "8.3.4"),
                      repair_trigger=True)
        if pkg.rels_part_for(name) in pkg.parts:
            bag.error("M1.25", f"关系部件自身又带了关系部件：{name}", part=name,
                      ref=NormRef("M1.25", OPC, "8.3.1"), repair_trigger=True)

        try:
            root = etree.fromstring(z.read(entry))
        except etree.XMLSyntaxError as exc:
            bag.error("M1.20", f"关系部件不是良构 XML：{exc.msg}", part=name,
                      line=exc.lineno, col=exc.offset, ref=NormRef("M1.20", OPC, "8.1.4"),
                      repair_trigger=True)
            continue

        seen: dict[str, Relationship] = {}
        out: list[Relationship] = []
        for i, el in enumerate(root):
            if etree.QName(el).localname != "Relationship":
                continue
            rid = el.get("Id") or ""
            rtype = el.get("Type") or ""
            target = el.get("Target") or ""
            mode = el.get("TargetMode")
            rel = Relationship(rid, rtype, target, mode, name, i)
            out.append(rel)
            if rid in seen:
                bag.error("M1.26", f"Relationship Id 在同一关系部件内重复：{rid!r}",
                          part=name, ref=NormRef("M1.26", OPC, "8.3.3"),
                          repair_trigger=True)
            seen.setdefault(rid, rel)
            if not rid or not _TOKEN_RE.match(rid):
                bag.error("M1.26", f"Relationship Id 不是合法的 xsd:ID：{rid!r}",
                          part=name, ref=NormRef("M1.26", OPC, "8.3.3"),
                          repair_trigger=True)
            if not _TOKEN_RE.match(rtype.replace(":", "").replace("/", "")):
                bag.warning("M1.27", f"Relationship Type 不是合法 URI：{rtype!r}", part=name,
                            ref=NormRef("M1.27", OPC, "8.3.3.2"))
            if target == "":
                bag.error("M1.28", f"Relationship Target 为空：Id={rid!r}", part=name,
                          ref=NormRef("M1.28", OPC, "8.3.3.2"), repair_trigger=True)
            elif target.lower() in PLACEHOLDER_TARGETS:
                bag.error("APP-REL-PLACEHOLDER",
                          f"Relationship Target 是占位符字面量：{target!r}（Id={rid!r}）",
                          part=name, ref=NormRef("M1.28", OPC, "8.3.3.2"),
                          notes=["这是程序在拿不到真实路径时写下的占位字符串，",
                                 "人手不会敲出这个值 —— 典型的自动化重链接 bug。"],
                          helps=["若同一 blip/媒体还有 r:embed 指向嵌入副本，"
                                 "可以安全地删掉这条外部关系引用。"],
                          confidence=0.95, normative=False, repair_trigger=False, fix_level="high",
                          data={"rid": rid, "target": target})
            if mode and mode.lower() not in ("internal", "external"):
                bag.error("M1.28", f"TargetMode 取值非法：{mode!r}", part=name,
                          ref=NormRef("M1.28", OPC, "8.3.3.2"), repair_trigger=True)
            if (mode or "").lower() == "external":
                # 必须按**原因**分流：占位符已在上面报过 APP-REL-PLACEHOLDER，
                # 这里只处理"本机绝对路径"。早期写成 `if is_bad_external(...)`，
                # 结果 Target="NULL" 会被同时报成"指向本机绝对路径：NULL"（错消息 + 重复报）。
                if is_bad_external(target) == "本机绝对路径":
                    bag.warning("APP-REL-LOCALPATH", f"外部关系指向本机绝对路径：{target}",
                                part=name, ref=NormRef("M1.28", OPC, "8.3.3.2"),
                                notes=["这类链接只在原作者电脑上有效；",
                                       "换台机器后可能提示“更新链接”或显示异常。"],
                                helps=["改为嵌入资源，或断开该链接。"],
                                confidence=0.9, normative=False, fix_level="high",
                                data={"rid": rid, "target": target})
            else:
                resolved = pkg.resolve(name, target)
                if resolved not in pkg.parts:
                    bag.error("M1.29", f"内部关系目标不存在：{target}（Id={rid!r}）",
                              part=name, ref=NormRef("M1.29", OPC, "8.3.3.2"),
                              notes=[f"解析结果：/{resolved}"],
                              helps=["删除该关系，或恢复缺失的部件。"],
                              repair_trigger=True,
                              data={"rid": rid, "target": target, "resolved": resolved})
        pkg.rels[name] = out


def unquote_part(name: str) -> str:
    return unquote(name)
