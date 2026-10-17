"""L0 — ZIP 物理层。

不用 ``zipfile``：它太宽容，会静默忽略我们想报告的容器缺陷。
自己解析 EOCD / ZIP64 / 中央目录 / 本地文件头，逐字段按 OPC 主题 3 核对。

规范依据：ECMA-376 Part 2 (OPC) 主题 3 "ZIP Physical Mapping"
          M3.1 M3.2 M3.3 M3.7 M3.8 M3.9 M3.13 M3.14 M3.21 / S3.1
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import Bag, NormRef

OPC = "ECMA-376 Part 2 (OPC)"

EOCD_SIG = b"PK\x05\x06"
EOCD64_LOC_SIG = b"PK\x06\x07"
EOCD64_SIG = b"PK\x06\x08"
CDFH_SIG = b"PK\x01\x02"
LFH_SIG = b"PK\x03\x04"
DD_SIG = b"PK\x07\x08"

FLAG_ENCRYPTED = 0x0001
FLAG_DESCRIPTOR = 0x0008
FLAG_UTF8 = 0x0800

MAX_COMMENT = 0xFFFF
ALLOWED_METHODS = {0: "stored", 8: "deflated"}


@dataclass
class ZipEntry:
    name: str
    name_raw: bytes
    flags: int
    method: int
    crc: int
    csize: int
    usize: int
    dos_time: int
    dos_date: int
    local_offset: int
    extra_len: int
    comment_len: int
    index: int
    # 从本地文件头读到的值（用于 M3.14 比对）
    lfh_flags: Optional[int] = None
    lfh_method: Optional[int] = None
    lfh_crc: Optional[int] = None
    lfh_csize: Optional[int] = None
    lfh_usize: Optional[int] = None
    lfh_name_ok: Optional[bool] = None
    lfh_present: bool = False
    # 实际解压校验结果
    crc_ok: Optional[bool] = None

    @property
    def is_dir(self) -> bool:
        return self.name.endswith("/")

    @property
    def has_descriptor(self) -> bool:
        return bool(self.flags & FLAG_DESCRIPTOR)

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & FLAG_ENCRYPTED)

    @property
    def dos_datetime(self) -> tuple[int, int, int, int, int, int]:
        d, t = self.dos_date, self.dos_time
        return (
            ((d >> 9) & 0x7F) + 1980, (d >> 5) & 0x0F, d & 0x1F,
            (t >> 11) & 0x1F, (t >> 5) & 0x3F, (t & 0x1F) * 2,
        )


@dataclass
class ZipArchive:
    data: bytes
    entries: list[ZipEntry] = field(default_factory=list)
    cd_offset: int = 0
    cd_size: int = 0
    eocd_offset: int = 0
    comment: bytes = b""
    zip64: bool = False
    trailing_bytes: int = 0
    usable: bool = True

    def by_name(self, name: str) -> Optional[ZipEntry]:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def read(self, entry: ZipEntry) -> bytes:
        if entry.usize == 0 and entry.csize == 0:
            return b""
        off = entry.local_offset
        if off + 30 > len(self.data) or self.data[off:off + 4] != LFH_SIG:
            return b""
        nlen, elen = struct.unpack_from("<HH", self.data, off + 26)
        start = off + 30 + nlen + elen
        blob = self.data[start:start + entry.csize]
        if entry.method == 0:
            return blob
        if entry.method == 8:
            try:
                return zlib.decompress(blob, -15)
            except zlib.error:
                return b""
        return b""

    def text(self, name: str) -> Optional[str]:
        e = self.by_name(name)
        if e is None:
            return None
        raw = self.read(e)
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("utf-8", "replace")


def _find_eocd(data: bytes) -> int:
    start = max(0, len(data) - (MAX_COMMENT + 22))
    i = len(data) - 22
    while i >= start:
        if data[i:i + 4] == EOCD_SIG:
            return i
        i -= 1
    return -1


def read_zip(data: bytes, bag: Bag, *, verify_crc: bool = True) -> ZipArchive:
    ar = ZipArchive(data=data)
    eocd = _find_eocd(data)
    if eocd < 0:
        bag.error("M3.13", "找不到 ZIP 中央目录结尾记录（EOCD）",
                  part="(容器)", ref=NormRef("M3.13", OPC, "9.2.8"),
                  notes=["文件不是可用的 ZIP 归档，或尾部被截断/覆盖。"],
                  helps=["确认文件是否完整下载 / 完整拷贝；必要时重新获取原文件。"],
                  repair_trigger=True)
        ar.usable = False
        return ar
    ar.eocd_offset = eocd
    (_sig, _d1, _d2, _n_this, total_cd, cd_size, cd_off, clen) = \
        struct.unpack_from("<IHHHHIIH", data, eocd)
    ar.cd_size, ar.cd_offset = cd_size, cd_off
    ar.comment = data[eocd + 22:eocd + 22 + clen]
    ar.trailing_bytes = len(data) - (eocd + 22 + clen)
    if ar.trailing_bytes > 0:
        bag.note("M3.13", f"EOCD 之后还有 {ar.trailing_bytes} 字节附加数据",
                 part="(容器)", ref=NormRef("M3.13", OPC, "9.2.8"),
                 notes=["可能是覆盖写、附加水印或流式写入残留。"])
    if ar.comment:
        bag.note("M3.13", f"ZIP 归档带 {len(ar.comment)} 字节注释", part="(容器)",
                 ref=NormRef("M3.13", OPC, "9.2.8"))

    if total_cd == 0xFFFF or cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        ar.zip64 = True
        loc = data.rfind(EOCD64_LOC_SIG, max(0, eocd - 1024), eocd)
        if loc >= 0:
            e64 = struct.unpack_from("<Q", data, loc + 8)[0]
            if data[e64:e64 + 4] == EOCD64_SIG:
                total_cd = struct.unpack_from("<Q", data, e64 + 32)[0]
                cd_size = struct.unpack_from("<Q", data, e64 + 40)[0]
                cd_off = struct.unpack_from("<Q", data, e64 + 48)[0]
                ar.cd_size, ar.cd_offset = cd_size, cd_off

    # ---- 中央目录 ----
    if cd_off + cd_size > len(data):
        bag.error("M3.14", "中央目录偏移/长度超出文件范围",
                  part="(容器)", ref=NormRef("M3.14", OPC, "Annex C"),
                  notes=[f"cd_offset={cd_off} cd_size={cd_size} file_len={len(data)}"],
                  helps=["文件很可能被截断或覆盖写。"], repair_trigger=True)
        ar.usable = False
        return ar

    off = cd_off
    idx = 0
    while off + 46 <= cd_off + cd_size:
        if data[off:off + 4] != CDFH_SIG:
            bag.warning("M3.14", "中央目录中出现无法识别的记录，解析提前结束",
                        part="(容器)", ref=NormRef("M3.14", OPC, "Annex C"),
                        notes=[f"偏移 {off}，期望签名 PK\\x01\\x02。"])
            break
        (_sig, _vmade, _vneed, flags, method, mtime, mdate, crc, csize, usize,
         nlen, elen, clen2, _diskno, _iattr, _eattr, lho) = \
            struct.unpack_from("<IHHHHHHIIIHHHHHII", data, off)
        name_raw = data[off + 46:off + 46 + nlen]
        dec = "utf-8" if (flags & FLAG_UTF8) else "cp437"
        try:
            name = name_raw.decode(dec)
        except UnicodeDecodeError:
            name = name_raw.decode("utf-8", "replace")
        e = ZipEntry(name=name, name_raw=name_raw, flags=flags, method=method, crc=crc,
                     csize=csize, usize=usize, dos_time=mtime, dos_date=mdate,
                     local_offset=lho, extra_len=elen, comment_len=clen2, index=idx)
        if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF or lho == 0xFFFFFFFF:
            bag.warning("M3.21", f"条目 {name!r} 使用了 ZIP64 扩展字段但高位标记可疑",
                        part=name, ref=NormRef("M3.21", OPC, "Annex C"))
        ar.entries.append(e)
        off += 46 + nlen + elen + clen2
        idx += 1

    if idx != total_cd:
        bag.warning("M3.14", f"中央目录声明 {total_cd} 个条目，实际解析出 {idx} 个",
                    part="(容器)", ref=NormRef("M3.14", OPC, "Annex C"))
        ar.usable = False

    _check_entries(ar, bag, verify_crc=verify_crc)
    return ar


def _check_entries(ar: ZipArchive, bag: Bag, *, verify_crc: bool) -> None:
    seen: dict[str, ZipEntry] = {}
    seen_ci: dict[str, ZipEntry] = {}

    for e in ar.entries:
        # ---- M3.2 条目名文法 ----
        raw = e.name_raw
        if not raw:
            bag.error("M3.2", "ZIP 条目名为空", part="(容器)",
                      ref=NormRef("M3.2", OPC, "9.2.2"), repair_trigger=True)
        if b"\\" in raw:
            bag.error("M3.2", f"ZIP 条目名含反斜杠：{e.name!r}", part=e.name,
                      ref=NormRef("M3.2", OPC, "9.2.2"),
                      notes=["OPC 要求条目名为 ZIP 归档文件名文法（正斜杠分隔）。"],
                      helps=["把条目名中的 \\ 改为 /。"], repair_trigger=True)
        if e.name.startswith("/"):
            bag.error("M3.2", f"ZIP 条目名以 / 开头：{e.name!r}", part=e.name,
                      ref=NormRef("M3.2", OPC, "9.2.2"), repair_trigger=True)
        if any(ord(c) < 0x20 for c in e.name):
            bag.error("M3.2", f"ZIP 条目名含控制字符：{e.name!r}", part=e.name,
                      ref=NormRef("M3.2", OPC, "9.2.2"), repair_trigger=True)
        if any(ord(c) > 0x7E for c in e.name):
            bag.warning("M3.2", f"ZIP 条目名含非 ASCII 字符：{e.name!r}", part=e.name,
                        ref=NormRef("M3.2", OPC, "9.2.2"),
                        notes=["M3.2 要求条目名为 ASCII 字符串。",
                               "某些作者工具会写入中文文件名，Office 一般能容忍。"])
        if len(raw) + e.extra_len + e.comment_len > 65535:
            bag.warning("M3.8", "条目名 + Extra + Comment 合计超过 65535 字节", part=e.name,
                        ref=NormRef("M3.8", OPC, "9.2.5"))

        # ---- M3.3 唯一性 ----
        if e.name in seen:
            bag.error("M3.3", f"ZIP 内出现重复条目名：{e.name!r}", part=e.name,
                      ref=NormRef("M3.3", OPC, "9.2.2"),
                      notes=[f"重复出现于条目 #{seen[e.name].index} 与 #{e.index}。",
                             "读取方拿到哪一个是不确定的，这是整包重写失败的典型痕迹。"],
                      helps=["重新用 PowerPoint 另存为一次，或用 repair --level low 重建。"],
                      repair_trigger=True)
        seen.setdefault(e.name, e)
        low = e.name.lower()
        if low in seen_ci and seen_ci[low].name != e.name:
            bag.error("M1.12", f"部件名大小写不敏感等价：{e.name!r} 与 {seen_ci[low].name!r}",
                      part=e.name, ref=NormRef("M1.12", OPC, "8.1.1.2"),
                      repair_trigger=True)
        seen_ci.setdefault(low, e)

        # ---- M3.9 加密 ----
        if e.encrypted:
            bag.error("M3.9", f"条目被加密：{e.name!r}", part=e.name,
                      ref=NormRef("M3.9", OPC, "9.2.5"),
                      notes=["OPC 明确禁止 ZIP 加密。"], repair_trigger=True)

        # ---- 压缩方法 ----
        if e.method not in ALLOWED_METHODS:
            bag.error("M3.13", f"使用了 OPC 未允许的压缩方法 {e.method}：{e.name!r}",
                      part=e.name, ref=NormRef("M3.13", OPC, "9.2.8"),
                      notes=["OPC 只允许 stored(0) 与 deflated(8)。"], repair_trigger=True)

        # ---- M3.14 本地头一致性 ----
        off = e.local_offset
        e.lfh_present = off + 30 <= len(ar.data) and ar.data[off:off + 4] == LFH_SIG
        if not e.lfh_present:
            bag.error("M3.14", f"本地文件头缺失或签名错误：{e.name!r}", part=e.name,
                      ref=NormRef("M3.14", OPC, "Annex C"),
                      notes=[f"local_header_offset={off}"], repair_trigger=True)
        else:
            (_lsig, _lver, lf, lfl, lft, lfd, lcrc, lcs, lus, lnl, lel) = \
                struct.unpack_from("<IHHHHHIIIHH", ar.data, off)
            e.lfh_flags, e.lfh_method, e.lfh_crc = lf, lfl, lcrc
            e.lfh_csize, e.lfh_usize = lcs, lus
            e.lfh_name_ok = ar.data[off + 30:off + 30 + lnl] == e.name_raw
            if not e.lfh_name_ok:
                bag.error("M3.14", f"本地文件头与中央目录的条目名不一致：{e.name!r}",
                          part=e.name, ref=NormRef("M3.14", OPC, "Annex C"),
                          repair_trigger=True)
            if not e.has_descriptor:
                if (lcrc != e.crc) or (lcs != e.csize) or (lus != e.usize):
                    bag.warning("M3.14", f"本地文件头与中央目录的 size/crc 字段不一致：{e.name!r}",
                                part=e.name, ref=NormRef("M3.14", OPC, "Annex C"),
                                notes=[f"local crc/size=({lcrc:#x},{lcs},{lus}) "
                                       f"central crc/size=({e.crc:#x},{e.csize},{e.usize})"],
                                helps=["用 PowerPoint 另存为一次可让容器重新规范化。"])
            if e.has_descriptor:
                bag.note("M3.14", f"条目使用了 Data Descriptor：{e.name!r}", part=e.name,
                         ref=NormRef("M3.14", OPC, "Annex C"),
                         notes=["M3.14 要求 Data Descriptor 与中央目录字段一致。"])

        # ---- CRC 实测 ----
        if verify_crc and not e.is_dir and not e.encrypted and e.method in ALLOWED_METHODS:
            raw = ar.read(e)
            if len(raw) != e.usize and e.usize:
                e.crc_ok = False
                bag.error("M3.14", f"解压后长度与声明不符：{e.name!r}", part=e.name,
                          ref=NormRef("M3.14", OPC, "Annex C"),
                          notes=[f"声明 {e.usize} 字节，实得 {len(raw)} 字节。"],
                          helps=["条目数据已损坏；若这是媒体文件会表现为图片/音频打不开。"],
                          repair_trigger=True)
            elif zlib.crc32(raw) & 0xFFFFFFFF != e.crc:
                e.crc_ok = False
                bag.error("M3.14", f"CRC32 校验失败：{e.name!r}", part=e.name,
                          ref=NormRef("M3.14", OPC, "Annex C"),
                          notes=["条目内容与中央目录记录的 CRC 不符，数据已损坏。"],
                          repair_trigger=True)
            else:
                e.crc_ok = True
