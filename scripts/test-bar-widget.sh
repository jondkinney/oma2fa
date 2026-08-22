#!/usr/bin/env bash

# Static contract and privacy checks for the bar entry point. Runtime QML
# behavior is covered by the Quickshell mock harness used during development;
# these checks keep accidental bridge duplication or secret exposure from
# slipping into a later edit.

set -euo pipefail

fail() {
  echo "bar widget test: $*" >&2
  exit 1
}

script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -P -- "$script_dir/.." && pwd)
manifest="$repo_root/manifest.json"

command -v jq >/dev/null 2>&1 || fail "jq is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

jq -e '
  .id == "io.github.oma2fa"
  and (.kinds | index("bar-widget") != null)
  and (.entryPoints.barWidget | type == "string" and length > 0)
  and .barWidget.allowMultiple == false
  and .barWidget.defaultSection == "right"
' "$manifest" >/dev/null || fail "manifest bar-widget contract is invalid"

widget_entrypoint=$(jq -er '.entryPoints.barWidget' "$manifest")
case "/$widget_entrypoint/" in
  //* | */../* | */./*) fail "manifest has an unsafe bar-widget entry point" ;;
esac
widget="$repo_root/$widget_entrypoint"
[[ -f "$widget" && ! -L "$widget" ]] ||
  fail "$widget_entrypoint is missing or symlinked"

python3 - "$widget" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

if len(re.findall(r"\bserviceFor\s*\(", text)) != 1:
    raise SystemExit("bar widget must retrieve exactly one shell-owned service")
if not re.search(r"serviceFor\s*\(\s*root\.moduleName\s*\)", text):
    raise SystemExit("bar widget must retrieve the service by its plugin id")
if len(re.findall(r"\bhost\.toggle\s*\(", text)) != 1:
    raise SystemExit("bar widget click path must contain exactly one overlay toggle")

forbidden_runtime = (
    "Process", "StdioCollector", "SplitParser", "bridgePath", "oma2fa-bridge",
    ".activate(", ".deleteRecord(", ".clearLocal(", ".sendRequest(",
    "wl-copy", "wtype",
)
for token in forbidden_runtime:
    if token in text:
        raise SystemExit(f"bar widget contains forbidden runtime capability: {token}")

# The widget may observe only aggregate count and coarse health from the
# shared service. In particular, it must never read a record, label, id, code,
# timestamp, backend error, or backend message.
allowed_service_properties = {"records", "bridgeAlive", "ready", "status"}
for match in re.finditer(
    r"(?:root\.)?omaService\.([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?", text
):
    first, second = match.groups()
    if first not in allowed_service_properties:
        raise SystemExit(f"bar widget reads forbidden service property: {first}")
    if first == "records" and second not in {None, "length"}:
        raise SystemExit(f"bar widget reads record content via records.{second}")
    if first != "records" and second is not None:
        raise SystemExit(f"bar widget reads unapproved nested state: {first}.{second}")

for field in ("code", "service", "source", "id", "received_at", "expires_at",
              "lastError", "message", "error"):
    if re.search(rf"\.{re.escape(field)}\b", text):
        raise SystemExit(f"bar widget references sensitive field: {field}")
PY

echo "Bar widget manifest, shared-service, click-only, and privacy checks passed."
