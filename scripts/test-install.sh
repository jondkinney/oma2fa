#!/usr/bin/env bash

# Isolated smoke test for install, managed reinstall, and uninstall. Every
# command that could reach the live Omarchy/Hyprland/systemd session is replaced
# in a temporary PATH.

set -euo pipefail

fail() {
  echo "installer smoke test: $*" >&2
  exit 1
}

resolve_script_dir() {
  cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
}

script_dir=$(resolve_script_dir)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
real_validator=$(command -v omarchy-plugin-validate || true)
[[ -n "$real_validator" ]] || fail "omarchy-plugin-validate is required"

test_root=$(mktemp -d)
cleanup() {
  case "$test_root" in
    /tmp/* | /var/tmp/*)
      rm -rf -- "$test_root"
      ;;
    *)
      echo "installer smoke test: refusing to clean unexpected path: $test_root" >&2
      ;;
  esac
}
trap cleanup EXIT

fake_bin="$test_root/bin"
test_home="$test_root/home"
test_config="$test_root/config"
mkdir -p -- "$fake_bin" "$test_home" "$test_config/hypr" "$test_config/omarchy/plugins"
chmod 0700 -- "$test_config/hypr" "$test_config/omarchy/plugins"

cat >"$fake_bin/omarchy" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "plugin validate")
    exec "$OMA2FA_TEST_VALIDATOR" "${3:?plugin folder required}"
    ;;
  "plugin enable")
    [[ -f "$OMA2FA_TEST_REGISTRY/known" ]] || {
      echo "mock plugin registry has not observed a rescan" >&2
      exit 1
    }
    id="${3:?plugin id required}"
    if [[ -f "$OMA2FA_TEST_REGISTRY/fail-enable-once" ]]; then
      rm -f -- "$OMA2FA_TEST_REGISTRY/fail-enable-once"
      printf 'enable-failed %s\n' "$id" >>"$OMA2FA_TEST_CALLS"
      exit 1
    fi
    target="$XDG_CONFIG_HOME/omarchy/plugins/$id"
    state="none"
    [[ -f "$OMA2FA_TEST_STATE" ]] && state=$(<"$OMA2FA_TEST_STATE")
    if [[ "$state" == "none" ]]; then
      kinds=$(<"$OMA2FA_TEST_REGISTRY/observed-kinds")
      if jq -e 'index("bar-widget") != null' <<<"$kinds" >/dev/null; then
        section=$(jq -r '.barWidget.defaultSection // "center"' "$target/manifest.json")
        printf 'bar:%s\n' "$section" >"$OMA2FA_TEST_STATE"
      else
        printf '%s\n' plugin >"$OMA2FA_TEST_STATE"
      fi
    fi
    printf 'enable %s\n' "$id" >>"$OMA2FA_TEST_CALLS"
    printf 'Enabled %s\n' "$id"
    ;;
  "plugin disable")
    id="${3:?plugin id required}"
    printf '%s\n' none >"$OMA2FA_TEST_STATE"
    printf 'disable %s\n' "$id" >>"$OMA2FA_TEST_CALLS"
    printf 'Disabled %s\n' "$id"
    ;;
  "plugin remove")
    id="${3:?plugin id required}"
    target="$XDG_CONFIG_HOME/omarchy/plugins/$id"
    backup="$XDG_CONFIG_HOME/omarchy/plugins/.${id}.bak.test"
    suffix=1
    while [[ -e "$backup" || -L "$backup" ]]; do
      backup="$XDG_CONFIG_HOME/omarchy/plugins/.${id}.bak.test-${suffix}"
      suffix=$((suffix + 1))
    done
    mv -- "$target" "$backup"
    printf '%s\n' none >"$OMA2FA_TEST_STATE"
    printf 'Removed %s. Backup at: %s\n' "$id" "$backup"
    ;;
  "menu keybindings")
    [[ "${3:-}" == "--print" ]] || exit 2
    printf '%s\n' 'SUPER + SPACE                       → Omarchy menu'
    bindings="$XDG_CONFIG_HOME/hypr/bindings.lua"
    if [[ -f "$bindings" ]] && grep -Fqx -- '-- BEGIN OMA2FA MANAGED BINDING' "$bindings"; then
      printf '%s\n' 'SUPER ALT + V                       → 2FA codes'
    fi
    ;;
  *)
    echo "unexpected mocked omarchy call: $*" >&2
    exit 2
    ;;
esac
MOCK

cat >"$fake_bin/omarchy-shell" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail

case "$*" in
  "shell rescanPlugins" | "-q shell rescanPlugins")
    rm -f -- "$OMA2FA_TEST_REGISTRY/known"
    printf '0\n' >"$OMA2FA_TEST_REGISTRY/polls"
    ;;
  "shell listPlugins")
    polls=$(<"$OMA2FA_TEST_REGISTRY/polls")
    polls=$((polls + 1))
    printf '%s\n' "$polls" >"$OMA2FA_TEST_REGISTRY/polls"
    if (( polls >= 2 )); then
      : >"$OMA2FA_TEST_REGISTRY/known"
      target="$XDG_CONFIG_HOME/omarchy/plugins/io.github.jondkinney.oma2fa"
      if [[ -f "$target/manifest.json" ]]; then
        kinds=$(jq -c '.kinds' "$target/manifest.json")
        if [[ -f "$OMA2FA_TEST_REGISTRY/stale-kinds-polls" ]]; then
          stale_polls=$(<"$OMA2FA_TEST_REGISTRY/stale-kinds-polls")
          if (( stale_polls > 0 )); then
            kinds='["service","overlay"]'
            printf '%s\n' "$((stale_polls - 1))" >"$OMA2FA_TEST_REGISTRY/stale-kinds-polls"
          fi
        fi
        printf '%s\n' "$kinds" >"$OMA2FA_TEST_REGISTRY/observed-kinds"
        state="none"
        [[ -f "$OMA2FA_TEST_STATE" ]] && state=$(<"$OMA2FA_TEST_STATE")
        if jq -e 'index("bar-widget") != null' <<<"$kinds" >/dev/null; then
          [[ "$state" == bar:* ]] && enabled=true || enabled=false
        else
          [[ "$state" == "plugin" ]] && enabled=true || enabled=false
        fi
        if [[ -f "$OMA2FA_TEST_REGISTRY/fail-bar-verification" ]] &&
          jq -e 'index("bar-widget") != null' <<<"$kinds" >/dev/null; then
          enabled=false
        fi
        jq -cn --arg id io.github.jondkinney.oma2fa --argjson kinds "$kinds" \
          --argjson enabled "$enabled" '[{id: $id, kinds: $kinds, enabled: $enabled}]'
      else
        printf '%s\n' '[]'
      fi
    else
      printf '%s\n' '[]'
    fi
    ;;
  *)
    echo "unexpected mocked omarchy-shell call: $*" >&2
    exit 2
    ;;
esac
MOCK

cat >"$fake_bin/hyprctl" <<'MOCK'
#!/usr/bin/env bash
case "${1:-}" in
  reload) printf '%s\n' ok ;;
  configerrors) : ;;
  *) exit 2 ;;
esac
MOCK

cat >"$fake_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  "--user is-active --quiet oma2fa-webhook.service" | \
    "--user is-enabled --quiet oma2fa-webhook.service")
    exit 1
    ;;
  *)
    echo "unexpected mocked systemctl call: $*" >&2
    exit 2
    ;;
esac
MOCK

chmod +x -- "$fake_bin/omarchy" "$fake_bin/omarchy-shell" "$fake_bin/hyprctl" \
  "$fake_bin/systemctl"

export HOME="$test_home"
export XDG_CONFIG_HOME="$test_config"
export OMA2FA_TEST_VALIDATOR="$real_validator"
export OMA2FA_TEST_REGISTRY="$test_root/registry"
export OMA2FA_TEST_STATE="$test_root/widget-state"
export OMA2FA_TEST_CALLS="$test_root/omarchy-calls"
export PATH="$fake_bin:$PATH"
mkdir -p -- "$OMA2FA_TEST_REGISTRY"
printf '%s\n' none >"$OMA2FA_TEST_STATE"
: >"$OMA2FA_TEST_CALLS"

"$repo_root/scripts/install.sh" --yes
[[ $(<"$OMA2FA_TEST_STATE") == "bar:right" ]] ||
  fail "fresh install did not place the widget in its default right section"
printf '%s\n' 'bar:left:2' >"$OMA2FA_TEST_STATE"
"$repo_root/scripts/install.sh" --yes
[[ $(<"$OMA2FA_TEST_STATE") == "bar:left:2" ]] ||
  fail "managed reinstall moved an already placed bar widget"
[[ $(grep -Fxc 'disable io.github.jondkinney.oma2fa' "$OMA2FA_TEST_CALLS" || true) == 0 ]] ||
  fail "managed reinstall unnecessarily disabled an existing bar widget"

target="$test_config/omarchy/plugins/io.github.jondkinney.oma2fa"
bindings="$test_config/hypr/bindings.lua"
[[ -f "$target/manifest.json" ]] || fail "plugin manifest was not installed"
[[ -f "$target/preview.png" ]] || fail "marketplace preview was not installed"
for shortcut_asset in shortcut-library.png shortcut-input.png shortcut-configuration.png shortcut-automation.png; do
  [[ -f "$target/assets/$shortcut_asset" && ! -L "$target/assets/$shortcut_asset" ]] ||
    fail "Shortcut walkthrough asset was not installed: $shortcut_asset"
done
[[ ! -e "$target/scripts/test-install.sh" ]] || fail "development tests leaked into install"
bar_widget_entrypoint=$(jq -er '.entryPoints.barWidget' "$target/manifest.json")
[[ "$bar_widget_entrypoint" == */* ]] ||
  fail "bar widget entry point is not isolated in a nested plugin directory"
[[ -f "$target/$bar_widget_entrypoint" && ! -L "$target/$bar_widget_entrypoint" ]] ||
  fail "nested bar widget entry point was not copied as a regular file"
[[ $(stat -c '%a' "$test_config/hypr") == 700 ]] ||
  fail "installer changed the existing Hyprland config directory mode"
[[ $(stat -c '%a' "$test_config/omarchy/plugins") == 700 ]] ||
  fail "installer changed the existing plugin directory mode"
grep -Fqx 'managed-by=oma2fa-install-v1' "$target/.oma2fa-managed" ||
  fail "ownership marker is missing"
[[ -z "$(find "$target" -type l -print -quit)" ]] || fail "installed plugin contains a symlink"
[[ $(grep -Fxc -- '-- BEGIN OMA2FA MANAGED BINDING' "$bindings") == 1 ]] ||
  fail "managed binding was duplicated during reinstall"
[[ $(grep -Fxc -- '-- END OMA2FA MANAGED BINDING' "$bindings") == 1 ]] ||
  fail "managed binding end marker was duplicated during reinstall"
find "$test_config/omarchy/plugins" -mindepth 1 -maxdepth 1 \
  -type d -name '.io.github.jondkinney.oma2fa.previous.*' -print -quit | grep -q . ||
  fail "reinstall did not preserve the previous managed copy"

"$repo_root/scripts/uninstall.sh" --yes
[[ ! -e "$target" && ! -L "$target" ]] || fail "uninstall left the managed plugin in place"
[[ $(grep -Fxc -- '-- BEGIN OMA2FA MANAGED BINDING' "$bindings" || true) == 0 ]] ||
  fail "uninstall left the managed binding in place"
find "$test_config/omarchy/plugins" -mindepth 1 -maxdepth 1 \
  -type d -name '.io.github.jondkinney.oma2fa.bak.test*' -print -quit | grep -q . ||
  fail "uninstall did not preserve a recoverable plugin backup"

# Upgrade from the original service/overlay-only manifest. Its enabled id is
# represented by a plugins[] entry; merely re-enabling after adding bar-widget
# would leave it there and never render the widget. The installer must remove
# that legacy location exactly once and let the new manifest place it.
install -d -m 0755 -- "$target"
cat >"$target/manifest.json" <<'LEGACY_MANIFEST'
{
  "schemaVersion": 1,
  "id": "io.github.jondkinney.oma2fa",
  "name": "Oma2FA",
  "version": "0.1.0",
  "kinds": ["service", "overlay"],
  "entryPoints": {"service": "Service.qml", "overlay": "Picker.qml"}
}
LEGACY_MANIFEST
cat >"$target/.oma2fa-managed" <<'LEGACY_MARKER'
managed-by=oma2fa-install-v1
plugin-id=io.github.jondkinney.oma2fa
installed-at=legacy-test
LEGACY_MARKER
printf '%s\n' plugin >"$OMA2FA_TEST_STATE"
: >"$OMA2FA_TEST_CALLS"

# Force post-enable verification to fail after the new widget has been
# inserted. Rollback must remove that bar location before restoring the old
# manifest, then recreate the old service/overlay plugins[] location.
: >"$OMA2FA_TEST_REGISTRY/fail-bar-verification"
if "$repo_root/scripts/install.sh" --yes >/dev/null 2>&1; then
  fail "legacy upgrade unexpectedly passed failed bar verification"
fi
rm -f -- "$OMA2FA_TEST_REGISTRY/fail-bar-verification"
jq -e '.kinds | index("bar-widget") == null' "$target/manifest.json" >/dev/null ||
  fail "failed legacy upgrade did not restore the old manifest"
[[ $(<"$OMA2FA_TEST_STATE") == "plugin" ]] ||
  fail "failed legacy upgrade did not restore the old plugins[] location"
expected_rollback_calls=$'disable io.github.jondkinney.oma2fa\nenable io.github.jondkinney.oma2fa\ndisable io.github.jondkinney.oma2fa\ndisable io.github.jondkinney.oma2fa\nenable io.github.jondkinney.oma2fa'
[[ $(<"$OMA2FA_TEST_CALLS") == "$expected_rollback_calls" ]] ||
  fail "failed legacy upgrade did not disable the new widget before restoring the old plugin"

: >"$OMA2FA_TEST_CALLS"
printf '%s\n' 3 >"$OMA2FA_TEST_REGISTRY/stale-kinds-polls"
"$repo_root/scripts/install.sh" --yes
[[ $(<"$OMA2FA_TEST_STATE") == "bar:right" ]] ||
  fail "legacy plugins[] entry was not migrated to the right bar section"
[[ $(<"$OMA2FA_TEST_REGISTRY/stale-kinds-polls") == 0 ]] ||
  fail "upgrade did not wait for the shell registry to expose bar-widget"
[[ $(grep -Fxc 'disable io.github.jondkinney.oma2fa' "$OMA2FA_TEST_CALLS" || true) == 1 ]] ||
  fail "legacy upgrade did not remove its old plugins[] entry exactly once"
[[ $(grep -Fxc 'enable io.github.jondkinney.oma2fa' "$OMA2FA_TEST_CALLS" || true) == 1 ]] ||
  fail "legacy upgrade did not re-enable the new bar widget exactly once"

"$repo_root/scripts/uninstall.sh" --yes
[[ ! -e "$target" && ! -L "$target" ]] ||
  fail "uninstall after legacy migration left the managed plugin in place"

symlink_target="$test_root/symlink-managed-bindings.lua"
printf '%s\n' '-- externally managed sentinel' >"$symlink_target"
rm -f -- "$bindings"
ln -s -- "$symlink_target" "$bindings"
if "$repo_root/scripts/install.sh" --yes >/dev/null 2>&1; then
  fail "installer accepted a symlinked bindings file"
fi
grep -Fqx -- '-- externally managed sentinel' "$symlink_target" ||
  fail "installer modified a symlink-managed bindings target"
[[ ! -e "$target" && ! -L "$target" ]] ||
  fail "installer changed plugin state before refusing a symlinked binding"

echo "Installer install/reinstall/uninstall smoke test passed."
