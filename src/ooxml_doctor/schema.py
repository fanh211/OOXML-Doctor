# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""L3 —— XSD 校验（**完全离线**）。

Schema 随包内置在 ``ooxml_doctor/schemas/``（12 个 ISO/IEC 29500-4:2016 XSD，412 KB），
运行时**不访问网络**。

覆盖范围（只覆盖 XSD 真正能给出增量信息的部分）：

    ppt/slides|slideLayouts|slideMasters|notesSlides|notesMasters/*  -> pml.xsd
    ppt/presentation.xml, presProps, viewProps, tableStyles,
    commentAuthors.xml, tags/*                                        -> pml.xsd
    ppt/theme/*                                                       -> dml-main.xsd
    docProps/app.xml                                                  -> shared-documentPropertiesExtended.xsd
    docProps/custom.xml                                               -> shared-documentPropertiesCustom.xsd

**刻意不做 XSD 的部件**：``[Content_Types].xml``、``*.rels``、``docProps/core.xml``。
理由（做减法）：OPC 自己的 schema 极其简单，而我们对这几类部件已有信息量更大的
自研规则（M1.2/M1.12/M1.13/M1.14/M1.26/M1.28/M1.29/M3.*），再叠一层 XSD 只会增加
代码量而不增加检出率。

严重性分级（用真实样本校准过）：
    * **结构性**违规（元素不该出现 / 缺必需子元素 / 顺序错） -> error，且会让 Office 要求修复
    * **取值性**违规（pattern / enumeration / 类型 facet）    -> warning，Office 会容忍
    校准依据：PowerPoint 官方修复版仍保留了 slideMaster2 的
    ``<a:buSzPct val="80000"/>``（模式要求 25%–400%），说明取值类违规不会触发修复。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lxml import etree

_PKG_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = _PKG_DIR / "schemas"
_MATERIALIZED: Path | None = None

# 外部覆盖：命令行 --schemas DIR 或环境变量 OOXML_DOCTOR_SCHEMAS。
# 用途：让分发者可以不带内置 XSD 发布（见 THIRD-PARTY-NOTICES.md §1）。
ENV_SCHEMAS = "OOXML_DOCTOR_SCHEMAS"


def configured(directory=None) -> Optional[Path]:
    """返回"应该用哪个 XSD 目录"：显式参数 > 环境变量 > 内置。"""
    import os
    if directory:
        return Path(directory)
    env = os.environ.get(ENV_SCHEMAS)
    return Path(env) if env else None


def schema_dir() -> Path:
    """返回一个**真实存在**的 XSD 目录。

    为什么需要这一步：
      * PyInstaller onefile：XSD 被解到 sys._MEIPASS 下，__file__ 相对路径可用；
      * **zipapp (.pyz)**：__file__ 指向 zip 内部的虚拟路径，
        ``Path.is_dir()`` 为假，必须用 importlib.resources 把资源解出来。
    自检在 zipapp 形态下就是靠这里发现问题的。
    """
    global _MATERIALIZED
    if SCHEMA_DIR.is_dir():
        return SCHEMA_DIR
    if _MATERIALIZED is not None and _MATERIALIZED.is_dir():
        return _MATERIALIZED
    import tempfile
    from importlib import resources

    tmp = Path(tempfile.mkdtemp(prefix="ooxml-doctor-xsd-"))
    try:
        pkg = resources.files("ooxml_doctor").joinpath("schemas")
        for entry in pkg.iterdir():
            if entry.name.endswith(".xsd"):
                (tmp / entry.name).write_bytes(entry.read_bytes())
    except Exception:
        return SCHEMA_DIR
    _MATERIALIZED = tmp
    return tmp

MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# 部件 -> 入口 XSD
_PART_SCHEMAS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^ppt/(slides|slideLayouts|slideMasters|notesSlides|notesMasters)/[^/]+\.xml$"), "pml.xsd"),
    (re.compile(r"^ppt/(presentation|presProps|viewProps|tableStyles|commentAuthors)\.xml$"), "pml.xsd"),
    (re.compile(r"^ppt/tags/[^/]+\.xml$"), "pml.xsd"),
    (re.compile(r"^ppt/theme/[^/]+\.xml$"), "dml-main.xsd"),
    (re.compile(r"^docProps/app\.xml$"), "shared-documentPropertiesExtended.xsd"),
    (re.compile(r"^docProps/custom\.xml$"), "shared-documentPropertiesCustom.xsd"),
]

# 这些部件由自研 OPC 规则覆盖，不做 XSD
NO_XSD = {"[Content_Types].xml", "docProps/core.xml"}

# 本消费方"认识"的扩展命名空间前缀（用于 mc:AlternateContent 选择）
KNOWN_PREFIXES = {"p", "a", "r", "p14", "p15", "a14", "a16", "p16"}


@dataclass
class XsdError:
    part: str
    message: str
    xpath: str
    line: int
    column: int
    structural: bool
    ignorable: bool = False


def schema_name_for(part_name: str) -> Optional[str]:
    if part_name in NO_XSD or part_name.endswith(".rels"):
        return None
    for pat, name in _PART_SCHEMAS:
        if pat.match(part_name):
            return name
    return None


class SchemaSet:
    """懒编译 + 进程内缓存。编译 pml.xsd 约 10 ms。"""

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory is not None else schema_dir()
        self._cache: dict[str, Optional[etree.XMLSchema]] = {}

    @property
    def available(self) -> bool:
        return (self.dir / "pml.xsd").is_file()

    @property
    def files(self) -> list:
        return sorted(p.name for p in self.dir.glob("*.xsd")) if self.dir.is_dir() else []

    def get(self, name: str) -> Optional[etree.XMLSchema]:
        if name in self._cache:
            return self._cache[name]
        path = self.dir / name
        schema = None
        if path.is_file():
            try:
                parser = etree.XMLParser(resolve_entities=False, no_network=True)
                schema = etree.XMLSchema(etree.parse(str(path), parser))
            except etree.XMLSchemaParseError:
                schema = None
        self._cache[name] = schema
        return schema


_DEFAULT_SET = SchemaSet()


# --------------------------------------------------------------------------- #
# MCE（ECMA-376 Part 5 的最小预处理；OPC M1.19 要求校验前必须做）
# --------------------------------------------------------------------------- #
def _expand_mce(doc):
    """展开 mc:AlternateContent：取第一个满足 Requires 的 Choice，否则取 Fallback。"""
    changed = False
    while True:
        target = next((e for e in doc.iter() if isinstance(e.tag, str)
                       and etree.QName(e).namespace == MC_NS
                       and etree.QName(e).localname == "AlternateContent"), None)
        if target is None:
            return changed
        chosen = None
        fallback = None
        for ch in target:
            tag = etree.QName(ch)
            if tag.namespace != MC_NS:
                continue
            if tag.localname == "Choice" and chosen is None:
                req = (ch.get("Requires") or "").split()
                if req and all(r in KNOWN_PREFIXES for r in req):
                    chosen = ch
            elif tag.localname == "Fallback" and fallback is None:
                fallback = ch
        branch = chosen if chosen is not None else fallback
        parent = target.getparent()
        if parent is None:                      # 根元素本身，无法替换
            return changed
        idx = parent.index(target)
        parent.remove(target)
        if branch is not None:
            for i, child in enumerate(list(branch)):
                parent.insert(idx + i, child)
        changed = True


def _strip_mc_attributes(doc) -> set[str]:
    """删掉 mc:* 机器属性，并返回 mc:Ignorable 声明的命名空间 URI 集合。

    只删属性、保留元素 —— 因为 XSD 里 ``p:ext`` 内的 ``xsd:any`` 默认 minOccurs=1，
    把可忽略命名空间的子元素整棵删掉会让 ``p:ext`` 变空而**反而**非法。
    """
    ignorable: set[str] = set()
    nsmap_root = {v: k for k, v in (doc.nsmap or {}).items() if k}
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        for k in list(el.attrib):
            q = etree.QName(k)
            if q.namespace != MC_NS:
                continue
            if q.localname == "Ignorable":
                for pfx in (el.attrib[k] or "").split():
                    uri = el.nsmap.get(pfx)
                    if uri:
                        ignorable.add(uri)
            del el.attrib[k]
    return ignorable


# --------------------------------------------------------------------------- #
_VALUE_HINTS = ("[facet ", "is not accepted by the pattern", "is not a valid value of",
                "is not an element of the set", "not a valid value", "must be greater than",
                "must be less than", "not accepted by the pattern")
_STRUCT_HINTS = ("This element is not expected", "Missing child element",
                 "attribute is not allowed", "This element is not allowed",
                 "Expected is", "is not expected")


def classify(message: str) -> bool:
    """返回 True 表示**结构性**违规（会让 Office 要求修复）。"""
    low = message
    if any(h in low for h in _VALUE_HINTS):
        return False
    if any(h in low for h in _STRUCT_HINTS):
        return True
    return True     # 保守：未知一律按结构性处理


def validate_part(part_name: str, data: bytes,
                  schema_set: SchemaSet | None = None) -> list[XsdError]:
    ss = schema_set or _DEFAULT_SET
    name = schema_name_for(part_name)
    if name is None:
        return []
    schema = ss.get(name)
    if schema is None:
        return []
    try:
        doc = etree.fromstring(data, etree.XMLParser(resolve_entities=False,
                                                     no_network=True,
                                                     remove_blank_text=False))
    except etree.XMLSyntaxError:
        return []            # 良构性问题由 xmlmodel 层负责报
    _expand_mce(doc)
    ignorable = _strip_mc_attributes(doc)
    if schema.validate(doc):
        return []
    out: list[XsdError] = []
    for e in schema.error_log:
        ign = False
        if ignorable and e.path:
            try:
                found = doc.xpath(e.path) if not e.path.startswith("/") else doc.getroottree().xpath(e.path)
            except Exception:
                found = []
            for node in found:
                if isinstance(node.tag, str) and etree.QName(node).namespace in ignorable:
                    ign = True
                    break
        out.append(XsdError(part=part_name, message=e.message, xpath=e.path or "",
                            line=e.line, column=e.column,
                            structural=classify(e.message), ignorable=ign))
    return out


# --------------------------------------------------------------------------- #
_PATH_SEG = re.compile(r"^([A-Za-z_][\w.-]*:)?([\w.-]+)(\[\d+\])?$")


def path_to_locals(xpath: str) -> Optional[list[str]]:
    """``/p:sld/p:cSld/p:spTree/p:pic[1]/p:nvPr`` -> ['sld','cSld','spTree','pic','nvPr']"""
    if not xpath or not xpath.startswith("/"):
        return None
    out = []
    for seg in xpath.strip("/").split("/"):
        m = _PATH_SEG.match(seg)
        if not m:
            return None
        out.append(m.group(2))
    return out
