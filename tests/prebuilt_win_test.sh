#!/bin/bash
# 门禁只看：pack/orch/oneclick_win.py pack/orch/stages.py pack/rewriter.py translator/spvc_inproc.py translator/dsh_batch.py graphics/tools/nxfmt.py graphics/tools/shbake/nxsh_extract.py graphics/widen/scan/ui_lyt.py launchers/windows/bundle_prepare_win.py tests/prebuilt_win_test.py
# Windows 启动器预编件（S2A_PREBUILT_TOOLS）各使用方：设了且预编件在 → 用预编件、不调编译器 / dotnet build；没设照旧现编。见 tests/prebuilt_win_test.py
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
PY=${S2A_PYTHON:-${S2A_PY:-python3}}
cd "$R" && PYTHONUTF8=1 PYTHONWARNINGS=ignore::ResourceWarning "$PY" tests/prebuilt_win_test.py
echo "✓ Windows 预编件使用方单测全过"
