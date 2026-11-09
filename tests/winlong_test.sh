#!/bin/bash
# Windows 长路径单测：bash tests/winlong_test.sh
cd "$(dirname "$0")/.." || exit 1
exec "${S2A_PY:-python3}" -B tests/test_winlong.py
