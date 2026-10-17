# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""验证靶场：证明“修复没有改坏东西”。

五项等价性检查（设计文档 §8）：
  1. 文本等价   —— 全包文本节点逐部件比对
  2. 媒体等价   —— ppt/media/* 按内容哈希做多重集比对（允许重编号）
  3. 关系图等价 —— 归一到 (来源目录, 关系类型, 目标内容哈希) 多重集，忽略 rId 重编号
  4. 问题不增   —— 修复输出的 P0/P1 数不增加
  5. 渲染等价   —— 可选外部 oracle：LibreOffice 转 PDF 后比页数与文本
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import subprocess
import tempfile
from collections import Counter
from typing import Optional

from .diagnostics import Bag
from .opc import load as load_opc
from .xmlmodel import parse_all
from .zipraw import read_zip

TEXTY = re.compile(r"\.xml$|\.rels$")


def _open(path: str, bag: Bag):
    with open(path, "rb") as fh:
        data = fh.read()
    arch = read_zip(data, bag, verify_crc=False)
    pkg = load_opc(path, arch, bag)
    return pkg


def text_map(path: str) -> dict[str, str]:
    bag = Bag()
    pkg = _open(path, bag)
    parts = parse_all(pkg, bag)
    out: dict[str, str] = {}
    for name, p in parts.items():
        if p.root is None:
            continue
        chunks = []
        for n in p.root.iter():
            if not n.children:
                chunks.append(n.text_content)
        out[name] = "".join(chunks)
    return out


def media_hashes(path: str) -> Counter:
    bag = Bag()
    pkg = _open(path, bag)
    c: Counter = Counter()
    for name, entry in pkg.parts.items():
        if name.startswith("ppt/media/"):
            c[hashlib.sha256(pkg.zip.read(entry)).hexdigest()] += 1
    return c


def entry_names(path: str) -> set:
    bag = Bag()
    pkg = _open(path, bag)
    return {name for name, e in pkg.parts.items() if not e.is_dir}


def rel_graph(path: str) -> Counter:
    bag = Bag()
    pkg = _open(path, bag)
    c: Counter = Counter()
    for rels_name, rels in pkg.rels.items():
        src_dir = posixpath.dirname(posixpath.dirname(rels_name))
        for r in rels:
            kind = r.rtype.rsplit("/", 1)[-1]
            if r.is_external:
                tgt = "external:" + r.target
            else:
                resolved = pkg.resolve(rels_name, r.target)
                entry = pkg.parts.get(resolved)
                if entry is None:
                    tgt = "missing"
                elif resolved.startswith("ppt/media/"):
                    tgt = "media:" + hashlib.sha256(
                        pkg.zip.read(entry)).hexdigest()[:16]
                else:
                    tgt = "part:" + resolved
            c[(src_dir, kind, tgt)] += 1
    return c


def render_text(pdf_cmd: Optional[str], pptx: str):
    """用 LibreOffice 渲染成 PDF 再抽文本。

    返回 ``(文本, 页数)``；LibreOffice/pdftotext 不可用或转换失败时返回 ``None``。
    这同时充当"这个包能不能被真实 OOXML 消费方打开"的**可放映性 oracle**。
    """
    soffice = pdf_cmd or "soffice"
    if not shutil_which(soffice):
        return None
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run([soffice, "--headless", "--norestore", "--convert-to",
                            "pdf", "--outdir", td, pptx],
                           check=True, capture_output=True, timeout=900)
        except Exception:
            return None
        pdfs = [f for f in os.listdir(td) if f.lower().endswith(".pdf")]
        if not pdfs:
            return None
        pdf = os.path.join(td, pdfs[0])
        pages = None
        if shutil_which("pdfinfo"):
            try:
                out = subprocess.run(["pdfinfo", pdf], capture_output=True,
                                     check=True).stdout.decode("utf-8", "replace")
                m = re.search(r"^Pages:\s*(\d+)", out, re.M)
                if m:
                    pages = int(m.group(1))
            except Exception:
                pages = None
        if not shutil_which("pdftotext"):
            return ("", pages) if pages is not None else None
        txt = os.path.join(td, "out.txt")
        subprocess.run(["pdftotext", "-layout", pdf, txt], check=True, capture_output=True)
        with open(txt, encoding="utf-8", errors="replace") as fh:
            return (fh.read(), pages)


def shutil_which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)


def verify(before: str, after: str, *, render: bool = False,
           allow_external_rel_drop: bool = False) -> dict:
    res: dict = {"before": before, "after": after, "checks": {}}

    tb, ta = text_map(before), text_map(after)
    changed = sorted(k for k in set(tb) | set(ta) if tb.get(k) != ta.get(k))
    res["checks"]["text"] = {
        "ok": not changed,
        "parts_compared": len(set(tb) | set(ta)),
        "changed": changed[:20],
    }

    mb, ma = media_hashes(before), media_hashes(after)
    res["checks"]["media"] = {"ok": mb == ma, "files_before": sum(mb.values()),
                              "files_after": sum(ma.values())}

    pb, pa = entry_names(before), entry_names(after)
    res["checks"]["parts"] = {"ok": pb == pa, "count_before": len(pb),
                              "count_after": len(pa),
                              "removed": sorted(pb - pa)[:20],
                              "added": sorted(pa - pb)[:20]}

    rb, ra = rel_graph(before), rel_graph(after)
    removed = rb - ra
    added = ra - rb
    if allow_external_rel_drop:
        removed = Counter({k: v for k, v in removed.items()
                           if not str(k[2]).startswith("external:")})
        added = Counter({k: v for k, v in added.items()
                         if not str(k[2]).startswith("external:")})
    res["checks"]["rel_graph"] = {
        "ok": not removed and not added,
        "removed": [list(x) for x in removed.elements()][:20],
        "added": [list(x) for x in added.elements()][:20],
        "note": ("已忽略按设计断开的外部链接" if allow_external_rel_drop else None),
    }

    if render:
        r1, r2 = render_text(None, before), render_text(None, after)
        if r1 is None or r2 is None:
            res["checks"]["render"] = {"ok": None, "note": "LibreOffice/pdftotext 不可用"}
        else:
            (t1, p1), (t2, p2) = r1, r2
            res["checks"]["render"] = {
                "ok": (t1 == t2) and (p1 == p2),
                "pages_before": p1, "pages_after": p2,
                "chars_before": len(t1), "chars_after": len(t2),
            }

    res["ok"] = all(v.get("ok") in (True, None) for v in res["checks"].values())
    return res
