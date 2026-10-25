#!/usr/bin/env python3
"""compare.py: row-level comparison of EMV2 dumps against the frozen map golden (runs on the VM, uses the duckdb CLI).

    compare.py CHAIN [--graph-bin BIN --image DIR] [--dump DIR] [--golden golden/CHAIN] [--out results/CHAIN] [--full-bal]

Inputs
  golden/<chain>/  edges.parquet, address_tokens.parquet, addresses.parquet, token_stats.parquet, checks.json, sample_addresses.txt
  graph:  BIN dump --image DIR --addresses FILE --table edges|nodes        (CSV on stdout, map keys, "from" = sampled address)
  ledger: DIR/bal.tsv.zst, DIR/token_stats.tsv.zst, DIR/counts.tsv          (TSV, header, 0x lower-case hex, decimal strings)

Writes results/<chain>/RESULT.json (PASS/FAIL per check with counts and up to 20 differing rows each) and prints a summary.
Exact fields must match exactly; net_approx (double in the map) is only measured against the V2 integer balance.
"""
import argparse
import json
import os
import subprocess
import sys

HEX = "'0x' || lower(hex({}))"


def duck(sql, db=":memory:"):
    p = subprocess.run(["duckdb", "-json", db, "-c", sql], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:2000])
    out = p.stdout.strip()
    return json.loads(out) if out else []


def run_graph(args, table, out):
    addrs = os.path.join(args.golden, "sample_addresses.txt")
    if args.emv2_dump:   # core tool: emv2-dump <build_dir> <table> --addresses F --out F.csv
        cmd, sink = [args.emv2_dump, args.image, table, "--addresses", addrs, "--out", out], None
    else:                # graph tool: emv2-graph dump --image DIR --addresses F --table T  (CSV on stdout)
        cmd, sink = [args.graph_bin, "dump", "--image", args.image, "--addresses", addrs, "--table", table], open(out, "w")
    p = subprocess.run(cmd, stdout=sink, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError(f"dump {table}: {p.stderr.strip()[:500]}")


def check(results, name, diff_sql, count_sql=None, setup=""):
    """diff_sql selects differing rows; PASS when it returns none."""
    try:
        n = duck(setup + f"SELECT count(*) AS n FROM ({diff_sql})")[0]["n"]
        rows = duck(setup + f"SELECT * FROM ({diff_sql}) LIMIT 20") if n else []
        total = duck(setup + count_sql)[0]["n"] if count_sql else None
        results.append({"check": name, "ok": n == 0, "differing": n, "compared": total, "sample": rows})
    except RuntimeError as e:
        results.append({"check": name, "ok": False, "error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("--graph-bin")
    ap.add_argument("--emv2-dump", help="core emv2-dump binary (alternative to --graph-bin); --image is then the build dir")
    ap.add_argument("--image")
    ap.add_argument("--dump", help="ledger dump directory (bal.tsv.zst, token_stats.tsv.zst, counts.tsv)")
    ap.add_argument("--golden")
    ap.add_argument("--out")
    ap.add_argument("--as-of", type=int, help="map end block: V2 rows touched after it (last_block > N) are time_skew, not failures")
    ap.add_argument("--full-bal", action="store_true", help="also compare every map address_tokens row (needs the full map parts in src/)")
    args = ap.parse_args()
    g = args.golden = args.golden or f"golden/{args.chain}"
    out = args.out or f"results/{args.chain}"
    os.makedirs(out, exist_ok=True)
    res = []
    meta = {"chain": args.chain, "as_of": args.as_of, "golden": json.load(open(os.path.join(g, "SOURCE.json"))) if os.path.exists(os.path.join(g, "SOURCE.json")) else None}

    hf, ht = HEX.format('"from"'), HEX.format('"to"')
    ge = (f"SELECT {hf} AS f, {ht} AS t, "
          f"CASE WHEN kind = 'native_tx' THEN 'native' ELSE {HEX.format('asset')} END AS a, kind AS k, n::UBIGINT AS n, "
          f"coalesce(total_amount::VARCHAR, total_amount_big) AS amt, first_block::UBIGINT AS fb, last_block::UBIGINT AS lb "
          f"FROM read_parquet('{g}/edges.parquet')")
    if args.graph_bin or args.emv2_dump:
        run_graph(args, "edges", f"{out}/v2_edges.csv")
        run_graph(args, "nodes", f"{out}/v2_nodes.csv")
        ve = (f"SELECT lower(\"from\") AS f, lower(\"to\") AS t, CASE WHEN kind = 'native_tx' THEN 'native' ELSE lower(asset) END AS a, "
              f"kind AS k, n::UBIGINT AS n, nullif(total_amount::VARCHAR, '') AS amt, first_block::UBIGINT AS fb, last_block::UBIGINT AS lb "
              f"FROM read_csv('{out}/v2_edges.csv', header=true, all_varchar=true) "
              f"WHERE lower(\"from\") IN (SELECT lower(column0) FROM read_csv('{g}/sample_addresses.txt', header=false))")
        setup = f"CREATE TEMP VIEW ge AS {ge}; CREATE TEMP VIEW ve AS {ve};"
        check(res, "edges: keys present in map but missing in V2",
              "SELECT * FROM ge ANTI JOIN ve USING (f, t, a, k)", "SELECT count(*) AS n FROM ge", setup)
        check(res, "edges: keys in V2 but not in map",
              "SELECT * FROM ve ANTI JOIN ge USING (f, t, a, k)", "SELECT count(*) AS n FROM ve", setup)
        check(res, "edges: n, total_amount, first/last block equal",
              "SELECT ge.*, ve.n AS v_n, ve.amt AS v_amt, ve.fb AS v_fb, ve.lb AS v_lb FROM ge JOIN ve USING (f, t, a, k) "
              "WHERE ge.n IS DISTINCT FROM ve.n OR ge.amt IS DISTINCT FROM ve.amt OR ge.fb IS DISTINCT FROM ve.fb OR ge.lb IS DISTINCT FROM ve.lb",
              None, setup)
        cols = ["first_block", "last_block", "tx_sent", "tx_received", "token_transfers_in", "token_transfers_out", "is_contract"]
        gn = (f"SELECT {HEX.format('address')} AS addr, " + ", ".join(f"{c}::VARCHAR AS {c}" for c in cols) + ", in_degree::VARCHAR AS in_degree, "
              f"out_degree::VARCHAR AS out_degree FROM read_parquet('{g}/addresses.parquet')")
        vn = f"SELECT lower(address) AS addr, * EXCLUDE (address) FROM read_csv('{out}/v2_nodes.csv', header=true, all_varchar=true)"
        setup_n = f"CREATE TEMP VIEW gn AS {gn}; CREATE TEMP VIEW vn AS {vn};"
        cond = " OR ".join(f"lower(gn.{c}) IS DISTINCT FROM lower(vn.{c})" for c in cols + ["in_degree", "out_degree"])
        check(res, "nodes: sampled addresses present", "SELECT * FROM gn ANTI JOIN vn USING (addr)", "SELECT count(*) AS n FROM gn", setup_n)
        check(res, "nodes: wallet-card counters equal", f"SELECT gn.addr, gn.*, vn.* FROM gn JOIN vn USING (addr) WHERE {cond}", None, setup_n)

    if args.dump:
        d = args.dump
        pick = lambda stem: next((f"{d}/{stem}{x}" for x in (".tsv.zst", ".csv", ".tsv", ".csv.zst") if os.path.exists(f"{d}/{stem}{x}")), f"{d}/{stem}.csv")
        bal = f"read_csv('{pick('bal')}', header=true, all_varchar=true)"
        has1155 = duck(f"SELECT count(*) AS n FROM {bal} WHERE kind = 'erc1155'")[0]["n"] > 0
        meta["erc1155_in_v2_bal"] = has1155
        k1155 = "" if has1155 else " WHERE kind <> 'erc1155'"
        gb = (f"SELECT {HEX.format('address')} AS addr, {HEX.format('token')} AS tok, kind AS k, n_in::UBIGINT AS n_in, n_out::UBIGINT AS n_out, "
              f"holds, net_approx, first_block::UBIGINT AS fb, last_block::UBIGINT AS lb FROM read_parquet('{g}/address_tokens.parquet'){k1155}")
        vb = (f"SELECT lower(address) AS addr, lower(token) AS tok, kind AS k, n_in::UBIGINT AS n_in, n_out::UBIGINT AS n_out, "
              f"balance::HUGEINT AS bal, first_block::UBIGINT AS fb, last_block::UBIGINT AS lb FROM {bal} "
              f"WHERE lower(address) IN (SELECT lower(column0) FROM read_csv('{g}/sample_addresses.txt', header=false))")
        setup = f"CREATE TEMP TABLE gb AS {gb}; CREATE TEMP TABLE vb AS {vb};"
        check(res, "bal: map (address, token, kind) rows missing in V2", "SELECT * FROM gb ANTI JOIN vb USING (addr, tok, k)",
              "SELECT count(*) AS n FROM gb", setup)
        check(res, "bal: V2 rows not in map", "SELECT * FROM vb ANTI JOIN gb USING (addr, tok, k)" + (f" WHERE fb <= {args.as_of}" if args.as_of else ""),
              "SELECT count(*) AS n FROM vb", setup)
        late = f" AND vb.lb <= {args.as_of}" if args.as_of else ""
        mism = ("SELECT gb.*, vb.n_in AS v_in, vb.n_out AS v_out, vb.fb AS v_fb, vb.lb AS v_lb FROM gb JOIN vb USING (addr, tok, k) "
                "WHERE (gb.n_in <> vb.n_in OR gb.n_out <> vb.n_out OR gb.fb IS DISTINCT FROM vb.fb OR gb.lb IS DISTINCT FROM vb.lb)")
        check(res, "bal: n_in, n_out, first/last block equal", mism + late, None, setup)
        if args.as_of:
            meta["bal_time_skew_rows"] = duck(setup + f"SELECT count(*) AS n FROM ({mism} AND vb.lb > {args.as_of})")[0]["n"]
            meta["bal_v2_rows_after_as_of"] = duck(setup + f"SELECT count(*) AS n FROM vb WHERE lb > {args.as_of}")[0]["n"]
        check(res, "bal: holds (exact sign in the map) == V2 balance > 0",
              "SELECT gb.addr, gb.tok, gb.k, gb.holds, vb.bal FROM gb JOIN vb USING (addr, tok, k) WHERE gb.holds <> (vb.bal > 0)"
              + (f" AND vb.lb <= {args.as_of}" if args.as_of else ""), None, setup)
        # measured, not a pass/fail: how far the map's double drifts from the exact integer
        try:
            m = duck(setup + "SELECT count(*) AS n, count(*) FILTER (WHERE (gb.net_approx > 0) <> (vb.bal > 0)) AS approx_sign_differs, "
                     "max(abs(gb.net_approx - vb.bal::DOUBLE) / greatest(abs(vb.bal::DOUBLE), 1)) AS max_rel_err "
                     "FROM gb JOIN vb USING (addr, tok, k) WHERE gb.k <> 'erc721'")[0]
            meta["net_approx_vs_exact"] = m
        except RuntimeError as e:
            meta["net_approx_vs_exact"] = {"error": str(e)}
        ts = f"read_csv('{pick('token_stats')}', header=true, all_varchar=true)"
        gt = f"SELECT {HEX.format('token')} AS tok, standard, n_transfers::UBIGINT AS n_tr, n_holders_approx::UBIGINT AS n_h FROM read_parquet('{g}/token_stats.parquet')" + ("" if has1155 else " WHERE standard NOT LIKE '%erc1155%'")
        vt = f"SELECT lower(token) AS tok, sum(n_transfers::UBIGINT) AS n_tr, sum(n_holders::UBIGINT) AS n_h FROM {ts} GROUP BY 1"
        setup_t = f"CREATE TEMP TABLE gt AS {gt}; CREATE TEMP TABLE vt AS {vt};"
        check(res, "token_stats: n_transfers equal per token", "SELECT gt.*, vt.n_tr AS v_tr FROM gt JOIN vt USING (tok) WHERE gt.n_tr <> vt.n_tr",
              "SELECT count(*) AS n FROM gt", setup_t)
        check(res, "token_stats: tokens missing in V2", "SELECT * FROM gt ANTI JOIN vt USING (tok)", None, setup_t)
        try:
            meta["n_holders_vs_map_approx"] = duck(setup_t + "SELECT count(*) AS tokens, count(*) FILTER (WHERE gt.n_h <> vt.n_h) AS differ "
                                                    "FROM gt JOIN vt USING (tok)")[0]
        except RuntimeError as e:
            meta["n_holders_vs_map_approx"] = {"error": str(e)}
        # chain totals against the map's checks.json (only the equations V2 can recompute from its own outputs)
        cpath = pick("counts")
        sep = "\t" if cpath.endswith((".tsv", ".tsv.zst")) else ","
        counts = dict(l.rstrip("\n").split(sep, 1) for l in open(cpath) if sep in l)
        meta["counts"] = counts
        exp = {c["check_name"]: c["expected"] for c in json.load(open(os.path.join(g, "checks.json")))}
        tt = exp.get("sum(address_tokens.n_in) == all token transfers")
        if not has1155 and tt is not None:   # phase 1: V2 bal leaves ERC1155 out, so its totals exclude the ERC1155 transfer count
            tt = str(int(tt) - int(exp.get("erc1155 edges.n == token_transfers ERC1155", 0)))
        for key, label in (("sum_n_in", "sum(n_in) == all token transfers"), ("sum_n_out", "sum(n_out) == all token transfers")):
            got = counts.get(key)
            res.append({"check": f"counts: {label}", "ok": got is not None and str(got) == str(tt), "got": got, "expected": tt})
        if args.full_bal:
            full = (f"SELECT {HEX.format('address')} AS addr, {HEX.format('token')} AS tok, kind AS k, n_in::UBIGINT AS n_in, n_out::UBIGINT AS n_out, holds "
                    f"FROM read_parquet('src/{args.chain}/full/address_tokens/*.parquet')")
            vfull = (f"SELECT lower(address) AS addr, lower(token) AS tok, kind AS k, n_in::UBIGINT AS n_in, n_out::UBIGINT AS n_out, "
                     f"balance::HUGEINT > 0 AS pos FROM {bal}")
            setup_f = f"SET threads=8; SET memory_limit='48GB'; SET temp_directory='/data/emv2-dev/verify/tmp'; CREATE TEMP VIEW gf AS {full}; CREATE TEMP VIEW vf AS {vfull};"
            check(res, "bal(full): every map row equal (n_in, n_out, holds)",
                  "SELECT gf.addr, gf.tok, gf.k, gf.n_in, gf.n_out, gf.holds, vf.n_in AS v_in, vf.n_out AS v_out, vf.pos FROM gf FULL JOIN vf USING (addr, tok, k) "
                  "WHERE gf.n_in IS DISTINCT FROM vf.n_in OR gf.n_out IS DISTINCT FROM vf.n_out OR gf.holds IS DISTINCT FROM vf.pos",
                  "SELECT count(*) AS n FROM gf", setup_f)

    ok = all(r.get("ok") for r in res) and bool(res)
    json.dump({"meta": meta, "ok": ok, "checks": res}, open(f"{out}/RESULT.json", "w"), indent=1, default=str)
    for r in res:
        print(("PASS " if r.get("ok") else "FAIL ") + r["check"], {k: r[k] for k in ("differing", "compared", "got", "expected", "error") if k in r})
    print(args.chain, "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
