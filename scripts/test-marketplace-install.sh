#!/usr/bin/env bash

# Exercise Omarchy's real Git-based plugin add/remove commands without touching
# the live shell or user configuration. Shell registry calls are replaced with
# small test doubles; clone, validation, placement, and deletion remain real.

set -euo pipefail

PLUGIN_ID="io.github.jondkinney.oma2fa"

fail() {
  echo "marketplace install test: $*" >&2
  exit 1
}

resolve_script_dir() {
  cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
}

script_dir=$(resolve_script_dir)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
real_add=$(command -v omarchy-plugin-add || true)
real_remove=$(command -v omarchy-plugin-remove || true)
real_validator=$(command -v omarchy-plugin-validate || true)
[[ -n "$real_add" && -n "$real_remove" && -n "$real_validator" ]] ||
  fail "current Omarchy plugin lifecycle commands are required"
git -C "$repo_root" rev-parse --verify HEAD >/dev/null 2>&1 ||
  fail "the repository needs a commit before its Git install path can be tested"

test_root=$(mktemp -d)
cleanup() {
  case "$test_root" in
    /tmp/* | /var/tmp/*)
      rm -rf -- "$test_root"
      ;;
    *)
      echo "marketplace install test: refusing to clean unexpected path: $test_root" >&2
      ;;
  esac
}
trap cleanup EXIT

fake_bin="$test_root/bin"
test_home="$test_root/home"
state="$test_root/enabled"
mkdir -p -- "$fake_bin" "$test_home/.config/omarchy/plugins"

cat >"$fake_bin/omarchy-plugin-catalog" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' '[]'
MOCK

cat >"$fake_bin/omarchy-plugin-list" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
target="$HOME/.config/omarchy/plugins/io.github.jondkinney.oma2fa"
if [[ -f "$target/manifest.json" ]]; then
  enabled=false
  [[ -f "$OMA2FA_MARKETPLACE_TEST_STATE" ]] && enabled=true
  jq -cn --argjson enabled "$enabled" \
    '[{id:"io.github.jondkinney.oma2fa",enabled:$enabled}]'
else
  printf '%s\n' '[]'
fi
MOCK

cat >"$fake_bin/omarchy-plugin-enable" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "io.github.jondkinney.oma2fa" ]] || exit 2
: >"$OMA2FA_MARKETPLACE_TEST_STATE"
printf 'Enabled %s\n' "$1"
MOCK

cat >"$fake_bin/omarchy-shell" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "shell rescanPlugins")
    ;;
  "shell listPlugins")
    enabled=false
    [[ -f "$OMA2FA_MARKETPLACE_TEST_STATE" ]] && enabled=true
    jq -cn --argjson enabled "$enabled" \
      '[{id:"io.github.jondkinney.oma2fa",enabled:$enabled}]'
    ;;
  "shell setPluginEnabled io.github.jondkinney.oma2fa false")
    rm -f -- "$OMA2FA_MARKETPLACE_TEST_STATE"
    ;;
  *)
    echo "unexpected shell call: $*" >&2
    exit 2
    ;;
esac
MOCK

ln -s -- "$real_validator" "$fake_bin/omarchy-plugin-validate"
chmod 0755 -- "$fake_bin/omarchy-plugin-catalog" "$fake_bin/omarchy-plugin-list" \
  "$fake_bin/omarchy-plugin-enable" "$fake_bin/omarchy-shell"

export HOME="$test_home"
export OMA2FA_MARKETPLACE_TEST_STATE="$state"
export PATH="$fake_bin:$PATH"

"$real_add" "$repo_root" --enable --yes

target="$test_home/.config/omarchy/plugins/$PLUGIN_ID"
[[ -d "$target/.git" ]] || fail "official add did not create a Git checkout"
[[ $(jq -r '.id' "$target/manifest.json") == "$PLUGIN_ID" ]] ||
  fail "installed manifest has the wrong plugin id"
[[ -f "$state" ]] || fail "official add did not enable the plugin"

"$real_remove" "$PLUGIN_ID" --yes

[[ ! -e "$target" && ! -L "$target" ]] || fail "official remove left the plugin checkout"
[[ ! -e "$state" ]] || fail "official remove left the plugin enabled"

echo "Marketplace Git add/enable/remove smoke test passed."
