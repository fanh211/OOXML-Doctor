# AGENT.md

> 面向在本仓库上继续工作的 **AI 编码代理**（以及接手的人类）。
> 读这一份就够上手；细节去 `README.md` / `使用用法.md`。

---

## 1. 这是什么

`ooxml-doctor` —— **PPTX 损坏诊断与修复**的命令行工具。
规范基线：**ECMA-376 5th edition / ISO/IEC 29500:2016**。

三条命令是全部门面：

```
ooxml-doctor scan   <pptx>                 # 诊断（Rust 编译器风格报告）
ooxml-doctor repair <pptx> --level low     # 最小修复：语义不变，保证能放映
ooxml-doctor repair <pptx> --level high    # 激进修复：多做不伤文件的处置，自动备份
```

辅助：`verify`（等价性）、`selftest`（打包后自检）、`licenses`（许可声明）、`catalog`（编辑器集成）。

---

## 2. 四层架构（改动前先定位到层）

| 层 | 文件 | 职责 | 规范依据 |
|---|---|---|---|
| L0 | `zipraw.py` | 自研 ZIP 中央目录解析（**不用 `zipfile`**，它太宽容） | OPC 主题 3 `M3.x` |
| L1 | `opc.py` | 部件名文法 / 内容类型 / 关系图 | OPC 主题 1 `M1.x` |
| L2 | `xmlmodel.py` | 良构性 / 编码 / DTD / MCE 检出 + **带字节偏移的 DOM** | `M1.17 M1.18 M1.19` |
| L3 | `schema.py` | **内置离线 XSD** 校验 + MCE 最小预处理 | `M1.20` + Part 5 |
| L4 | `rules.py` | 附加语法约束 + 来源/安全指纹 | Part 4 专章 / 非规范推断 |
| — | `repair.py` | 两级修复引擎（字节区间编辑） | |
| — | `report.py` `oracle.py` `cli.py` | 渲染 / 验证靶场 / 入口 | |

**诊断码的唯一出处是代码里的 `NormRef`**（`opc.py` / `rules.py`），
条款号取自 OPC Annex H（`M1.x`/`M3.x`…）与 Part 4「Additional Syntax Constraints」（`P4-ASC`）。

---

## 3. 红线（违反即返工）

这些是从 6 个真实 bug 里换来的，不要重新踩：

1. **诊断码必须用规范条款号。** OPC Annex H 的 `M1.13` / `M3.3` 等，不要自造 `OPC001`。
   非规范推断必须 `normative=False` 且 `confidence < 1`。
   单测 `test_every_diagnostic_has_code_and_severity` 会断言"声称会触发修复的诊断必须挂 `ref`"。

2. **修复永不原地改文件。** 只写新文件 + `manifest.json`；`high` 先自动备份。

3. **修不了就明确返回"需要人工介入"**（退出码 `4`），**不许硬修**。
   见 `_SAFE_DETACH_ELEMENTS` 白名单与 `plan.skipped`。

4. **编辑必须表达在"原始字节"坐标系里，并从后往前应用。**
   - 移动元素 = **删除原位置 + 在目标位置插入** 两个点编辑。
     *不要*重建整段子元素区域 —— 区域编辑会把区域内的其它编辑判为重叠而丢掉
     （曾经导致 `high` 修不掉 `Target="NULL"` 的音频链接）。
   - 计算插入点时注意**相邻兄弟**：判定"锚点在 node 之后"必须用 `>=` 而不是 `>`
     （`anchor.start == node.end` 是相邻，用 `>` 会让编辑退化成空操作）。

5. **不要把 `p:cNvPr` 当成 `a:cNvPr`。** PresentationML 的形状是 `p:` 命名空间；
   写错会让整条规则变成静默死代码（曾让"形状 id 唯一"和"媒体类型错配"两条检查全失效）。

6. **同一个判断只写一处。** "外部目标是否不可用"收敛在 `opc.is_bad_external()`。
   曾经在三处各写一份，只有一份漏了大写盘符 `D:\`。

7. **XSD 报错的严重性要分级。**
   结构性（元素不该出现 / 缺子元素）→ `error` + `repair_trigger`；
   取值性（pattern / enum / facet）→ `warning`，**不算触发项**。
   校准依据：`slideMaster2` 的 `<a:buSzPct val="80000"/>` 在 **PowerPoint 官方修复版里依然存在**。

8. **解析异常必须兜住 `ValueError`。** expat 遇到 `encoding="gb2312"` 会抛
   `multi-byte encodings are not supported`，只捕 `ExpatError` 会让整个工具崩。

9. **XSD 必须能离线用，且要能从 zip 里读。** `schema_dir()` 用 `importlib.resources`
   把资源解到临时目录 —— zipapp 形态下 `Path(__file__)` 读不到包内文件。
   改 `schemas/` 相关代码后**必须** `ooxml-doctor selftest`。

10. **许可证文件不能丢。** `LICENSE`（GPLv3，仅覆盖本项目源码）、`THIRD-PARTY-NOTICES.md`、
    `src/ooxml_doctor/schemas/README.md`、`src/ooxml_doctor/NOTICE.txt`（内嵌进包）。
    打包时 `NOTICE.txt` 必须进 `datas`，否则单文件分发的 `licenses` 读不到 = 不合规。

---

## 4. 怎么改（工作流）

```bash
cd /home/trixie/ooxml-doctor
export PYTHONPATH=src

python3 tests/test_smoke.py                       # 13 项，纯合成夹具，秒级，改完必跑
python3 -m ooxml_doctor selftest                  # 确认内置 XSD 可用且真的在判对错
python3 -m ooxml_doctor scan 样本.pptx                        # 真实样本冒烟
python3 tests/adversarial.py --seed 42            # 改规则/修复后必跑（约 80s）
```

**新增一条规则必须同时交付**：
1. 规则本体（挂规范 `NormRef`）
2. 正样本 + 负样本（干净包不能误报）
3. 若可修：一个 `fixer` + 期望档位（`fix_level`）
4. 在 `tests/adversarial.py` 里加一个变异算子（含命中断言）

**禁止**：只加检测不加单测；只加 fixer 不改 `adversarial.py` 的期望；
用正则做通用修复（正则只适合已知的、固定模式的少量替换）。

---

## 5. 变异测试台的坑（它自己也会错）

`tests/adversarial.py` 是红队工具，**它的 bug 会把工具冤枉成有 bug**。已修的：

- 用"诊断码是否存在"判定 → 被基线同码污染。必须用 **(码, 部件, 消息) 实例级差集**。
- 差集基准用原文件 → re-zip 的时间戳噪声污染。基准也要用重打包包，且**固定条目时间戳**。
- 变异算子写错（正则命中嵌套元素 / 把开标签重复 / 把文件前缀拼进去）→ 得出"工具修不动"的假结论。
  每个算子都要 `assert` 命中。
- 渲染等价性拿"已被搞崩的变异体"当基准 → 判定无意义。变异体不可渲染时判 **`None`（无法判定）**。

---

## 6. 平台边界

**Windows 上只需要 `scan` + `repair`，不依赖任何外部程序。**

`verify --render` 需要 LibreOffice + poppler，仅开发期用；缺失时降级为"无法判定"，
**不要**把它改成硬失败。验证用 `tests/run_validation.sh` 与 `tests/adversarial.py` 现场复现。

PyInstaller **不能交叉编译**：Windows 的 exe 只能在 Windows 上构建
（`build/build_windows.bat`）。

---

## 7. 合规

- 源码 **GPLv3**（`LICENSE`，`GPL-3.0-only`）。改许可需同步改 `pyproject.toml` 与全部文档。
- `src/ooxml_doctor/schemas/*.xsd` 是 **ISO/IEC 29500-4:2016** 标准文件，
  **版权 ISO/IEC + Ecma International，不属于 GPLv3，是"仅被聚合"的独立作品**。
  本项目不对其再分发权利作任何声明。
- 需要时：`build/build.py exe --no-schemas`、`--schemas DIR`、`OOXML_DOCTOR_SCHEMAS`。
- 真实课件**不入库**（第三方版权）；测试夹具全部由 `tests/synth.py` 合成。
- 本仓库是 **AIGC 项目**，见 `README.md` 的 AIGC 声明。
- **分发二进制（exe/pyz）时必须同时提供完整对应源码**（GPLv3 §6）。
- 修改版本必须显著标注修改（GPLv3 §5a），且不得追加额外限制。
- **不要引入与非 GPL 兼容的依赖**（现有 lxml BSD-3、libxml2 MIT、Python PSF 都兼容）。

---

## 8. 代码风格

- Python 3.10+，无第三方依赖（除 `lxml`）。**不要引入新的运行时依赖。**
- 中文注释/文档字符串，诊断消息面向教师用中文。
- 数据类优先；函数保持短小；`diagnostics.py` 是唯一诊断构造函数收口。
- 不要用 `print` 做日志（CLI 例外）；不要吞异常。
