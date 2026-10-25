#!/bin/bash
# golden_sample.sh CHAIN MAPPREFIX "BUCKETS" [EXTRA_ADDR...]: on the VM (root), pull map parts for the given buckets from Blob and
# freeze a sample of addresses plus every map row keyed by them (edges by "from", address_tokens/addresses by address).
# Output: golden/<chain>/{sample_addresses.txt, edges.parquet, address_tokens.parquet, addresses.parquet, token_stats.parquet, SOURCE.json}
set -euo pipefail
CH=$1; MP=$2; BK=$3; shift 3
cd /data/emv2-dev/verify
S=src/$CH; G=golden/$CH; mkdir -p $S $G
for b in $BK; do for t in edges address_tokens addresses; do
  [ -s $S/${t}_$b.parquet ] || python3 blob_get.py $MP/$t/part-$b.parquet $S/${t}_$b.parquet >/dev/null
done; done
[ -s $S/token_stats.parquet ] || python3 blob_get.py $MP/token_stats/token_stats.parquet $S/token_stats.parquet >/dev/null
python3 blob_get.py $MP/checks.json $G/checks.json >/dev/null
EXTRA=$(printf "'%s'," "$@"); EXTRA="[${EXTRA%,}]"; [ $# -eq 0 ] && EXTRA="[]::VARCHAR[]"
nice -n 10 duckdb -c "
SET threads=4; SET memory_limit='8GB'; SET preserve_insertion_order=false;
CREATE TABLE a AS SELECT * FROM read_parquet('$S/addresses_*.parquet');
CREATE TABLE pick AS
  SELECT address, 'hub_out' AS why FROM (SELECT address FROM a ORDER BY out_degree DESC LIMIT 25)
  UNION ALL SELECT address, 'hub_in' FROM (SELECT address FROM a ORDER BY in_degree DESC LIMIT 25)
  UNION ALL SELECT address, 'recent' FROM (SELECT address FROM a ORDER BY last_block DESC, address LIMIT 200)
  UNION ALL SELECT address, 'new' FROM (SELECT address FROM a ORDER BY first_block DESC, address LIMIT 100)
  UNION ALL SELECT address, 'contract' FROM (SELECT address FROM a WHERE is_contract ORDER BY hash(address) LIMIT 100)
  UNION ALL SELECT address, 'random' FROM (SELECT address FROM a ORDER BY hash(address || 'emv2') LIMIT 1500)
  UNION ALL SELECT from_hex(substr(x, 3)), 'named' FROM unnest($EXTRA) t(x);
CREATE TABLE s AS SELECT address, string_agg(DISTINCT why, ',') AS why FROM pick GROUP BY address;
COPY (SELECT '0x' || lower(hex(address)) AS address, why FROM s ORDER BY address) TO '$G/sample.csv' (HEADER);
COPY (SELECT '0x' || lower(hex(address)) FROM s ORDER BY 1) TO '$G/sample_addresses.txt' (HEADER false);
COPY (SELECT e.* FROM read_parquet('$S/edges_*.parquet') e SEMI JOIN s ON e.\"from\" = s.address ORDER BY \"from\", \"to\", asset, kind)
  TO '$G/edges.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT t.* FROM read_parquet('$S/address_tokens_*.parquet') t SEMI JOIN s ON t.address = s.address ORDER BY address, token, kind)
  TO '$G/address_tokens.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT a.* FROM a SEMI JOIN s ON a.address = s.address ORDER BY address) TO '$G/addresses.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM read_parquet('$S/token_stats.parquet')) TO '$G/token_stats.parquet' (FORMAT parquet, COMPRESSION zstd);
SELECT 'sample', count(*) FROM s UNION ALL SELECT 'edges', count(*) FROM read_parquet('$G/edges.parquet')
UNION ALL SELECT 'address_tokens', count(*) FROM read_parquet('$G/address_tokens.parquet')
UNION ALL SELECT 'addresses', count(*) FROM read_parquet('$G/addresses.parquet');
"
python3 - "$G" "$MP" "$BK" <<'PY'
import hashlib, json, os, sys, time
g, mp, bk = sys.argv[1:4]
files = {f: hashlib.sha256(open(os.path.join(g, f), 'rb').read()).hexdigest() for f in sorted(os.listdir(g)) if f != 'SOURCE.json'}
json.dump({"map": mp, "buckets": bk.split(), "frozenAt": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), "sha256": files,
           "rule": "edges: every map row whose \"from\" is a sampled address (edges are bucketed by from); address_tokens/addresses: rows of sampled addresses"},
          open(os.path.join(g, 'SOURCE.json'), 'w'), indent=1)
PY
echo done $CH
