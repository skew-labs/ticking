#!/bin/bash
# capture_api.sh OUTDIR "name|path?query" ...: GET each request from the local EM services once (sequential, retry on 429 with
# backoff, max 6 tries), saving body to OUTDIR/name.json and a line "name code seconds tries" to OUTDIR/_index.tsv.
OUT=$1; shift; mkdir -p "$OUT"
for spec in "$@"; do
  name=${spec%%|*}; url=${spec#*|}
  for try in 1 2 3 4 5 6; do
    r=$(curl -s -m 180 -o "$OUT/$name.json" -w "%{http_code} %{time_total}" "$url")
    code=${r%% *}
    [ "$code" != 429 ] && break
    sleep $((try * 8))
  done
  printf "%s\t%s\t%s\t%s\n" "$name" "$code" "${r#* }" "$try" | tee -a "$OUT/_index.tsv"
done
