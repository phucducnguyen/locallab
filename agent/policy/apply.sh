#!/usr/bin/env bash
# Lock the Lab Watch sandbox's network down to the bench and local inference.
# Run on the GB10 from the repo root:  agent/policy/apply.sh [sandbox] [--dry-run]
# Commands and keys come from the installed NemoClaw (v0.0.137) help and docs:
# "policy add --from-file", "policy remove <preset>", "policy exclude <key>".
# managed_inference (inference.local, the gateway's own route) cannot be excluded.
set -euo pipefail
SANDBOX="${1:-labwatch}"
DRY="${2:-}"
export PATH="$HOME/.local/bin:$PATH" NEMOCLAW_GATEWAY_PORT="${NEMOCLAW_GATEWAY_PORT:-8090}"
export NEMOCLAW_NON_INTERACTIVE=1
HERE="$(cd "$(dirname "$0")" && pwd)"
ACK="--yes"
[ "$DRY" = "--dry-run" ] && ACK="--dry-run"

# Keep a copy of the policy as it was, to restore by hand if needed.
nemoclaw "$SANDBOX" policy get > "/tmp/lw-policy-before-$(date +%s).yaml"

nemoclaw "$SANDBOX" policy add --from-file "$HERE/labwatch-bench.yaml" "$ACK"

# User-added presets (github.com sits in brew-balanced).
for p in brew-balanced huggingface npm openclaw-pricing pypi; do
  if nemoclaw "$SANDBOX" policy list | grep -q "● $p "; then
    nemoclaw "$SANDBOX" policy remove "$p" "$ACK"
  fi
done

# Baseline entries that reach the internet.
for k in clawhub npm_registry nvidia openclaw_api openclaw_docs; do
  if openshell policy get "$SANDBOX" --full | grep -q "^  $k:$"; then
    nemoclaw "$SANDBOX" policy exclude "$k" "$ACK"
  fi
done

openshell policy get "$SANDBOX" --full | grep -E '^  [A-Za-z0-9_-]+:$|host:|allowed_ips|- .[0-9./]+.$'
