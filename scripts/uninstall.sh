#!/usr/bin/env bash

set -euo pipefail

PLUGIN_ID="io.github.jondkinney.oma2fa"
BIND_BEGIN="-- BEGIN OMA2FA MANAGED BINDING"
BIND_END="-- END OMA2FA MANAGED BINDING"
MARKER_NAME=".oma2fa-managed"
ASSUME_YES=0
REMOVE_PLUGIN=1
REMOVE_BINDING=1

fail() {
  echo "oma2fa uninstall: $*" >&2
  exit 1
}

warn() {
  echo "oma2fa uninstall: warning: $*" >&2
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/uninstall.sh [--yes] [--keep-plugin] [--keep-binding]

Remove only the plugin and marked keybinding created by scripts/install.sh.
Omarchy's plugin removal command preserves the non-git plugin as a hidden
backup so it can be recovered.

Options:
  --yes, -y       Confirm non-interactively.
  --keep-plugin   Remove the managed keybinding but leave the plugin installed.
  --keep-binding  Remove the managed plugin but leave the keybinding in place.
  --help, -h      Show this help.
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

unique_path() {
  local base="$1" candidate="$1" suffix=1
  while [[ -e "$candidate" || -L "$candidate" ]]; do
    candidate="${base}-${suffix}"
    suffix=$((suffix + 1))
  done
  printf '%s\n' "$candidate"
}

remove_managed_binding() {
  local begin_count=0 end_count=0 backup_file tmp_file config_errors

  [[ -f "$bindings_file" ]] || {
    echo "No Hyprland bindings file to change."
    return 0
  }

  begin_count=$(grep -Fxc -- "$BIND_BEGIN" "$bindings_file" || true)
  end_count=$(grep -Fxc -- "$BIND_END" "$bindings_file" || true)
  if (( begin_count == 0 && end_count == 0 )); then
    echo "No Oma2FA-managed binding found."
    return 0
  fi
  if (( begin_count != 1 || end_count != 1 )); then
    fail "refusing to edit incomplete or duplicate marker blocks in $bindings_file"
  fi

  command -v hyprctl >/dev/null 2>&1 ||
    fail "hyprctl is required to validate the Hyprland config change"

  backup_file=$(unique_path "$bindings_file.oma2fa.bak.$timestamp")
  tmp_file=$(mktemp "$(dirname -- "$bindings_file")/.bindings.lua.oma2fa.XXXXXX")
  cp -a -- "$bindings_file" "$backup_file"

  if ! awk -v begin="$BIND_BEGIN" -v end="$BIND_END" '
    $0 == begin {
      if (inside) exit 40
      inside = 1
      next
    }
    $0 == end {
      if (!inside) exit 41
      inside = 0
      next
    }
    !inside { print }
    END { if (inside) exit 42 }
  ' "$bindings_file" >"$tmp_file"; then
    rm -f -- "$tmp_file"
    fail "could not isolate the exact managed binding block; left the file unchanged"
  fi

  chmod --reference="$bindings_file" "$tmp_file"
  mv -- "$tmp_file" "$bindings_file"

  if ! hyprctl reload >/dev/null 2>&1; then
    cp -a -- "$backup_file" "$bindings_file"
    hyprctl reload >/dev/null 2>&1 || true
    fail "Hyprland reload failed; restored $bindings_file"
  fi
  if ! config_errors=$(hyprctl configerrors 2>&1) || [[ -n "$config_errors" ]]; then
    cp -a -- "$backup_file" "$bindings_file"
    hyprctl reload >/dev/null 2>&1 || true
    [[ -n "$config_errors" ]] && warn "$config_errors"
    fail "Hyprland reported config errors; restored $bindings_file"
  fi

  echo "Removed the Oma2FA binding. Backup: $backup_file"
}

while (( $# > 0 )); do
  case "$1" in
    --yes | -y)
      ASSUME_YES=1
      ;;
    --keep-plugin)
      REMOVE_PLUGIN=0
      ;;
    --keep-binding)
      REMOVE_BINDING=0
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

(( REMOVE_PLUGIN || REMOVE_BINDING )) || fail "--keep-plugin and --keep-binding leave nothing to do"

config_home="${XDG_CONFIG_HOME:-${HOME:?HOME is not set}/.config}"
[[ "$config_home" == /* ]] || fail "XDG_CONFIG_HOME must be an absolute path"
plugins_dir="$config_home/omarchy/plugins"
target="$plugins_dir/$PLUGIN_ID"
bindings_file="$config_home/hypr/bindings.lua"
webhook_unit_file="$config_home/systemd/user/oma2fa-webhook.service"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
plugin_present=0
binding_present=0
binding_begin_count=0
binding_end_count=0

if (( REMOVE_BINDING )) && [[ -L "$bindings_file" ]]; then
  fail "refusing to edit symlinked bindings file: $bindings_file (use --keep-binding and edit its target manually)"
fi

if (( REMOVE_PLUGIN )) && [[ -e "$target" || -L "$target" ]]; then
  [[ ! -L "$target" && -d "$target" ]] || fail "refusing to remove non-directory or symlink: $target"
  [[ -f "$target/$MARKER_NAME" && ! -L "$target/$MARKER_NAME" ]] ||
    fail "refusing to remove unowned plugin directory: $target"
  grep -Fqx 'managed-by=oma2fa-install-v1' "$target/$MARKER_NAME" ||
    fail "refusing to remove plugin with an unrecognized ownership marker: $target"
  [[ ! -e "$target/.git" && ! -L "$target/.git" ]] ||
    fail "refusing destructive removal after a .git entry appeared in the managed plugin: $target"
  plugin_present=1
fi

if (( REMOVE_PLUGIN )); then
  webhook_unit_found=0
  [[ -e "$webhook_unit_file" || -L "$webhook_unit_file" ]] && webhook_unit_found=1
  if command -v systemctl >/dev/null 2>&1 && {
    systemctl --user is-active --quiet oma2fa-webhook.service 2>/dev/null ||
      systemctl --user is-enabled --quiet oma2fa-webhook.service 2>/dev/null
  }; then
    webhook_unit_found=1
  fi
  if (( webhook_unit_found )); then
    fail "optional webhook unit remains; run 'systemctl --user disable --now oma2fa-webhook.service', remove $webhook_unit_file, run 'systemctl --user daemon-reload', then retry"
  fi
fi

if (( REMOVE_BINDING )) && [[ -f "$bindings_file" ]]; then
  binding_begin_count=$(grep -Fxc -- "$BIND_BEGIN" "$bindings_file" || true)
  binding_end_count=$(grep -Fxc -- "$BIND_END" "$bindings_file" || true)
  if (( binding_begin_count == 1 && binding_end_count == 1 )); then
    binding_present=1
  elif (( binding_begin_count != 0 || binding_end_count != 0 )); then
    fail "refusing to edit incomplete or duplicate marker blocks in $bindings_file"
  fi
fi

if (( ! plugin_present && ! binding_present )); then
  echo "No installer-managed Oma2FA files were found."
  exit 0
fi

cat >&2 <<SUMMARY

Oma2FA uninstall will change only installer-owned state:
  plugin:  $([[ $plugin_present == 1 ]] && printf '%s' "$target" || printf '%s' '<unchanged>')
  binding: $([[ $binding_present == 1 ]] && printf '%s' "$bindings_file" || printf '%s' '<unchanged>')

SUMMARY
confirm "Continue?" || fail "aborted"

if (( binding_present )); then
  remove_managed_binding
fi

if (( plugin_present )); then
  command -v omarchy >/dev/null 2>&1 || fail "required command not found: omarchy"
  omarchy plugin remove "$PLUGIN_ID" --yes
fi

echo "Oma2FA uninstall complete."
