#!/usr/bin/env python3
"""tx_narratives: one Korean + one English sentence per multi-action tx for top wallets.

Example: "Aave V3에 1,000 WETH 담보 예치 → 2,000,000 USDC 대출 → Uniswap V3에서 USDC→ARB 스왑"
Rules: LP sub-legs (token0/token1) merge into one step; consecutive swaps in a tx collapse into
first-in -> last-out (hop count kept); Comet base legs use the positions-step relabel
(supply while in debt = repay, withdraw into debt = borrow)."""
import json
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
PNAME = {
    "aave_v3": "Aave V3", "aave_v2": "Aave V2", "radiant_v2": "Radiant V2", "radiant_v1": "Radiant V1",
    "compound_v3": "Compound V3", "morpho_blue": "Morpho Blue", "silo_v1": "Silo", "dolomite": "Dolomite",
    "uniswap_v2": "Uniswap V2", "uniswap_v3": "Uniswap V3", "uniswap_v4": "Uniswap V4", "camelot_v2": "Camelot V2",
    "camelot_v3": "Camelot V3", "sushiswap_v2": "SushiSwap", "gmx_v1": "GMX V1", "gmx_v2": "GMX V2",
    "arbitrum_bridge": "Arbitrum 브릿지", "robinhood_bridge": "Robinhood 브릿지", "arb_gateway": "Arbitrum 게이트웨이",
    "arbsys": "Arbitrum 브릿지", "arb_deposit_tx": "L1→L2 입금", "cctp": "CCTP", "cctp_v1": "CCTP", "cctp_v2": "CCTP V2",
    "across": "Across", "relay": "Relay", "oft": "LayerZero OFT", "stargate_v2": "Stargate", "usdt0": "USDT0 OFT",
    "usdg": "USDG OFT", "hop": "Hop", "merkl": "Merkl", "weth": "WETH", "erc4626": "볼트", "robinhood_stock": "Robinhood 주식토큰",
    "stablecoin": "스테이블코인",
}
PNAME_EN = dict(PNAME, arbitrum_bridge="Arbitrum bridge", robinhood_bridge="Robinhood bridge", arb_gateway="Arbitrum gateway",
                arbsys="Arbitrum bridge", arb_deposit_tx="L1→L2 deposit", erc4626="vault", robinhood_stock="Robinhood stock token",
                stablecoin="stablecoin")


def pname(p, en=False):
    if not p:
        return "?"
    d = PNAME_EN if en else PNAME
    if p in d:
        return d[p]
    if ":" in p:
        fam, addr = p.split(":", 1)
        return f"{fam.replace('_fork', '')}-fork({addr[:6]}…)"
    return p


def fmt(v):
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if a == 0:
        return "0"
    return f"{v:.4g}"


def run(con, out, chain, n_wallets=200, per_wallet=15):
    has_sum = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='balance_sheet_summary'").fetchone()[0]
    top_sql = """
      SELECT wallet FROM (
        SELECT wallet, net_worth_usd AS s FROM balance_sheet_summary WHERE net_worth_usd IS NOT NULL ORDER BY net_worth_usd DESC LIMIT {n}
      ) UNION SELECT wallet FROM (
        SELECT wallet, count(*) c FROM econ_actions WHERE action NOT IN ('swap','corporate_action','issuer_mint','issuer_burn')
        GROUP BY 1 ORDER BY c DESC LIMIT {m})""".format(n=n_wallets, m=n_wallets // 2) if has_sum else \
        f"SELECT wallet FROM econ_actions WHERE action <> 'swap' GROUP BY 1 ORDER BY count(*) DESC LIMIT {n_wallets}"
    con.execute(f"CREATE OR REPLACE TEMP TABLE nar_wallets AS {top_sql}")
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE nar_tx AS
      SELECT wallet, tx_hash, any_value(block_time) bt, count(DISTINCT action || coalesce(cast(log_index AS VARCHAR),'')) n
      FROM econ_actions WHERE wallet IN (SELECT wallet FROM nar_wallets) AND tx_hash IS NOT NULL
      GROUP BY 1, 2 HAVING count(DISTINCT log_index) >= 2 AND count(DISTINCT action) >= 2
      QUALIFY row_number() OVER (PARTITION BY wallet ORDER BY n DESC, bt DESC) <= {per_wallet}""")
    rows = con.execute("""
      SELECT a.wallet, a.tx_hash, a.block_time, a.log_index, a.sub, a.protocol, coalesce(r.action, a.action) AS action, a.asset,
             a.amount_raw::DOUBLE AS amt, a.extra_json, a.pos_kind, tm.symbol, tm.decimals,
             json_extract_string(a.extra_json, '$.token_out') AS t_out
      FROM econ_actions a JOIN nar_tx t ON t.wallet = a.wallet AND t.tx_hash = a.tx_hash
      LEFT JOIN comet_relabel r ON r.tx_hash = a.tx_hash AND r.log_index = a.log_index AND r.sub = a.sub
      LEFT JOIN token_meta tm ON tm.token = a.asset
      ORDER BY a.wallet, a.block_time, a.tx_hash, a.log_index, a.sub""").fetchall()
    meta = {r[0]: (r[1], r[2]) for r in con.execute("SELECT token, symbol, decimals FROM token_meta").fetchall()}

    def sym(t):
        if t == NATIVE:
            return "ETH"
        m = meta.get(t)
        return (m[0] if m and m[0] else (t[:8] + "…" if t else "?"))

    def units(t, raw):
        if raw is None:
            return None
        m = meta.get(t)
        d = m[1] if m and m[1] is not None else 18
        return raw / (10 ** d)

    txs = {}
    for r in rows:
        txs.setdefault((r[0], r[1]), []).append(r)
    outrows = []
    for (wallet, txh), acts in txs.items():
        steps = []   # (kind, dict)
        i = 0
        while i < len(acts):
            w, h, bt, li, sub, proto, action, asset, amt, extra, pk, s, dec, t_out = acts[i]
            ex = json.loads(extra) if extra else {}
            if action in ("lp_add", "lp_remove", "lp_collect"):
                legs = [acts[i]]
                while i + 1 < len(acts) and acts[i + 1][3] == li and acts[i + 1][6] == action:
                    i += 1
                    legs.append(acts[i])
                steps.append((action, {"proto": proto, "legs": [(x[7], units(x[7], x[8])) for x in legs]}))
            elif action == "swap":
                hops = 1
                tin, ain = asset, units(asset, amt)
                tout = t_out
                aout = units(t_out, float(ex.get("amount_out") or 0))
                protos = [proto]
                while i + 1 < len(acts) and acts[i + 1][6] == "swap":
                    i += 1
                    nx = acts[i]
                    nex = json.loads(nx[9]) if nx[9] else {}
                    hops += 1
                    tout = nx[13]
                    aout = units(tout, float(nex.get("amount_out") or 0))
                    protos.append(nx[5])
                steps.append(("swap", {"proto": protos[0] if len(set(protos)) == 1 else "multi", "tin": tin, "ain": ain,
                                       "tout": tout, "aout": aout, "hops": hops}))
            elif action == "issuer_mint" and any(a[6] in ("bridge_in", "lp_remove", "withdraw") for a in acts):
                pass   # mint leg of a bridge / position exit, already described
            else:
                steps.append((action, {"proto": proto, "asset": asset, "amt": units(asset, amt), "pk": pk, "ex": ex}))
            i += 1
        if len(steps) < 2:
            continue
        ko, en = [], []
        last_proto = None
        for kind, d in steps:
            p = d.get("proto")
            pk_, pe = pname(p), pname(p, True)
            new_p = p != last_proto
            last_proto = p
            if kind in ("lp_add", "lp_remove", "lp_collect"):
                legs = " + ".join(f"{fmt(a)} {sym(t)}" for t, a in d["legs"] if t)
                pair = "/".join(sym(t) for t, _ in d["legs"] if t)
                if kind == "lp_add":
                    ko.append(f"{pk_}에 {pair} 유동성 공급({legs})"); en.append(f"added {pair} liquidity on {pe} ({legs})")
                elif kind == "lp_remove":
                    ko.append(f"{pk_}에서 {pair} 유동성 회수({legs})"); en.append(f"removed {pair} liquidity on {pe} ({legs})")
                else:
                    ko.append(f"{pk_} LP 수수료/원금 수령({legs})"); en.append(f"collected {legs} from {pe} LP")
                continue
            if kind == "swap":
                hop = f", {d['hops']}홉" if d["hops"] > 1 else ""
                hop_en = f", {d['hops']} hops" if d["hops"] > 1 else ""
                where = "여러 DEX" if p == "multi" else pk_
                where_en = "multiple DEXs" if p == "multi" else pe
                ko.append(f"{where}에서 {sym(d['tin'])}→{sym(d['tout'])} 스왑({fmt(d['ain'])} → {fmt(d['aout'])}{hop})")
                en.append(f"swapped {fmt(d['ain'])} {sym(d['tin'])} → {fmt(d['aout'])} {sym(d['tout'])} on {where_en}{hop_en}")
                continue
            a, s_ = fmt(d["amt"]), sym(d["asset"])
            ex = d["ex"]
            at_ko = f"{pk_}에 " if new_p else ""
            at_en = f" on {pe}" if new_p else ""
            if kind == "supply":
                coll = d["pk"] == "collateral" or ex.get("collateral") or p.startswith("aave")
                ko.append(f"{at_ko}{a} {s_} {'담보 예치' if coll else '예치'}")
                en.append(f"supplied {a} {s_}{' as collateral' if coll else ''}{at_en}")
            elif kind == "withdraw":
                ko.append(f"{pk_ + '에서 ' if new_p else ''}{a} {s_} 인출"); en.append(f"withdrew {a} {s_}{' from ' + pe if new_p else ''}")
            elif kind == "borrow":
                ko.append(f"{pk_ + '에서 ' if new_p else ''}{a} {s_} 대출"); en.append(f"borrowed {a} {s_}{' from ' + pe if new_p else ''}")
            elif kind == "repay":
                ko.append(f"{at_ko}{a} {s_} 상환"); en.append(f"repaid {a} {s_}{at_en}")
            elif kind == "liquidated":
                ko.append(f"{pk_}에서 {a} {s_} 청산당함"); en.append(f"was liquidated for {a} {s_} on {pe}")
            elif kind == "liquidate":
                ko.append(f"{pk_}에서 {a} {s_} 청산 실행"); en.append(f"liquidated a borrower repaying {a} {s_} on {pe}")
            elif kind == "flashloan":
                ko.append(f"{pk_} 플래시론 {a} {s_}"); en.append(f"flash-borrowed {a} {s_} from {pe}")
            elif kind == "bridge_out":
                dest = ex.get("dest_chain") or ex.get("dst_eid") or ex.get("dest_domain") or ("L1" if p in ("arbitrum_bridge", "robinhood_bridge", "arbsys", "arb_gateway") else None)
                ko.append(f"{pk_}로 {a} {s_} 브릿지 출금{'(→' + str(dest) + ')' if dest else ''}")
                en.append(f"bridged out {a} {s_} via {pe}{' to ' + str(dest) if dest else ''}")
            elif kind == "bridge_in":
                src = ex.get("origin_chain") or ex.get("src_eid")
                ko.append(f"{pk_}로 {a} {s_} 브릿지 입금{'(←' + str(src) + ')' if src else ''}")
                en.append(f"received {a} {s_} via {pe}{' from ' + str(src) if src else ''}")
            elif kind in ("perp_increase", "perp_decrease", "perp_liquidated"):
                side = ex.get("is_long")
                side_ko = "롱" if side else ("숏" if side is not None else "")
                side_en = "long" if side else ("short" if side is not None else "")
                if ex.get("leg") == "collateral":
                    continue
                usd = (d["amt"] or 0) * (10 ** (meta.get(d["asset"], (None, 18))[1] or 18)) / 1e30 if d["amt"] is not None else None
                verb = {"perp_increase": ("포지션 증가", "increased"), "perp_decrease": ("포지션 축소", "decreased"),
                        "perp_liquidated": ("포지션 청산당함", "was liquidated on")}[kind]
                ko.append(f"{pk_} {s_} {side_ko} {verb[0]}(${fmt(usd)})")
                en.append(f"{verb[1]} a {s_} {side_en} on {pe} (${fmt(usd)} notional)")
            elif kind == "stake":
                ko.append(f"{pk_}에 {a} {s_} 스테이킹/예치"); en.append(f"staked {a} {s_} into {pe}")
            elif kind == "unstake":
                ko.append(f"{pk_}에서 {a} {s_} 언스테이킹"); en.append(f"unstaked {a} {s_} from {pe}")
            elif kind == "wrap":
                ko.append(f"{a} ETH→{s_} 래핑"); en.append(f"wrapped {a} ETH to {s_}")
            elif kind == "unwrap":
                ko.append(f"{a} {s_}→ETH 언래핑"); en.append(f"unwrapped {a} {s_} to ETH")
            elif kind == "incentive_claim":
                ko.append(f"{pk_} 인센티브 {a} {s_} 수령"); en.append(f"claimed {a} {s_} incentives from {pe}")
            elif kind == "issuer_mint":
                ko.append(f"{s_} {a} 신규 발행(창출)"); en.append(f"{a} {s_} minted by issuer (creation)")
            elif kind == "issuer_burn":
                ko.append(f"{s_} {a} 소각(상환)"); en.append(f"{a} {s_} burned (redemption)")
            elif kind == "corporate_action":
                ko.append(f"{s_} 기업행위(배수 갱신)"); en.append(f"{s_} corporate action (multiplier update)")
            else:
                ko.append(f"{kind} {a} {s_}"); en.append(f"{kind} {a} {s_}")
        if len(ko) < 2:
            continue
        outrows.append({"chain": chain, "wallet": wallet, "tx_hash": txh, "block_time": str(acts[0][2]),
                        "n_steps": len(ko), "protocols": ",".join(sorted({str(a[5]) for a in acts})),
                        "narrative_ko": " → ".join(ko), "narrative_en": ("; then ".join(en))[:1].upper() + ("; then ".join(en))[1:]})
    tbl = pa.Table.from_pylist(outrows) if outrows else pa.table({"chain": pa.array([], pa.int64())})
    pq.write_table(tbl, os.path.join(out, "tx_narratives.parquet"))
    import csv
    with open(os.path.join(out, "tx_narratives.csv"), "w", newline="") as f:
        if outrows:
            wr = csv.DictWriter(f, fieldnames=list(outrows[0].keys()))
            wr.writeheader()
            wr.writerows(outrows)
    print(f"narratives: {len(outrows)} txs for {len({r['wallet'] for r in outrows})} wallets", file=sys.stderr)
