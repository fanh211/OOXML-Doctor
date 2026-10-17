"""XML 层（L2）：良构性、编码（M1.17）、DTD（M1.18）、MCE 存在性（M1.19）。

用 expat 自建带**字节偏移**的轻量 DOM：
  * 偏移全是字节偏移（expat 的 CurrentByteIndex 语义）
  * 修复阶段因此可以直接做字节级外科手术式替换
  * XSD 报出的 XPath 可以在这里解析回精确的字节区间，用于代码窗口定位

元素顺序校验**不在这一层**，已交给真正的 XSD（见 ``schema.py``）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from xml.parsers import expat

from .diagnostics import Bag, NormRef
from .opc import OpcPackage

OPC = "ECMA-376 Part 2 (OPC)"

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_FAM = {P_NS: "p", A_NS: "a"}


@dataclass(eq=False)
class Node:
    qname: str
    local: str
    uri: Optional[str]
    family: Optional[str]
    attrib: dict[str, str]
    attr_uri: dict[str, Optional[str]]
    start: int          # 起始 '<' 的字节偏移
    tag_end: int        # 起始标签 '>' 之后的字节偏移
    end: int = 0        # 整个元素结束 '>' 之后的字节偏移
    parent: Optional["Node"] = None
    children: list["Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    @property
    def text_content(self) -> str:
        return "".join(self.text)

    def attr(self, local: str, uri: Optional[str] = None) -> Optional[str]:
        for k, v in self.attrib.items():
            if k.split(":")[-1] != local:
                continue
            if uri is None or self.attr_uri.get(k) == uri:
                return v
        return None

    def iter(self) -> Iterable["Node"]:
        yield self
        for c in self.children:
            yield from c.iter()

    def find_path(self, locals_: list[str]) -> Optional["Node"]:
        """按 localname 序列查找（忽略命名空间前缀与位置谓词）。"""
        if not locals_:
            return self
        if self.local != locals_[0]:
            return None
        if len(locals_) == 1:
            return self
        for c in self.children:
            got = c.find_path(locals_[1:])
            if got is not None:
                return got
        return None


@dataclass
class XmlPart:
    name: str
    data: bytes
    text: str
    root: Optional[Node]
    error: Optional[str] = None
    has_mc: bool = False

    def slice(self, start: int, end: int) -> str:
        return self.data[start:end].decode("utf-8", "replace")


def _scan_tag_end(data: bytes, start: int) -> int:
    i = start + 1
    n = len(data)
    quote = 0
    while i < n:
        c = data[i]
        if quote:
            if c == quote:
                quote = 0
        elif c in (0x22, 0x27):
            quote = c
        elif c == 0x3E:      # '>'
            return i + 1
        i += 1
    return n


def build_tree(data: bytes):
    """只建树、不产生任何诊断。返回 ``(root, (msg, line, col) | None)``。

    修复阶段需要对"假设已改动"的字节反复试算并重新校验，
    所以需要一个无副作用的轻量解析入口。
    """
    root_holder: list[Node] = []
    stack: list[Node] = []
    ns_stack: list[dict[str, str]] = [{"xml": "http://www.w3.org/XML/1998/namespace"}]

    parser = expat.ParserCreate()
    parser.buffer_text = True

    def start_handler(qname, attrs):
        scope = dict(ns_stack[-1])
        for k, v in attrs.items():
            if k == "xmlns":
                scope[""] = v
            elif k.startswith("xmlns:"):
                scope[k[6:]] = v
        prefix = qname.split(":")[0] if ":" in qname else ""
        uri = scope.get(prefix)
        node = Node(qname=qname, local=qname.split(":")[-1], uri=uri,
                    family=_FAM.get(uri or ""), attrib=dict(attrs),
                    attr_uri={k: scope.get(k.split(":")[0] if ":" in k else "")
                              for k in attrs},
                    start=parser.CurrentByteIndex, tag_end=0)
        node.tag_end = _scan_tag_end(data, node.start)
        ns_stack.append(scope)
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
        else:
            root_holder.append(node)
        stack.append(node)

    def end_handler(_qname):
        if stack:
            node = stack[-1]
            # expat 的 CurrentByteIndex 在 EndElementHandler 里语义有歧义：
            #   自闭合标签 -> 指向该标签之后
            #   普通标签   -> 指向它自己的结束标签 '<'
            # 用"自身是否自闭合"来无歧义地判定。
            if node.tag_end == node.start or \
                    (node.tag_end >= 2 and data[node.tag_end - 2:node.tag_end] == b"/>"):
                node.end = node.tag_end
            else:
                node.end = _scan_tag_end(data, parser.CurrentByteIndex)
            stack.pop()
        if ns_stack:
            ns_stack.pop()

    def char_handler(chunk):
        if stack:
            stack[-1].text.append(chunk)

    parser.StartElementHandler = start_handler
    parser.EndElementHandler = end_handler
    parser.CharacterDataHandler = char_handler
    try:
        parser.Parse(data, True)
        return (root_holder[0] if root_holder else None, None)
    except (expat.ExpatError, ValueError) as exc:
        # ValueError：expat 对声明的多字节编码（如 gb2312）会直接抛
        # "multi-byte encodings are not supported"，必须和 ExpatError 一样兜住。
        return (root_holder[0] if root_holder else None,
                (str(exc),
                 getattr(parser, "ErrorLineNumber", 0) or 0,
                 getattr(parser, "ErrorColumnNumber", 0) or 0))


_XMLDECL = re.compile(rb"^\s*<\?xml\s+([^?]*)\?>")
_DOCTYPE = re.compile(rb"<!DOCTYPE[^\[>]*(?:\[[^\]]*\])?[^>]*>", re.I)


def parse_part(name: str, data: bytes, bag: Bag) -> XmlPart:
    text = data.decode("utf-8-sig", "replace")
    part = XmlPart(name=name, data=data, text=text, root=None)
    part.has_mc = MC_NS.encode() in data

    m = _XMLDECL.match(data)
    if m:
        em = re.search(rb'encoding\s*=\s*["\']([^"\']+)["\']', m.group(1))
        if em:
            enc = em.group(1).decode("ascii", "replace").lower()
            if enc not in ("utf-8", "utf8", "utf-16", "utf-16le", "utf-16be"):
                # 字节实际能被 UTF-8 解码时，把声明改成 UTF-8 是**语义不变**的安全修复
                safe = True
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    safe = False
                bag.error("M1.17", f"XML 编码声明不是 UTF-8/UTF-16：{enc!r}", part=name,
                          ref=NormRef("M1.17", OPC, "8.1.4"),
                          notes=["OPC 只允许 UTF-8 / UTF-16。",
                                 "该部件字节本身能被 UTF-8 正常解码，"
                                 "因此把声明改写为 UTF-8 不会改变内容。"] if safe else
                                ["字节不是合法 UTF-8，无法自动判定真实编码。"],
                          helps=["repair --level low 会把声明改写为 UTF-8。"] if safe
                                else ["需要人工确认原始编码后重新另存。"],
                          repair_trigger=True, fix_level=("low" if safe else None),
                          data={"kind": "bad-encoding-decl",
                                "start": m.start() + em.start(1),
                                "end": m.start() + em.end(1)})

    dt = _DOCTYPE.search(data[:4096])
    if dt:
        bag.error("M1.18", "XML 中含 DTD 声明", part=name,
                  ref=NormRef("M1.18", OPC, "8.1.4"),
                  notes=["OPC 规定 DTD 声明必须被视为错误。",
                         "除规范原因外，DTD 也是 XXE / 实体扩展攻击的载体。",
                         "OOXML 的 XML 不使用 DTD，删除它不改变文档内容。"],
                  helps=["repair --level low 会删除该 DTD 声明。"],
                  repair_trigger=True, fix_level="low",
                  data={"kind": "doctype", "start": dt.start(), "end": dt.end()})

    part.root, err = build_tree(data)
    if err is not None:
        msg, line, col = err
        part.error = msg
        bag.error("M1.20", f"XML 无法解析：{msg}",
                  part=name, line=line or None, col=col or None,
                  ref=NormRef("M1.20", OPC, "8.1.4"),
                  notes=["解析器无法建立该部件的语法树，后续 XSD 校验与语义规则都会跳过它。",
                         "若上面还有一条 M1.17 编码类报错，请先修编码。"],
                  repair_trigger=True)

    if part.has_mc and part.root is not None:
        bag.note("M1.19", "该部件含 MCE（Markup Compatibility）内容", part=name,
                 ref=NormRef("M1.19", OPC, "8.1.4"),
                 notes=["M1.19 要求：含 MCE 的 XML 必须先按 Part 5 预处理再校验。",
                        "本工具的做法：展开 mc:AlternateContent、剥离 mc:* 属性后",
                        "再做 XSD 校验；落在 mc:Ignorable 命名空间上的 XSD 报错降级为 note。"],
                 confidence=0.8)
    return part


def parse_all(pkg: OpcPackage, bag: Bag) -> dict[str, XmlPart]:
    out: dict[str, XmlPart] = {}
    for name, entry in pkg.parts.items():
        if not name.endswith(".xml") and not name.endswith(".rels"):
            continue
        if name == "[Content_Types].xml":
            continue
        data = pkg.zip.read(entry)
        if not data:
            continue
        out[name] = parse_part(name, data, bag)
    return out
