#!/usr/bin/env bash
# 对两档修复产物做完整验证（含 LibreOffice 渲染等价性）。
# 用法:  bash tests/run_validation.sh <origin.pptx> [输出目录]
#
# 注意：--render 需要 soffice + pdftotext/pdfinfo，属**开发期**验证；
#       Windows 上只做 scan/repair 完全用不到它。
set -u
ORIGIN="${1:-sample.pptx}"
OUT="${2:-./validation-out}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
export PYTHONPATH=src
mkdir -p "$OUT"

if [ ! -f "$ORIGIN" ]; then
  echo "找不到样本：$ORIGIN"
  echo "真实课件不随仓库分发；请用第一个参数指定一个损坏的 pptx。"
  exit 2
fi

echo "### 1/4 修复前扫描"
python3 -m ooxml_doctor scan "$ORIGIN" --no-color > "$OUT/report-00-origin.txt"
python3 -m ooxml_doctor scan "$ORIGIN" --format json > "$OUT/report-00-origin.json"

echo "### 2/4 两档修复"
python3 -m ooxml_doctor repair "$ORIGIN" --level low \
  -o "$OUT/origin_low.pptx" --manifest "$OUT/manifest-low.json" --no-color \
  > "$OUT/log-repair-low.txt"
python3 -m ooxml_doctor repair "$ORIGIN" --level high \
  -o "$OUT/origin_high.pptx" --manifest "$OUT/manifest-high.json" --no-color \
  > "$OUT/log-repair-high.txt"
rm -f "$ORIGIN".backup-*

echo "### 3/4 等价性验证（渲染项会调用 LibreOffice，较慢）"
python3 -m ooxml_doctor verify "$ORIGIN" "$OUT/origin_low.pptx" \
  --render > "$OUT/verify-low.txt" 2>&1
echo "  low  exit=$?"
python3 -m ooxml_doctor verify "$ORIGIN" "$OUT/origin_high.pptx" \
  --render --allow-external-rel-drop > "$OUT/verify-high.txt" 2>&1
echo "  high exit=$?"

echo "### 4/4 单元测试"
python3 tests/test_smoke.py > "$OUT/unit-tests.txt" 2>&1
echo "  exit=$?"
echo "DONE -> $OUT"
