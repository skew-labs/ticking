#!/bin/bash
# reset_ledger.sh (VM, as root): drop a build dir's ledger outputs (format change or interrupted run) and keep its graph outputs,
# so run_chain.sh can rerun the ledger stage only. Logs are kept with a suffix.
#   reset_ledger.sh <build_dir> <reason>
set -euo pipefail
d=$1; why=${2:-reset}
[ -f "$d/GRAPH_OK" ] || { echo "no GRAPH_OK in $d; refusing" >&2; exit 1; }
cd "$d"
rm -f bal.*.seg* tt.*.seg* tok.*.seg* blk.time.seg* dict.tok.seg* dict.tok.str.seg* ledger.facts.json manifest.ledger.json BUILD_OK PUBLISHED.json
rm -rf dump
for f in ledger.log; do [ -e "$f" ] && mv "$f" "$f.$why.$(date -u +%H%M%S)"; done
ls | grep -v -e '^g\.' -e '^dict\.addr' | tr '\n' ' '; echo
