#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
# 门禁覆盖：graphics/tools/direct/dsh/batch/Build.cs graphics/tools/direct/tests/t_dsh_activity.py tests/dsh_activity_test.sh
# 真实 Ticker 阶段协议与生命周期；不运行 Metal、不下载依赖。
set -eu
H=$(cd "$(dirname "$0")/.." && pwd)
exec "${S2A_PY:-python3}" "$H/graphics/tools/direct/tests/t_dsh_activity.py"
