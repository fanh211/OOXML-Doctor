# 第三方组件与许可说明（THIRD-PARTY NOTICES）

本文件说明 ooxml-doctor 里**不属于本项目 GPLv3 许可**的部分。请与 `LICENSE` 一并保留。

## 0. 本项目的许可与"聚合"关系

* **ooxml-doctor 自身的源码**：**GNU General Public License v3.0**（`GPL-3.0-only`），
  全文见 `LICENSE`。
* **`src/ooxml_doctor/schemas/*.xsd`（12 个 ISO/IEC 29500-4:2016 schema）**：
  **不是本项目的作品，也不受 GPLv3 覆盖。** 它们由 ISO/IEC 与 Ecma International 发布，
  本项目只是把它们与自己的代码**放在同一介质上分发（mere aggregation）**。
* 依 GPLv3 的机制，聚合在同一个分发介质上的独立作品**不会**被并入本程序的许可范围
  （GPLv3 §5 末段对"聚合"的说明）；反向亦然：本项目也无法把 GPLv3 的权利延伸到它们身上。
* **正因如此**，本项目提供 `--no-schemas` 构建与 `--schemas DIR` 运行参数：
  分发者若需要一份"许可上完全干净"的 GPLv3 产物，直接排除这些 schema 即可，
  工具功能不受影响（仅关闭 `M1.20` 的 XSD 校验层）。

### 分发 GPLv3 产物时的三项义务（提醒）

| 义务 | 依据 | 本项目怎么满足 |
|---|---|---|
| 分发二进制须同时提供**完整对应源码** | GPLv3 §6 | 源码即本仓库；分发 exe/pyz 时请附上或给出仓库地址 |
| 修改过的版本须**显著标注修改** | GPLv3 §5a | `AGENT.md` 要求维护者标注 |
| 不得对 GPLv3 授予的权利**追加额外限制** | GPLv3 §10 | 请勿在分发时附加 EULA 式限制 |

---

## 1. 内置 XML Schema（`src/ooxml_doctor/schemas/*.xsd`）—— **最重要**

### 是什么

12 个 XML Schema 文件，来自 **ISO/IEC 29500-4:2016**（等同 **ECMA-376 第 5 版 Part 4 Annex A**
的 Office Schemas）：

```
pml.xsd                                  PresentationML
dml-main.xsd / dml-chart.xsd / dml-chartDrawing.xsd / dml-diagram.xsd
dml-lockedCanvas.xsd / dml-picture.xsd   DrawingML
shared-commonSimpleTypes.xsd / shared-relationshipReference.xsd
shared-documentPropertiesExtended.xsd / shared-documentPropertiesCustom.xsd
shared-documentPropertiesVariantTypes.xsd  Shared
```

合计约 412 KB。它们让工具能在**完全离线**的情况下做 `M1.20`（对 XSD 有效）校验，
以及"元素顺序违反 schema"这类最关键的结构性检查。

### 版权

**Copyright (c) ISO/IEC and Ecma International. 保留所有权利。**

这些 schema 是国际标准文档的组成部分，**不受本项目的 GPLv3 许可覆盖**，
属于与本程序仅"聚合"的独立作品。
文件本身**不含**任何版权声明头（它们就是裸 schema），因此你必须依赖本文件来获知出处。

### 出处

* 标准：**ISO/IEC 29500-4:2016** / **ECMA-376 5th edition, Part 4**
* 本仓库内置副本的获取方式：从公开镜像的 `ISO-IEC29500-4_2016` schema 集按
  `pml.xsd` 的 `schemaLocation` 依赖闭包递归抓取（2026-10-17），未做任何修改。
* 官方发布渠道：<https://ecma-international.org/publications-and-standards/standards/ecma-376/>
  （ECMA-376 可免费下载；ISO/IEC 29500 通过 ISO 发行）

### ⚠ 再分发前请自行确认授权

本项目**不对**这些 schema 的再分发权利作出任何声明或保证。
不同使用场景风险不同：

| 场景 | 建议 |
|---|---|
| 个人/学校内部使用、不对外分发 | 通常风险很低 |
| 把 exe 分发给其他学校（含 schema） | **建议先核对 ECMA / ISO 的再分发条款** |
| 公开发布 / 商业分发 | **必须自行取得授权**，或使用下面的无 schema 构建 |

### 不想带上它们？两种做法

**做法 A（推荐给分发者）—— 构建时排除：**

```bash
python build/build.py exe --no-schemas
```

产物不含 XSD。工具**仍然可用**，只是：

- `M1.20` 的 XSD 校验层会关闭（抓不到"元素顺序违反 schema"这类问题）
- 其余所有检查（OPC 容器 / 内容类型 / 关系图 / ZIP / 附加语法约束 / 来源指纹）与全部修复功能**正常**
- `selftest` 会明确报告"XSD 不可用"，不会假装健康

**做法 B —— 运行时外挂自己的（已获授权的）副本：**

```bash
ooxml-doctor scan 课件.pptx --schemas /path/to/my/ISO-IEC29500-4_2016
# 或
export OOXML_DOCTOR_SCHEMAS=/path/to/my/ISO-IEC29500-4_2016
```

这样内置副本可以被删掉，工具改用你提供的目录。

---

## 2. 运行时依赖

| 依赖 | 许可 | 是否随包分发 |
|---|---|---|
| **lxml**（libxml2 / libxslt） | lxml: BSD-3-Clause；libxml2/libxslt: MIT | 单文件 exe **会**打包其二进制 |
| Python 标准库 | PSF License | 单文件 exe **会**打包运行时 |

单文件可执行（PyInstaller onefile）里包含上述组件的二进制副本。
对外分发该 exe 时，请一并保留 lxml 与 Python 的许可声明。

---

## 3. 可选的外部工具（**不随包分发**）

| 工具 | 用途 | 说明 |
|---|---|---|
| **LibreOffice**（`soffice`） | `verify --render` 的渲染 oracle | 仅**测试**用（`tests/run_validation.sh`）。工具不依赖它；Windows 上只做修复完全用不到 |
| **pdftotext / pdfinfo**（poppler） | 同上 | 同上 |
| **PyInstaller** | 构建单文件 exe | 仅构建期依赖 |

---

## 4. 测试夹具

`tests/` 下的夹具全部**合成**（`tests/synth.py` 现场生成），
**不包含**任何第三方课件或教材内容。

真实课件样本仅用于本地校准，**未**进入本仓库或任何发行物。
