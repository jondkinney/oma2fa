#!/usr/bin/env bash

set -euo pipefail

fail() {
  echo "oma2fa qml test: $*" >&2
  exit 1
}

command -v quickshell >/dev/null 2>&1 || fail "quickshell is required"

script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
shell_root="/usr/share/omarchy/shell"
[[ -d "$shell_root/Commons" && -d "$shell_root/Ui" ]] ||
  fail "Omarchy shell QML modules are unavailable"

runtime_dir=$(mktemp -d /tmp/oma2fa-qml.XXXXXX)
harness_pid=""
cleanup() {
  if [[ -n "$harness_pid" ]] && kill -0 "$harness_pid" 2>/dev/null; then
    kill "$harness_pid" 2>/dev/null || true
    wait "$harness_pid" 2>/dev/null || true
  fi
  case "$runtime_dir" in
    /tmp/oma2fa-qml.*)
      [[ -d "$runtime_dir" && ! -L "$runtime_dir" ]] && rm -rf -- "$runtime_dir"
      ;;
    *)
      echo "oma2fa qml test: refusing to clean unexpected path: $runtime_dir" >&2
      ;;
  esac
}
trap cleanup EXIT

install -m 0644 -- "$repo_root/tests/qml/bar_widget_harness.qml" \
  "$runtime_dir/shell.qml"
install -m 0644 -- "$repo_root/tests/qml/RootProbe.qml" \
  "$runtime_dir/RootProbe.qml"
ln -s -- "$shell_root/Commons" "$runtime_dir/Commons"
ln -s -- "$shell_root/Ui" "$runtime_dir/Ui"

output_file="$runtime_dir/output.log"
OMA2FA_QML_WIDGET_PATH="$runtime_dir/ui/Oma2FABarWidget.qml" \
  timeout 10s quickshell --no-color -p "$runtime_dir/shell.qml" \
  >"$output_file" 2>&1 &
harness_pid=$!

ready=0
for _attempt in {1..100}; do
  if grep -Fq 'OMA2FA_QML_HARNESS_READY' "$output_file"; then
    ready=1
    break
  fi
  if ! kill -0 "$harness_pid" 2>/dev/null; then
    break
  fi
  sleep 0.05
done
if (( ! ready )); then
  sed -n '1,240p' "$output_file" >&2
  fail "Quickshell harness did not reach the hot-upgrade checkpoint"
fi

# Add the nested entry point only after Quickshell has cached the root QML
# directory, matching an upgrade from Oma2FA 0.1 while the shell remains up.
install -d -m 0755 -- "$runtime_dir/ui"
install -m 0644 -- "$repo_root/ui/Oma2FABarWidget.qml" \
  "$runtime_dir/ui/Oma2FABarWidget.qml"

if ! wait "$harness_pid"; then
  harness_pid=""
  sed -n '1,240p' "$output_file" >&2
  fail "Quickshell harness failed"
fi
harness_pid=""

grep -Fq 'OMA2FA_QML_HARNESS_PASS' "$output_file" || {
  sed -n '1,240p' "$output_file" >&2
  fail "Quickshell harness did not report success"
}

echo "Oma2FA bar widget Quickshell harness passed."
