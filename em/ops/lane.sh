#!/bin/bash
# lane.sh (VM, as root through systemd-run): run several chain builds one after another in one memory-capped unit.
#   lane.sh "<chain> <build_id> <max_block> <parts> [out_root] [publish]" ...   (one quoted spec per build; EMV2_SIMD=x;spec allowed)
set -uo pipefail
for spec in "$@"; do
  simd=""; case $spec in *";"*) simd=${spec%%;*}; spec=${spec#*;};; esac
  # shellcheck disable=SC2086
  if [ -n "$simd" ]; then env $simd bash /data/emv2-dev/ops/run_chain.sh $spec; rc=$?; else bash /data/emv2-dev/ops/run_chain.sh $spec; rc=$?; fi
  echo "$(date -u +%T) lane: '$spec' exit $rc"
done
