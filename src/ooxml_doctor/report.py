"""Rust 风格报告渲染器。

设计目标：**人类可读优先**。
  * 每条诊断按 rustc 的样式排版：``error[CODE]: 摘要`` / ``--> 位置`` / 代码窗口 + 插入符
  * 末尾给一段「结论 / 统计 / 处理建议」，直接回答“能不能上课、修完丢什么”
  * 同时提供 ``--format json`` 给机器读
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from . import __version__, SPEC_NAME
from .diagnostics import Bag, Diagnostic, Sev

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


class Paint:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}{RESET}" if self.on else s

    def bold(self, s): return self._w("1", s)
    def dim(self, s): return self._w("2", s)
    def red(self, s): return self._w("1;31", s)
    def yellow(self, s): return self._w("1;33", s)
    def blue(self, s): return self._w("1;34", s)
    def cyan(self, s): return self._w("1;36", s)
    def green(self, s): return self._w("1;32", s)
    def magenta(self, s): return self._w("1;35", s)


def _sev_style(p: Paint, sev: Sev, text: str) -> str:
    return {Sev.ERROR: p.red, Sev.WARNING: p.yellow, Sev.NOTE: p.cyan}[sev](text)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    out, line = [], indent
    for word in text.split(" "):
        if len(line) + len(word) + 1 > width and line.strip():
            out.append(line.rstrip())
            line = indent + word + " "
        else:
            line += word + " "
    if line.strip():
        out.append(line.rstrip())
    return out


def _width() -> int:
    try:
        return max(60, min(110, shutil.get_terminal_size((96, 24)).columns))
    except Exception:
        return 96


def render_diagnostic(d: Diagnostic, p: Paint, width: int) -> list[str]:
    out: list[str] = []
    head = f"{d.severity.label}[{d.code}]"
    out.append(_sev_style(p, d.severity, head) + ": " + p.bold(d.message))
    if not d.normative:
        out.append("  " + p.dim(f"（非规范推断，置信度 {d.confidence:.2f}）"))

    loc = d.part or "(包)"
    pos = f"{loc}:{d.line}:{d.col}" if d.line else loc
    out.append("  " + p.blue("--> ") + pos)

    if d.snippet:
        caret = d.caret or 0
        clen = max(1, d.caret_len)
        gutter = " " * (len(str(d.line or 1)) + 1)
        out.append(p.blue(gutter + " |"))
        out.append(p.blue(f"{d.line or 1} | ") + d.snippet)
        label = (" " + d.label) if d.label else ""
        out.append(p.blue(gutter + " | ") + " " * caret
                   + _sev_style(p, d.severity, "^" * clen) + _sev_style(p, d.severity, label))
        out.append(p.blue(gutter + " |"))

    for n in d.notes:
        for i, ln in enumerate(_wrap(n, width - 12, "")):
            out.append("  " + (p.cyan("= note: ") if i == 0 else "         ") + p.dim(ln))
    for h in d.helps:
        for i, ln in enumerate(_wrap(h, width - 12, "")):
            out.append("  " + (p.green("= help: ") if i == 0 else "         ") + ln)
    if d.ref and (d.ref.req_id or d.ref.clause):
        out.append("  " + p.magenta("= spec: ") + d.ref.render())
    return out


def verdict_lines(bag: Bag, p: Paint, path: str, width: int) -> list[str]:
    out: list[str] = []
    out.append(p.bold("▶ 判定"))
    if bag.will_repair:
        trig = bag.repair_triggers
        codes = sorted({d.code for d in trig})
        out.append(p.red("  这个文件会被 PowerPoint 判定为损坏，打开时弹“需要修复”。"))
        out.append(f"  触发项 {len(trig)} 条，涉及规范条款/规则：" + ", ".join(codes))
        worst = trig[0]
        out.append(f"  最直接的一条：{worst.message}")
    else:
        out.append(p.green("  未发现会让 Office 要求修复的结构性缺陷。"))
        if bag.n_error:
            out.append(p.yellow(f"  但有 {bag.n_error} 条 error 级问题，建议查看。"))
        else:
            out.append(p.green("  文件结构看起来是健康的。"))

    auto_low = [d for d in bag.items if d.fix_level == "low"]
    auto_high = [d for d in bag.items if d.fix_level == "high"]
    manual = [d for d in bag.items if d.severity == Sev.ERROR and not d.fix_level]
    if auto_low or auto_high or manual:
        out.append("")
        out.append(p.bold("▶ 修复可行性"))
        out.append(f"  --level low  可自动修 {len(auto_low)} 项（语义不变，保证能放映）")
        out.append(f"  --level high 额外修 {len(auto_high)} 项（会改变行为，自动备份）")
    if manual:
        out.append("")
        out.append(p.bold("▶ 需要人工介入（自动修复不做这一部分）"))
        out.append(p.yellow(f"  {len(manual)} 项必须人工处理 —— 工具不会替你猜："))
        for d in manual[:8]:
            action = (d.helps[0] if d.helps else "需要人工判断并修改")
            out.append(f"    · {p.bold(d.code)} {d.message}")
            out.append(f"      {p.dim('位置')} {d.part or '(包)'}")
            out.append(f"      {p.green('怎么办')} {action}")
        if len(manual) > 8:
            out.append(p.dim(f"    …另有 {len(manual) - 8} 项，见 --format json"))
    return out


def render(bag: Bag, *, path: str, color: bool = True, verbose: bool = False,
           repaired_from: Optional[str] = None) -> str:
    p = Paint(color and os.environ.get("NO_COLOR") is None)
    w = _width()
    out: list[str] = []
    out.append(p.dim("─" * w))
    out.append(p.bold(f"ooxml-doctor {__version__}") + p.dim(f"  ·  {SPEC_NAME}"))
    out.append(p.dim(f"目标: {path}"))
    if repaired_from:
        out.append(p.dim(f"（对比基线: {repaired_from}）"))
    out.append("")

    items = bag.sorted()
    if not items:
        out.append(p.green("✔ 没有发现问题。"))
    for d in items:
        if not verbose and d.severity == Sev.NOTE and d.code.startswith("PROV-"):
            continue
        out.extend(render_diagnostic(d, p, w))
        out.append("")

    out.append(p.dim("─" * w))
    out.extend(verdict_lines(bag, p, path, w))
    out.append("")
    out.append(p.dim("─" * w))
    summary = (f"{p.red('error ' + str(bag.n_error))}   "
               f"{p.yellow('warning ' + str(bag.n_warning))}   "
               f"{p.cyan('note ' + str(bag.n_note))}      "
               + p.dim(f"({bag.elapsed:.2f}s)"))
    out.append(summary)
    if bag.will_repair:
        out.append("")
        out.append("下一步：")
        out.append(p.green(f"  ooxml-doctor repair {os.path.basename(path)} --level low")
                   + p.dim("   # 最小修复，保证能放映"))
        out.append(p.yellow(f"  ooxml-doctor repair {os.path.basename(path)} --level high")
                   + p.dim("  # 激进修复，会先自动备份"))
    return "\n".join(out)


def render_json(bag: Bag, *, path: str) -> str:
    payload = {
        "tool": "ooxml-doctor",
        "version": __version__,
        "spec": SPEC_NAME,
        "target": os.path.abspath(path),
        "elapsed_sec": round(bag.elapsed, 3),
        "summary": {"error": bag.n_error, "warning": bag.n_warning, "note": bag.n_note},
        "verdict": {
            "office_will_ask_to_repair": bag.will_repair,
            "repair_triggers": [d.code for d in bag.repair_triggers],
        },
        "diagnostics": [
            {
                "code": d.code,
                "severity": d.severity.label,
                "message": d.message,
                "part": d.part,
                "line": d.line,
                "column": d.col,
                "label": d.label,
                "notes": d.notes,
                "helps": d.helps,
                "normative": d.normative,
                "confidence": d.confidence,
                "repair_trigger": d.repair_trigger,
                "fix_level": d.fix_level,
                "spec": ({"requirement": d.ref.req_id, "spec": d.ref.spec,
                          "clause": d.ref.clause} if d.ref else None),
            }
            for d in bag.sorted()
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
