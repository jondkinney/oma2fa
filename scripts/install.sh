#!/usr/bin/env bash

set -euo pipefail

PLUGIN_ID="io.github.jondkinney.oma2fa"
BINDING="SUPER + ALT + V"
BINDING_NORMALIZED="SUPERALTV"
BIND_BEGIN="-- BEGIN OMA2FA MANAGED BINDING"
BIND_END="-- END OMA2FA MANAGED BINDING"
MARKER_NAME=".oma2fa-managed"
ASSUME_YES=0
INSTALL_BINDING=1

fail() {
  echo "oma2fa install: $*" >&2
  exit 1
}

warn() {
  echo "oma2fa install: warning: $*" >&2
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/install.sh [--yes] [--no-bind]

Validate and copy this checkout into the per-user Omarchy plugin directory,
enable it, and add SUPER+ALT+V when that key is currently unused.

Options:
  --yes, -y   Confirm non-interactively.
  --no-bind   Install and enable the plugin without editing Hyprland bindings.
  --help, -h  Show this help.
USAGE
}

interactive() {
  [[ -t 0 && -t 1 ]]
}

confirm() {
  local prompt="$1" reply
  (( ASSUME_YES )) && return 0
  interactive || fail "confirmation required; rerun with --yes"
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" || "$reply" == "yes" || "$reply" == "YES" ]]
}

resolve_script_dir() {
  local source_path="${BASH_SOURCE[0]}"
  local source_dir link_target

  while [[ -L "$source_path" ]]; do
    source_dir=$(cd -P -- "$(dirname -- "$source_path")" && pwd)
    link_target=$(readlink -- "$source_path")
    if [[ "$link_target" == /* ]]; then
      source_path="$link_target"
    else
      source_path="$source_dir/$link_target"
    fi
  done

  cd -P -- "$(dirname -- "$source_path")" && pwd
}

unique_path() {
  local base="$1" candidate="$1" suffix=1
  while [[ -e "$candidate" || -L "$candidate" ]]; do
    candidate="${base}-${suffix}"
    suffix=$((suffix + 1))
  done
  printf '%s\n' "$candidate"
}

is_ignored_name() {
  case "$1" in
    .git | .venv | __pycache__ | .pytest_cache | .mypy_cache | .ruff_cache | .env | .env.* | .coverage | .DS_Store | *.pyc | *.pyo)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

copy_tree_without_links() {
  local source="$1" destination="$2" path relative mode part ignored
  local -a parts=()

  if [[ -L "$source" ]]; then
    fail "refusing to copy symlink: $source"
  fi

  while IFS= read -r -d '' path; do
    relative="${path#"$source"}"
    ignored=0
    IFS='/' read -r -a parts <<<"${relative#/}"
    for part in "${parts[@]}"; do
      if is_ignored_name "$part"; then
        ignored=1
        break
      fi
    done
    (( ignored )) && continue

    if [[ -L "$path" ]]; then
      fail "symlinks are not permitted in the installed plugin: $path"
    elif [[ -d "$path" ]]; then
      install -d -m 0755 -- "$destination$relative"
    elif [[ -f "$path" ]]; then
      mode=0644
      [[ -x "$path" ]] && mode=0755
      install -D -m "$mode" -- "$path" "$destination$relative"
    fi
  done < <(find "$source" -mindepth 0 -print0)
}

copy_root_file() {
  local source="$1" destination="$2" mode=0644
  [[ -L "$source" ]] && fail "refusing to copy symlink: $source"
  [[ -x "$source" ]] && mode=0755
  install -m "$mode" -- "$source" "$destination/$(basename -- "$source")"
}

restore_plugin_after_failure() {
  local failed_path

  # If the one-time upgrade already inserted the new bar widget, remove it
  # while the shell still has the new manifest loaded. Otherwise restoring the
  # old files first can leave a service/overlay-only plugin stranded in a bar
  # layout entry.
  if (( legacy_location_removed && new_plugin_enable_succeeded )); then
    omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1 ||
      warn "could not remove the failed new bar-widget location before rollback"
  fi

  if [[ -d "$target" && -f "$target/$MARKER_NAME" ]]; then
    failed_path=$(unique_path "$plugins_dir/.${PLUGIN_ID}.failed.$timestamp")
    mv -- "$target" "$failed_path"
    warn "the failed install was preserved at $failed_path"
  fi
  if [[ -n "$previous_backup" && -d "$previous_backup" ]]; then
    mv -- "$previous_backup" "$target"
    warn "restored the previously installed plugin"
  fi
  if (( legacy_location_removed )); then
    if rescan_and_wait_for_plugin &&
      omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1 &&
      omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1 &&
      wait_for_plugin_enabled; then
      warn "restored and re-enabled the previously installed plugin"
    else
      warn "restored the previous plugin files, but could not re-enable them automatically"
    fi
  else
    omarchy-shell -q shell rescanPlugins >/dev/null 2>&1 || true
  fi
}

rescan_and_wait_for_plugin() {
  local plugin_list expected_kinds attempt

  expected_kinds=$(jq -c '.kinds' "$target/manifest.json" 2>/dev/null) || return 1

  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || return 1
  for attempt in {1..50}; do
    if plugin_list=$(omarchy-shell shell listPlugins 2>/dev/null) &&
      jq -e --arg id "$PLUGIN_ID" --argjson expected "$expected_kinds" '
        type == "array" and any(.[];
          .id == $id
          and (.kinds | type == "array")
          and (($expected - .kinds) | length == 0)
        )
      ' <<<"$plugin_list" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

wait_for_plugin_enabled() {
  local plugin_list attempt

  for attempt in {1..30}; do
    if plugin_list=$(omarchy-shell shell listPlugins 2>/dev/null) &&
      jq -e --arg id "$PLUGIN_ID" '
        type == "array" and any(.[]; .id == $id and .enabled == true)
      ' <<<"$plugin_list" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

install_binding_if_free() {
  local begin_count=0 end_count=0 bindings_output line key_text normalized conflict=""
  local bindings_dir backup_file tmp_file config_errors original_exists=0
  local managed_binding=0 match_count=0

  (( INSTALL_BINDING )) || {
    echo "Skipped Hyprland binding (--no-bind)."
    return 0
  }

  [[ ! -L "$bindings_file" ]] ||
    fail "refusing to edit symlinked bindings file: $bindings_file (use --no-bind and edit its target manually)"

  if [[ -f "$bindings_file" ]]; then
    begin_count=$(grep -Fxc -- "$BIND_BEGIN" "$bindings_file" || true)
    end_count=$(grep -Fxc -- "$BIND_END" "$bindings_file" || true)
  fi
  if (( begin_count == 1 && end_count == 1 )); then
    managed_binding=1
  fi
  if ! (( (begin_count == 0 && end_count == 0) ||
    (begin_count == 1 && end_count == 1) )); then
    fail "incomplete or duplicate Oma2FA marker block in $bindings_file; repair it manually"
  fi

  if ! bindings_output=$(omarchy menu keybindings --print 2>/dev/null); then
    if (( managed_binding )); then
      warn "could not inspect current Omarchy keybindings; left the existing managed block unchanged"
    else
      warn "could not inspect current Omarchy keybindings; skipped $BINDING"
    fi
    return 0
  fi
  if [[ -z "$bindings_output" ]]; then
    if (( managed_binding )); then
      warn "Omarchy returned no current keybindings; left the existing managed block unchanged"
    else
      warn "Omarchy returned no current keybindings; skipped $BINDING"
    fi
    return 0
  fi

  while IFS= read -r line; do
    [[ "$line" == *"→"* ]] || continue
    key_text="${line%%→*}"
    normalized=$(printf '%s' "$key_text" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]+')
    if [[ "$normalized" == "$BINDING_NORMALIZED" ]]; then
      match_count=$((match_count + 1))
      conflict="$line"
    fi
  done <<<"$bindings_output"

  if (( managed_binding )); then
    if (( match_count == 0 )); then
      warn "the managed $BINDING block exists but is not active in Hyprland"
    elif (( match_count > 1 )); then
      warn "$BINDING appears $match_count times; one is managed by Oma2FA, but another binding may conflict"
    else
      echo "$BINDING binding is already managed by Oma2FA."
    fi
    return 0
  fi

  if [[ -n "$conflict" ]]; then
    warn "$BINDING is already in use; left bindings unchanged"
    warn "current binding: $conflict"
    echo "Open Oma2FA manually with: omarchy-shell shell toggle $PLUGIN_ID '{}'"
    return 0
  fi

  command -v hyprctl >/dev/null 2>&1 || {
    warn "hyprctl is unavailable, so the binding could not be safely validated; skipped $BINDING"
    return 0
  }

  bindings_dir=$(dirname -- "$bindings_file")
  if [[ -e "$bindings_dir" || -L "$bindings_dir" ]]; then
    [[ -d "$bindings_dir" ]] || fail "Hyprland config parent is not a directory: $bindings_dir"
  else
    install -d -m 0755 -- "$bindings_dir"
  fi
  backup_file=$(unique_path "$bindings_file.oma2fa.bak.$timestamp")
  tmp_file=$(mktemp "$bindings_dir/.bindings.lua.oma2fa.XXXXXX")
  if [[ -f "$bindings_file" ]]; then
    original_exists=1
    cp -a -- "$bindings_file" "$backup_file"
    cp -a -- "$bindings_file" "$tmp_file"
  else
    : >"$tmp_file"
  fi

  {
    printf '\n%s\n' "$BIND_BEGIN"
    printf '%s\n' 'o.bind("SUPER + ALT + V", "2FA codes", "omarchy-shell shell toggle io.github.jondkinney.oma2fa '\''{}'\''")'
    printf '%s\n' "$BIND_END"
  } >>"$tmp_file"
  mv -- "$tmp_file" "$bindings_file"

  if ! hyprctl reload >/dev/null 2>&1; then
    warn "Hyprland reload failed; restoring the prior bindings file"
    if (( original_exists )); then
      cp -a -- "$backup_file" "$bindings_file"
    else
      rm -f -- "$bindings_file"
    fi
    hyprctl reload >/dev/null 2>&1 || true
    return 1
  fi

  if ! config_errors=$(hyprctl configerrors 2>&1) || [[ -n "$config_errors" ]]; then
    warn "Hyprland rejected the binding; restoring the prior bindings file"
    [[ -n "$config_errors" ]] && warn "$config_errors"
    if (( original_exists )); then
      cp -a -- "$backup_file" "$bindings_file"
    else
      rm -f -- "$bindings_file"
    fi
    hyprctl reload >/dev/null 2>&1 || true
    return 1
  fi

  if (( original_exists )); then
    echo "Added $BINDING. Backup: $backup_file"
  else
    echo "Added $BINDING in new file: $bindings_file"
  fi
}

while (( $# > 0 )); do
  case "$1" in
    --yes | -y)
      ASSUME_YES=1
      ;;
    --no-bind)
      INSTALL_BINDING=0
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
  shift
done

for command_name in omarchy omarchy-shell jq find install wl-copy wtype timeout; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command not found: $command_name"
done

python_bin="${OMA2FA_PYTHON:-python3}"
command -v -- "$python_bin" >/dev/null 2>&1 || fail "Python interpreter not found: $python_bin"
"$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' ||
  fail "Oma2FA requires Python 3.12 or newer: $python_bin"

script_dir=$(resolve_script_dir)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
config_home="${XDG_CONFIG_HOME:-${HOME:?HOME is not set}/.config}"
[[ "$config_home" == /* ]] || fail "XDG_CONFIG_HOME must be an absolute path"
plugins_dir="$config_home/omarchy/plugins"
target="$plugins_dir/$PLUGIN_ID"
bindings_file="$config_home/hypr/bindings.lua"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
stage="$plugins_dir/.${PLUGIN_ID}.install.$$"
previous_backup=""
stage_created=0
legacy_location_needs_migration=0
legacy_location_removed=0
new_plugin_enable_succeeded=0

if (( INSTALL_BINDING )) && [[ -L "$bindings_file" ]]; then
  fail "refusing to edit symlinked bindings file: $bindings_file (use --no-bind and edit its target manually)"
fi

[[ -f "$repo_root/manifest.json" ]] || fail "manifest.json not found at repository root: $repo_root"
[[ -f "$repo_root/bin/oma2fa-bridge" ]] || fail "bin/oma2fa-bridge is missing"
[[ -f "$repo_root/oma2fa/cli.py" ]] || fail "oma2fa/cli.py is missing"

manifest_id=$(jq -r '.id // empty' "$repo_root/manifest.json")
[[ "$manifest_id" == "$PLUGIN_ID" ]] || fail "expected manifest id '$PLUGIN_ID', found '${manifest_id:-<empty>}'"

if [[ -e "$target" || -L "$target" ]]; then
  [[ ! -L "$target" && -d "$target" ]] || fail "refusing to replace non-directory or symlink: $target"
  [[ -f "$target/$MARKER_NAME" && ! -L "$target/$MARKER_NAME" ]] ||
    fail "refusing to replace unowned plugin directory: $target"
  grep -Fqx 'managed-by=oma2fa-install-v1' "$target/$MARKER_NAME" ||
    fail "refusing to replace plugin with an unrecognized ownership marker: $target"
  # Before Oma2FA shipped a bar widget, enabling this multi-kind plugin put
  # its id in shell.json's plugins[] array. Omarchy quite correctly regards
  # that as already enabled, but it therefore will not add the newly declared
  # widget to the bar. Remove that legacy location once, after the new plugin
  # has loaded, then enable it again so the manifest's defaultSection applies.
  # Subsequent installs see bar-widget here and leave the user's placement
  # untouched.
  if ! jq -e '.kinds | type == "array" and index("bar-widget") != null' \
    "$target/manifest.json" >/dev/null 2>&1; then
    legacy_location_needs_migration=1
  fi
fi

cat >&2 <<WARNING

Oma2FA is an unsandboxed Omarchy shell plugin. Installing it will copy code to:
  $target
and enable it in the running Omarchy shell.

WARNING
if (( INSTALL_BINDING )); then
  cat >&2 <<WARNING
If SUPER+ALT+V is free, this will also append one clearly marked binding block to:
  $bindings_file

Use --no-bind to leave Hyprland configuration unchanged.

WARNING
fi
confirm "Install Oma2FA from $repo_root?" || fail "aborted"

if [[ -e "$plugins_dir" || -L "$plugins_dir" ]]; then
  [[ -d "$plugins_dir" ]] || fail "Omarchy plugin parent is not a directory: $plugins_dir"
else
  install -d -m 0755 -- "$plugins_dir"
fi
if [[ -e "$stage" || -L "$stage" ]]; then
  fail "staging path unexpectedly exists: $stage"
fi
install -d -m 0755 -- "$stage"
stage_created=1
cleanup_stage() {
  (( stage_created )) || return 0
  case "$stage" in
    "$plugins_dir"/."$PLUGIN_ID".install.*)
      [[ -d "$stage" && ! -L "$stage" ]] && rm -rf -- "$stage"
      ;;
    *)
      warn "refusing to clean unexpected staging path: $stage"
      ;;
  esac
}
trap cleanup_stage EXIT

root_files=(manifest.json pyproject.toml README.md LICENSE preview.png)
for root_name in "${root_files[@]}"; do
  [[ -f "$repo_root/$root_name" ]] && copy_root_file "$repo_root/$root_name" "$stage"
done
shopt -s nullglob
for root_qml in "$repo_root"/*.qml; do
  copy_root_file "$root_qml" "$stage"
done
shopt -u nullglob
[[ -f "$repo_root/qmldir" ]] && copy_root_file "$repo_root/qmldir" "$stage"

for source_dir_name in oma2fa bin systemd docs ui assets; do
  [[ -d "$repo_root/$source_dir_name" ]] || continue
  copy_tree_without_links "$repo_root/$source_dir_name" "$stage/$source_dir_name"
done
install -d -m 0755 -- "$stage/scripts"
for lifecycle_script in install.sh uninstall.sh; do
  copy_root_file "$repo_root/scripts/$lifecycle_script" "$stage/scripts"
done

cat >"$stage/$MARKER_NAME" <<MARKER
managed-by=oma2fa-install-v1
plugin-id=$PLUGIN_ID
installed-at=$timestamp
MARKER
chmod 0644 -- "$stage/$MARKER_NAME"

if ! omarchy plugin validate "$stage"; then
  fail "staged plugin did not pass Omarchy validation"
fi

if [[ -d "$target" ]]; then
  previous_backup=$(unique_path "$plugins_dir/.${PLUGIN_ID}.previous.$timestamp")
  mv -- "$target" "$previous_backup"
fi
if ! mv -- "$stage" "$target"; then
  if [[ -n "$previous_backup" && -d "$previous_backup" &&
    ! -e "$target" && ! -L "$target" ]]; then
    mv -- "$previous_backup" "$target" ||
      warn "could not restore the previous plugin at $target"
  fi
  fail "could not move the staged plugin into $target"
fi
stage_created=0
trap - EXIT

if ! rescan_and_wait_for_plugin; then
  restore_plugin_after_failure
  fail "the Omarchy shell could not load and enable $PLUGIN_ID"
fi

if (( legacy_location_needs_migration )); then
  if ! omarchy plugin disable "$PLUGIN_ID"; then
    restore_plugin_after_failure
    fail "the Omarchy shell could not migrate $PLUGIN_ID to its bar widget"
  fi
  legacy_location_removed=1
fi

if ! omarchy plugin enable "$PLUGIN_ID"; then
  restore_plugin_after_failure
  fail "the Omarchy shell could not load and enable $PLUGIN_ID"
fi
new_plugin_enable_succeeded=1
if ! wait_for_plugin_enabled; then
  restore_plugin_after_failure
  fail "the Omarchy shell could not load and enable $PLUGIN_ID"
fi

echo "Installed and enabled $PLUGIN_ID at $target"
[[ -n "$previous_backup" ]] && echo "Previous managed install preserved at: $previous_backup"

if ! install_binding_if_free; then
  warn "plugin installation succeeded, but the hotkey was not installed"
  exit 1
fi

echo "Oma2FA is ready. Open it with $BINDING."
