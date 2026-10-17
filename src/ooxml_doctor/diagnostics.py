"""诊断模型。

诊断码命名规则（见设计文档 §3.1）::

    诊断码 = 规范合规条款 ID           例如 M1.13 / M3.3 / M1.20      （OPC Annex H）
           | "P4-ASC:<约束名>"        例如 P4-ASC:ASC-PML-RELTARGET    （Part 4 附加语法约束）
           | "P5:<条款>"              例如 P5:MCE-PREPROCESS          （Part 5）
           | "XSD:<schema>"           例如 XSD:pml.xsd                （Part 4 Annex A）
           | "APP-*/PROV-*/SIM-*"     非规范推断，必须 confidence<1 或显式标注
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Optional


class Sev(IntEnum):
    NOTE = 1
    WARNING = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return {1: "note", 2: "warning", 3: "error"}[int(self)]

    @property
    def brief(self) -> str:
        return {1: "N", 2: "W", 3: "E"}[int(self)]


@dataclass(frozen=True)
class NormRef:
    """指向规范条款。req_id 是 OPC Annex H 的 M/S/O 编号（若有）。"""

    req_id: Optional[str] = None
    spec: Optional[str] = None
    clause: Optional[str] = None
    quote: Optional[str] = None

    def render(self) -> str:
        bits = []
        if self.req_id:
            bits.append(self.req_id)
        if self.clause:
            bits.append("§" + self.clause)
        head = " ".join(bits)
        if self.spec:
            return f"{self.spec}{' ' + head if head else ''}".strip()
        return head


@dataclass
class Diagnostic:
    code: str
    severity: Sev
    message: str
    # 定位
    part: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None
    snippet: Optional[str] = None
    caret: Optional[int] = None
    caret_len: int = 1
    label: Optional[str] = None
    # 解释
    notes: list[str] = field(default_factory=list)
    helps: list[str] = field(default_factory=list)
    ref: Optional[NormRef] = None
    # 行为
    repair_trigger: bool = False      # 是否会让 Office 弹"需要修复"
    fix_level: Optional[str] = None   # None | "low" | "high"
    confidence: float = 1.0           # <1.0 表示非规范推断
    normative: bool = True
    data: dict = field(default_factory=dict)

    def sort_key(self) -> tuple:
        return (-int(self.severity), self.part or "", self.line or 0, self.col or 0, self.code)


def make_snippet(data, start: int, end: int, width: int = 96):
    """围绕字节区间 [start,end) 切窗口，返回 (snippet, caret, caret_len)。

    偏移量全部是**字节**偏移（expat 的 CurrentByteIndex 语义），
    而 caret 必须是**字符**偏移，所以这里做了字节→字符换算。
    """
    if data is None:
        return ("", 0, 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    start = max(0, min(start, len(data)))
    end = max(start + 1, min(end, len(data)))

    def _prep(b: bytes) -> str:
        return b.decode("utf-8", "replace").replace("\n", "⏎").replace("\t", "→")

    wbytes = max(40, width * 2)          # 中文场景下窗口按字节放宽
    left = max(0, start - max(8, (wbytes - (end - start)) // 2))
    right = min(len(data), left + wbytes)
    left = max(0, right - wbytes)
    while left > 0 and (data[left] & 0xC0) == 0x80:
        left -= 1
    while right < len(data) and (data[right] & 0xC0) == 0x80:
        right += 1
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(data) else ""
    body = _prep(data[left:right])
    caret = len(prefix) + len(_prep(data[left:start]))
    caret_len = max(1, len(_prep(data[start:end])))
    return (prefix + body + suffix, caret, caret_len)


class Bag:
    """收集诊断，并给出总体判定。"""

    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self.t0 = time.time()
        self.elapsed = 0.0

    def add(self, d: Diagnostic) -> Diagnostic:
        self.items.append(d)
        return d

    def error(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Diagnostic(code, Sev.ERROR, message, **kw))

    def warning(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Diagnostic(code, Sev.WARNING, message, **kw))

    def note(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Diagnostic(code, Sev.NOTE, message, **kw))

    def stop(self) -> None:
        self.elapsed = time.time() - self.t0

    # ---- 查询 ----
    def by_sev(self, s: Sev) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == s]

    @property
    def n_error(self) -> int:
        return len(self.by_sev(Sev.ERROR))

    @property
    def n_warning(self) -> int:
        return len(self.by_sev(Sev.WARNING))

    @property
    def n_note(self) -> int:
        return len(self.by_sev(Sev.NOTE))

    @property
    def repair_triggers(self) -> list[Diagnostic]:
        return [d for d in self.items if d.repair_trigger]

    @property
    def fixable(self) -> list[Diagnostic]:
        return [d for d in self.items if d.fix_level]

    @property
    def will_repair(self) -> bool:
        return bool(self.repair_triggers)

    def sorted(self) -> list[Diagnostic]:
        return sorted(self.items, key=lambda d: d.sort_key())

    def codes(self) -> Iterable[str]:
        return (d.code for d in self.items)
