# 内置 XML Schema —— 版权与出处

本目录 12 个 `.xsd` 文件来自 **ISO/IEC 29500-4:2016**（等同 **ECMA-376 5th edition Part 4 Annex A**）。

**Copyright (c) ISO/IEC and Ecma International. 保留所有权利。
这些文件不受本项目 MIT 许可覆盖。**

它们是**裸 schema**（文件内不含版权声明头），所以本文件就是你需要的出处说明。
完整说明见仓库根目录的 `THIRD-PARTY-NOTICES.md`。

## 再分发

本项目不对其再分发权利作任何声明。
若需对外分发，请：

```bash
python build/build.py exe --no-schemas        # 构建时排除
```

或运行时指向你自备的副本：

```bash
ooxml-doctor scan 课件.pptx --schemas /path/to/ISO-IEC29500-4_2016
export OOXML_DOCTOR_SCHEMAS=/path/to/ISO-IEC29500-4_2016
```

排除后工具仍可正常运行，只是 `M1.20` 的 XSD 校验层关闭（`selftest` 会明确报告）。
