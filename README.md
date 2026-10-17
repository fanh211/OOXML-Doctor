# ooxml-doctor

**PPTX 损坏诊断与修复工具**（CLI）。
按国际标准实现：**ECMA-376 5th edition / ISO/IEC 29500:2016**。
**完全离线** —— XSD 随包内置（12 个 schema，412 KB），运行时不访问网络。

> ## 🤖 AIGC 声明
>
> **本项目是 AIGC（AI 生成内容）项目。** 全部源码、测试、文档与报告均由
> **AI 编码代理（DeepSeek Harness，`deepseek-flash` 模型驱动）**在人类指令下生成。
>
> | 项 | 由谁决定 |
> |---|---|
> | 需求、范围（只做 PPTX）、验收标准、合规判断与最终采用 | **人类** |
> | 架构设计、代码、测试、文档、报告撰写 | **AI 代理** |
> | 是否满足验收：13 项单元测试 + 归档的变异测试/等价性验证 | **机器可复现** |
>
> **这对使用者意味着什么：**
>
> - 工具的行为**不靠承诺，靠可复现的测试**。所有能力声明都能在 `tests/` 里找到证据。
> - AIGC 产出**可能仍有错误**。软件按 MIT「AS IS」提供，不附带任何担保；
>   用于正式场合前请自行复核。
> - 开发过程中的 6 个真实 bug（含 2 个会让规则静默失效、1 个会**删掉整张幻灯片**）
>   的成因与修复要点已浓缩进 [`AGENT.md`](AGENT.md) 的「红线」一节。
> - 若你所在地区/机构对 AIGC 有标识或备案要求（如生成式 AI 服务管理、
>   AI 生成内容标识），**请保留本声明**并在分发时一并说明。

```
ooxml-doctor scan   文件.pptx               # ① 扫描：Rust 风格的人类可读报告
ooxml-doctor repair 文件.pptx --level low   # ② 最小修复：保证能放映
ooxml-doctor repair 文件.pptx --level high  # ③ 激进修复：会先自动备份
ooxml-doctor verify 修复前.pptx 修复后.pptx  # 验证修复没改坏东西
```

---

## 1. 设计要点：诊断码直接用规范条款号

ECMA-376 Part 2 (OPC) 的 Annex H 是合规条款总表，规范原文明确写道：

> 每条合规要求有唯一 ID，由字母（**M** 强制 / **S** 建议 / **O** 可选）+ 主题编号 + 序号组成……
> **生产者和消费者可以用这些 ID 报告错误。**

所以本工具**不自造错误码**，而是直接用规范编号汇报：

| 诊断码 | 含义 |
|---|---|
| `M1.13` | §8.1.2 内容类型须符合 RFC 2616 §3.7 的媒体类型语法 |
| `M1.12` | §8.1.1.2 部件名等价性按大小写不敏感 ASCII 比较 |
| `M1.17 / M1.18` | §8.1.4 XML 编码仅限 UTF-8/UTF-16；不得使用 DTD |
| `M1.19` | §8.1.4 含 MCE 的 XML **必须**先按 Part 5 预处理再校验 |
| `M1.20` | §8.1.4 XML 须对相应 XSD 有效（用**内置离线 XSD** 实现） |
| `M1.26 / M1.28 / M1.29` | §8.3.3 Relationship 的 Id 类型/Target 是 URI reference/内部目标解析 |
| `M3.2 / M3.3 / M3.9 / M3.14` | §9.2 ZIP 条目名文法/唯一性/禁止加密/中央目录与本地头一致 |
| `P4-ASC:…` | Part 4 **"Additional Syntax Constraints"** 专章（规范自认 XSD 表达不了、但属于一致性要求） |
| `APP-*` / `PROV-*` | **非规范**推断（注册表常识、来源指纹），一律标 `confidence < 1` |

条款号取自 OPC Annex H 与 Part 4「Additional Syntax Constraints」；
诊断码与条款的对照关系直接写在 `src/ooxml_doctor/rules.py` / `opc.py` 的 `NormRef` 里。

---

## 2. 安装与运行

```bash
cd ooxml-doctor
pip install -e .            # 或直接：PYTHONPATH=src python3 -m ooxml_doctor ...
ooxml-doctor scan 课件.pptx
```

依赖只有 `lxml`（libxml2 绑定）。

---

## 3. 三个子命令

### `scan` —— 扫描确认错误

Rust 编译器风格：`error[CODE]: 摘要` / `--> 位置` / 代码窗口 + 插入符 / `= note:` `= help:` `= spec:`。
报告末尾给一段**人类结论**：会不会弹"需要修复"、修完会丢什么、下一步该敲什么。

```
error[M1.20]: 元素 <p:custDataLst> 出现在 <p:extLst> 之后，违反内容模型顺序
  --> ppt/slides/slide7.xml:1:1
   |
1 | …"main" r:embed="rId8"/></p:ext></p:extLst><p:custDataLst><p:tags r:id="rId9"/>…
   |                                             ^^^^^^^^^^^^^^^ 应为 <p:custDataLst> 在 <p:extLst> 之前
   |
  = note: 父元素 <p:nvPr> 的规定顺序（XSD sequence）：
  = note:   ph → (audioCd | audioFile | …) → custDataLst → extLst
  = help: 把 <p:custDataLst> 移到 <p:extLst> 之前（repair --level low 会自动做）
  = spec: ECMA-376 Part 2 (OPC) M1.20 §8.1.4
```

选项：`--format json`、`--no-color`、`-v/--verbose`（展开来源指纹）、`--fast`（跳过 CRC 实测）。
退出码：`0` 健康，`3` 会被 Office 要求修复（可直接用于 CI 门禁）。

### `repair --level low` —— 最小修复，保证能放映

只做**语义不变**的规范化：

| 修什么 | 依据 |
|---|---|
| 元素顺序对齐 XSD `sequence`（仅在父元素全部子元素都在内容模型内时整体重排） | M1.20 |
| 内容类型纠错（`image/.jpg` → `image/jpeg`、`audio/mp3` → `audio/mpeg`） | M1.13 |
| 删除 `r:id=""` 这类空关系引用 | P4-ASC |

**保证不碰**外部链接、媒体节点、图片音频数据。

### `repair --level high` —— 激进修复（有概率加剧损坏，会先自动备份）

在 low 之上额外做**会改变行为**的处置：

| 修什么 | 依据 |
|---|---|
| 断开指向坏目标的外部 `r:link`（保留 `r:embed` 嵌入副本） | M1.28 / APP-REL-* |
| 删除既无 `r:link` 也无 `r:embed` 的空壳 `a:audioFile` 等 | P4-ASC |
| 同一 `spid` 上重复的媒体节点只留一个（优先留 `p:audio`） | P4-ASC |
| 删除与实际媒体类型不符的媒体节点 | P4-ASC |
| 清理已不被引用且目标不可用的关系条目 | M1.28 |
| 丢弃重复的 ZIP 条目名 | M3.3 |

`--level high` 会先把原文件复制成 `<原名>.backup-<时间戳>`，并在结尾打印回退命令。

**安全边界**：`high` 只做"不影响文档内容"的处置。凡是**文档结构引用**上的悬空关系
（如 `<p:sldId>` / `<p:sldLayoutId>`）一律**不改**，列入"需要人工介入"。
重复形状 id 也只在**没有被 `spTgt` 引用**时才重编号。

### 诚实结论：修不了就说"需要人工介入"

修复后自动复检，存在无法自动处理的项时打印独立区块并**以退出码 4 结束**
（`0` 完全修好 / `3` 仅扫描且会触发修复 / `4` 需要人工介入）。
`manifest.json` 里带 `status`（`fixed` / `needs_human`）与 `skipped_needs_human[]`。
`scan` 报告也会单列"▶ 需要人工介入"，逐项给出「位置 + 怎么办」。

### 通用选项

`--dry-run`（只打印修复计划）、`-o/--output`、`--manifest repair.json`。
所有改动都是**字节区间替换**，输出新文件、原文件不动、清单可审计。

---

## 4. 修复后强制自检（本工具的护城河）

`repair` 结束后自动跑一遍等价性验证，并打印结果：

```
等价性自检（证明没有改坏原文）：
  ✔ text  381 个部件文本一致
  ✔ media  22 个媒体文件内容一致
  ✔ rel_graph  （已忽略按设计断开的外部链接）
```

`verify` 子命令提供四项检查：文本等价、媒体内容哈希多重集、关系图归一化同构
（忽略 rId 重编号）、可选 LibreOffice 渲染比对（页数 + 文本）。

---

## 5. 架构

```
src/ooxml_doctor/
├── zipraw.py      L0  ZIP 物理层（自研中央目录解析）        OPC 主题 3
├── opc.py         L1  包模型：部件名/内容类型/关系图        OPC 主题 1
├── xmlmodel.py    L2   XML 解析（字节偏移 DOM）+ 良构/编码/DTD/MCE 检出
├── schema.py      L3   ★ 内置离线 XSD 校验 + MCE 最小预处理   M1.19 / M1.20
├── rules.py       L4   附加语法约束 + 来源/安全指纹           Part 4 专章
├── repair.py      两级修复引擎（字节区间编辑 + 清单）
├── report.py      Rust 风格渲染 + JSON
├── oracle.py      验证靶场（文本/媒体/关系图/渲染 四项等价）
├── cli.py         命令行
└── schemas/       ★ 内置 12 个 ISO/IEC 29500-4:2016 XSD（412 KB，离线）
spec/
├── annex_h.json   OPC Annex H 合规条款（已转录主题 1、3）
└── asc_index.json Part 4 附加语法约束索引
tests/
├── synth.py       合成夹具（**不含**任何第三方课件）
└── test_smoke.py  8 项测试
```

### 已知边界（诚实声明）

- **内置离线 XSD**（`src/ooxml_doctor/schemas/`，ISO/IEC 29500-4:2016，412 KB）覆盖：
  `ppt/{slides,slideLayouts,slideMasters,notesSlides,notesMasters}/*`、`ppt/presentation.xml`、
  `presProps` / `viewProps` / `tableStyles` / `commentAuthors` / `tags/*` → `pml.xsd`；
  `ppt/theme/*` → `dml-main.xsd`；`docProps/app.xml`、`custom.xml` → 对应 shared schema。
  编译 10 ms，校验 16 张幻灯片 10 ms。
- **刻意不做 XSD 的部件**：`[Content_Types].xml`、`*.rels`、`docProps/core.xml` ——
  OPC 自身的 schema 极简单，而自研规则信息量更大，叠一层只增代码不增检出率。
- **MCE 只做最小预处理**：展开 `mc:AlternateContent` + 剥离 `mc:*` 属性，
  不实现 Part 5 完整预处理模型（真实课件中 MCE 出现率为 0）。
- **不做修复模拟器**（预测 PowerPoint 修复会丢什么）。不做 DOCX/XLSX。
- 不承诺"修完一定能在 Office 2016 打开"——没有官方 oracle 就不这么声称。

### 严重性分级（真实样本校准）

| XSD 违规 | 判定 | 依据 |
|---|---|---|
| 结构性（元素不该出现 / 缺必需子元素） | `error` ＋ 修复触发项 | PowerPoint 会弹"需要修复" |
| 取值性（pattern / enumeration / 类型） | `warning`，不算触发项 | `slideMaster2` 的 `<a:buSzPct val="80000"/>`（模式要求 25%–400%）在 **PowerPoint 官方修复版里依然存在** |


---

## 6. 实测结果

### 真实样本（一对"损坏 → PowerPoint 官方修复"的 16 页课件）

| | error | warning | 会触发修复 | 说明 |
|---|---|---|---|---|
| `origin`（损坏原件） | 12 | 5 | **是**（3 × `M1.20` 结构性） | WPS + Aspose 反复另存写坏 |
| `repaired`（官方修复版） | 9 | 5 | 否 | 官方修复留下了 7 条 P4-ASC + 2 条 NULL 链接 |
| **`--level low`** | **5** | 5 | **否** | 文本/媒体/关系图/渲染 四项等价 ✔ |
| **`--level high`** | **2** | 3 | **否** | 文本/媒体/渲染等价 ✔ |

**low** 修完：文本 / 媒体 / 关系图 / LibreOffice 渲染 **四项等价性全部通过**。
**high** 修完：文本 / 媒体 / 渲染等价，关系图差异恰好是"按设计断开的 2 条 QQ 缓存外部链接"。

**顺带发现（只有真 XSD 才抓得到）**：`ppt/slideMasters/slideMaster2.xml` 的
`<a:buSzPct val="80000"/>` 违反 XSD 取值模式（项目符号尺寸必须是 25%–400%）——
**PowerPoint 官方修复版里它依然存在**，因此正确分级是 `warning` 而不是 `error`。

> 对比很说明问题：**官方修复留下 9 条 error 却声称修好了；本工具 low 级就把触发项清零，并且能自证没改坏原文。**

### 合成夹具

`tests/test_smoke.py` 8 项全通过，含：
- 注入的 4 类缺陷全部检出
- 修复触发项精确等于 `{M1.20}`
- **干净包 0 error / 0 触发项**（零假阳性门禁）
- low 不产生 high 级改动；high 比 low 修掉更多且保持等价
- **取值类违规必须是 warning 且不算触发项**（用真实样本校准过的分界线）

---

## 7. 后续里程碑

| 阶段 | 内容 |
|---|---|
| **M0–M1** | L0–L4 全部实现 + 内置离线 XSD + `scan`/`repair low`/`repair high`/`verify` | ✅ 完成（约 2900 行，8 项测试） |
| **M5′** | PyInstaller 打成单文件 exe（课堂双击版） | 待做 |

**明确不做**：修复模拟器（原 M4）、SARIF/SBOM/CI 门禁、模糊测试语料、
DOCX/XLSX、Web UI、任何联网行为（XSD 已内置，运行时零网络）。

---

## 8. 平台与验证归档

| 功能 | 依赖外部程序 | Windows |
|---|---|---|
| `scan` / `repair` / `selftest` / `catalog` | 无 | ✔ |
| `verify`（文本 / 媒体 / 关系图 / 部件清单） | 无 | ✔ |
| `verify --render` | LibreOffice + poppler | 需自行安装；缺失时降级为“无法判定” |

**Windows 上只需要 `scan` + `repair`，完全不依赖外部程序。**
`--render` 只是开发期用来验证“修完还能真放映”；本仓库为保持精简不含验证归档，
需要时用 `tests/run_validation.sh` + `tests/adversarial.py` 现场复现。

## 9. 许可与合规

| 部分 | 许可 |
|---|---|
| 本工具源码 | **MIT** — [`LICENSE`](LICENSE) |
| `src/ooxml_doctor/schemas/*.xsd`（12 个 ISO/IEC 29500-4:2016 schema） | **不属于 MIT**；版权 ISO/IEC 与 Ecma International |
| 运行时依赖 | lxml (BSD-3) / libxml2 (MIT) / Python (PSF) |

本项目**不对**内置 schema 的再分发权利作任何声明。需要时：

```bash
python build/build.py exe --no-schemas        # 构建时排除内置 XSD
ooxml-doctor scan 课件.pptx --schemas DIR      # 运行时外挂自备副本
export OOXML_DOCTOR_SCHEMAS=DIR
```

排除后工具照常可用，仅关闭 `M1.20` 的 XSD 校验层（`selftest` 会明确报告“XSD 不可用”）。
完整说明见 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)。
测试夹具全部**合成**；真实课件仅作本地校准，不进入发行物。
