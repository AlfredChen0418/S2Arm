#!/bin/bash
# 门禁覆盖：launchers/shared/* ios/settings.m ios/l10n/*/S2ArmHost.strings runtime/include/s2a_direct_prompt.h runtime/include/s2a_init_fail.h runtime/include/s2a_selfcheck_page.h pack/jobs/errors.py pack/targets/ios/adapter.py pack/contracts/protocol.py launchers/macos/Sources/S2ArmLauncher/PackMode.swift launchers/macos/adapter/Sources/S2ArmAppleAdapter/Resources/Localizable.json
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
PY=${S2A_PYTHON:-${S2A_PY:-python3}}
exec "$PY" "$R/launchers/shared/check_experience.py" "$@"
