#!/usr/bin/env bash

set -euo pipefail

fail() {
  echo "oma2fa picker status test: $*" >&2
  exit 1
}

command -v quickshell >/dev/null 2>&1 || fail "quickshell is required"

script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
shell_root="/usr/share/omarchy/shell"
[[ -d "$shell_root/Commons" && -d "$shell_root/Ui" ]] ||
  fail "Omarchy shell QML modules are unavailable"

runtime_dir=$(mktemp -d /tmp/oma2fa-picker-qml.XXXXXX)
cleanup() {
  case "$runtime_dir" in
    /tmp/oma2fa-picker-qml.*)
      [[ -d "$runtime_dir" && ! -L "$runtime_dir" ]] && rm -rf -- "$runtime_dir"
      ;;
    *)
      echo "oma2fa picker status test: refusing to clean unexpected path: $runtime_dir" >&2
      ;;
  esac
}
trap cleanup EXIT

install -m 0644 -- "$repo_root/tests/qml/picker_status_harness.qml" \
  "$runtime_dir/shell.qml"
install -m 0644 -- "$repo_root/Picker.qml" "$runtime_dir/Picker.qml"
ln -s -- "$shell_root/Commons" "$runtime_dir/Commons"
ln -s -- "$shell_root/Ui" "$runtime_dir/Ui"
ln -s -- "$repo_root/assets" "$runtime_dir/assets"

output_file="$runtime_dir/output.log"
if ! timeout 10s quickshell --no-color -p "$runtime_dir/shell.qml" \
    >"$output_file" 2>&1; then
  sed -n '1,240p' "$output_file" >&2
  fail "Quickshell harness failed"
fi

grep -Fq 'OMA2FA_PICKER_STATUS_PASS' "$output_file" || {
  sed -n '1,240p' "$output_file" >&2
  fail "Quickshell harness did not report success"
}

echo "Oma2FA picker status Quickshell harness passed."
