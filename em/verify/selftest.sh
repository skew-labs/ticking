#!/bin/bash
# selftest.sh CHAIN: build a fake "perfect V2" dump from the golden itself (and one with planted differences) and check that
# compare.py passes the first and fails the second with exactly the planted rows. Proves the harness, not the engine.
set -euo pipefail
CH=$1; G=golden/$CH; T=tmp/selftest/$CH; rm -rf $T; mkdir -p $T/img $T/dump $T/bad/dump
duckdb -c "
COPY (SELECT '0x'||lower(hex(\"from\")) AS \"from\", '0x'||lower(hex(\"to\")) AS \"to\", CASE WHEN kind='native_tx' THEN 'native' ELSE '0x'||lower(hex(asset)) END AS asset,
      kind, n, coalesce(total_amount::VARCHAR, total_amount_big) AS total_amount, first_block, last_block FROM read_parquet('$G/edges.parquet')) TO '$T/img/edges.csv' (HEADER);
COPY (SELECT '0x'||lower(hex(address)) AS address, first_block, last_block, tx_sent, tx_received, token_transfers_in, token_transfers_out, is_contract,
      in_degree, out_degree, out_degree AS v2_out_entries, in_degree AS v2_in_entries FROM read_parquet('$G/addresses.parquet')) TO '$T/img/nodes.csv' (HEADER);
COPY (SELECT '0x'||lower(hex(address)) AS address, '0x'||lower(hex(token)) AS token, kind, n_in, n_out,
      CASE WHEN kind='erc721' THEN (n_in::HUGEINT - n_out::HUGEINT) WHEN holds THEN greatest(round(net_approx)::HUGEINT, 1) ELSE least(round(net_approx)::HUGEINT, 0) END AS balance,
      first_block, last_block FROM read_parquet('$G/address_tokens.parquet')) TO '$T/dump/bal.tsv.zst' (HEADER, DELIMITER '\t', COMPRESSION zstd);
COPY (SELECT '0x'||lower(hex(token)) AS token, standard AS kind, n_transfers, n_holders_approx AS n_holders, '0' AS supply FROM read_parquet('$G/token_stats.parquet'))
  TO '$T/dump/token_stats.tsv.zst' (HEADER, DELIMITER '\t', COMPRESSION zstd);
"
TT=$(python3 -c "import json;print({c['check_name']:c['expected'] for c in json.load(open('$G/checks.json'))}['sum(address_tokens.n_in) == all token transfers'])")
printf "key\tvalue\nsum_n_in\t%s\nsum_n_out\t%s\nbuild_id\tselftest\n" $TT $TT > $T/dump/counts.tsv
cat > $T/fakebin <<SH
#!/bin/bash
# fake emv2-graph: dump --image DIR --addresses FILE --table T  -> cat DIR/T.csv
while [ \$# -gt 0 ]; do case \$1 in --image) I=\$2; shift;; --table) TB=\$2; shift;; esac; shift; done
cat \$I/\$TB.csv
SH
chmod +x $T/fakebin
echo "== perfect copy (expect PASS)"
python3 compare.py $CH --graph-bin $T/fakebin --image $T/img --dump $T/dump --out $T/res-good | tail -14
# planted: one edge amount changed, one edge dropped, one balance sign flipped, one n_in off by one
mkdir -p $T/bad/img; cp $T/dump/token_stats.tsv.zst $T/dump/counts.tsv $T/bad/dump/
python3 - $T <<'PY'
import csv, sys
t = sys.argv[1]
rows = list(csv.reader(open(f"{t}/img/edges.csv")))
hdr, body = rows[0], rows[1:]
i = next(k for k, r in enumerate(body) if r[5]); body[i][5] = str(int(body[i][5]) + 1); del body[i + 1]
csv.writer(open(f"{t}/bad/img/edges.csv", "w")).writerows([hdr] + body)
open(f"{t}/bad/img/nodes.csv", "w").write(open(f"{t}/img/nodes.csv").read())
PY
duckdb -c "COPY (SELECT * EXCLUDE (rn) REPLACE (CASE WHEN rn = 3 THEN -1 * abs(balance::HUGEINT) - 1 ELSE balance::HUGEINT END AS balance,
                                   CASE WHEN rn = 5 THEN n_in::UBIGINT + 1 ELSE n_in::UBIGINT END AS n_in)
  FROM (SELECT *, row_number() OVER () AS rn FROM read_csv('$T/dump/bal.tsv.zst', delim='\t', header=true, all_varchar=true) WHERE kind <> 'erc721'))
  TO '$T/bad/dump/bal.tsv.zst' (HEADER, DELIMITER '\t', COMPRESSION zstd)"
echo "== planted differences (expect FAIL on edges amount, edges missing, holds, n_in; bal rows of erc721 dropped -> missing)"
python3 compare.py $CH --graph-bin $T/fakebin --image $T/bad/img --dump $T/bad/dump --out $T/res-bad | tail -14 || true
