#!/usr/bin/env bash
# Chạy trước mỗi lần commit. Ba bước: syntax -> import -> pytest.
# Dừng ngay khi bước nào đỏ, in rõ file nào lỗi.
set -u
PY=./env/bin/python
FAIL=0

echo "== 1/3 syntax check =="
while IFS= read -r -d '' f; do
    "$PY" -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" \
        || { echo "  SYNTAX FAIL: $f"; FAIL=1; }
done < <(find . -path ./env -prune -o -name '*.py' -print0)
[ "$FAIL" -eq 0 ] && echo "  OK"

echo "== 2/3 import check =="
for mod in node1_ingestion node2_core node3_audio node4_dashboard node4_timing node5_memory shared; do
    PYTHONPATH=. "$PY" -c "import $mod" 2>/tmp/import_err.txt \
        || { echo "  IMPORT FAIL: $mod"; cat /tmp/import_err.txt; FAIL=1; }
done
[ "$FAIL" -eq 0 ] && echo "  OK"

echo "== 3/3 pytest =="
PYTHONPATH=. "$PY" -m pytest tests/ -q || FAIL=1

echo "----------------------------------------"
if [ "$FAIL" -eq 0 ]; then
    echo "TẤT CẢ XANH"
else
    echo "CÓ LỖI — xem log ở trên trước khi commit"
fi
exit $FAIL