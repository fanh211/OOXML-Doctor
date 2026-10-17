# 打包说明

## 为什么 Windows 的 exe 要在这台 Windows 机器上打

PyInstaller **不支持交叉编译**：在 Linux 上跑只能得到 Linux 可执行文件，
在 Windows 上跑才能得到 `.exe`。所以要给教室大屏用的单文件 exe，
必须在一台 Windows 上执行 `build_windows.bat`。

## 三种分发形态

| 形态 | 构建命令 | 目标机要求 | 体积 | 适用 |
|---|---|---|---|---|
| **单文件 exe** | `build_windows.bat`（Windows） | 无 | ~20–30 MB | **教室 U 盘，首选** |
| `.pyz` | `python3 build/build.py zipapp` | 有 Python 3.10+ 与 lxml | ~0.5 MB | 备课机/CI |
| `pip install -e .` | — | 有 Python | — | 开发 |

## 内置 XSD 必须一起打进去

`src/ooxml_doctor/schemas/*.xsd` 是运行时由 `schema.py` 通过 `__file__` 相对路径读取的。
`ooxml-doctor.spec` 里已经把它列进 `datas`：

```python
datas = [(os.path.join(SRC, "ooxml_doctor", "schemas"), "ooxml_doctor/schemas")]
```

漏了这一步的话，exe 一跑就会报"内置 XSD 不可用"，XSD 校验（也就是抓
"元素顺序违反 schema"那层）会整层失效。打完包请务必用 `--selftest` 复验。

## 打完包怎么验

```bat
dist\ooxml-doctor.exe --selftest
dist\ooxml-doctor.exe scan 课件.pptx
```
