# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 ooxml-doctor contributors
"""ooxml-doctor — PPTX 损坏诊断与修复。

规范基线：
  ECMA-376 5th edition / ISO-IEC 29500:2016
    Part 2 (OPC)  Annex H  -> 合规条款编号 M/S/O（规范明确授权用于报错）
    Part 4        Annex A  -> XSD；另有 "Additional Syntax Constraints" 专章
    Part 5        MCE      -> 预处理模型（OPC M1.19 强制）
"""

__version__ = "0.1.0.dev0"
SPEC_NAME = "ECMA-376 5th edition / ISO/IEC 29500:2016"
