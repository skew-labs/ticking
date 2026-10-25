#!/bin/bash
# emv2_daily.sh (skew, root; emv2-daily.service): refresh the address-keyed overlays (labels change daily: Muse, label deltas),
# reload emv2-serve, then rebuild the featured home snapshots. Each step logs one line; a failing chain keeps its previous files.
set -uo pipefail
ROOT=/srv/skew/emv2; PY=/srv/skew/em-refresh/.venv/bin/python; T=/srv/skew/emv2/tools
log() { echo "$(date -u +%FT%TZ) $*"; }
for d in $ROOT/*/current; do
  c=$(basename "$(dirname "$d")")
  L=/srv/skew/em-data/compact/$c/labels.parquet; [ -f /srv/skew/em-graphloop/export/$c/labels.parquet ] && L=/srv/skew/em-graphloop/export/$c/labels.parquet
  [ -f "$L" ] || { log "$c: no labels, overlay kept"; continue; }
  K=/srv/skew/em-data/econ/$c/token_meta.parquet; A=(--labels "$L"); [ -f "$K" ] && A+=(--tokens "$K")
  if $PY $T/emv2_overlay.py "$c" "$ROOT/$c/overlay" "${A[@]}"; then chown -R ubuntu:ubuntu "$ROOT/$c/overlay"; log "$c: overlay ok"; else log "$c: overlay FAILED (kept)"; fi
done
systemctl kill -s HUP emv2-serve && log "emv2-serve reloaded"; sleep 5
$PY $T/emv2_featured.py --out $ROOT/featured && chown -R ubuntu:ubuntu $ROOT/featured && log "featured ok" || log "featured FAILED (kept)"
