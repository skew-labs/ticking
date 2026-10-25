#!/usr/bin/env python3
"""Economic Machine — Perceive → Plan → Act for track seeds (autonomous judgment).

SoT for hops: parquet under HOPS_ROOT when readable; else honest empty / TT fallback
hints only (wallet-flow markers), never fake CEX/bridge.

Does not talk to the user with address lists — returns Muse track act.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hops_to_chart_markers import hops_to_chart_markers
try:
    from tip_hot_fills import fills_for_address as tip_hot_fills_for_address
except Exception:
    tip_hot_fills_for_address = None  # type: ignore

from muse_track_mode import (
    MODE,
    build_track_act,
    detect_track_intent,
    follow_next_hop_act,
)

SCHEMA_PERCEIVE = "ticking.perceive.track.v1"
SCHEMA_PLAN = "ticking.plan.track.v1"
HOPS_ROOT = Path(os.environ.get("HOPS_ROOT", "/srv/skew-bulk/hops"))
DUCKDB_PY = os.environ.get(
    "HOPS_DUCKDB_PYTHON",
    "/srv/skew/holdings-venv/bin/python",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def coverage_for_chains(chains: list[str]) -> dict:
    """Filesystem honesty — shallow checks only (no full tree walk)."""
    out = {"transfers": {}, "swaps": {}, "bridges": {}}

    def _has_parquet(root: Path) -> bool:
        try:
            next(root.glob("*.parquet"))
            return True
        except StopIteration:
            pass
        try:
            next(root.glob("*/*.parquet"))
            return True
        except StopIteration:
            pass
        try:
            next(root.glob("block=*/part-*.parquet"))
            return True
        except StopIteration:
            return False

    for c in chains:
        for kind, key in (("transfers", "transfers"), ("swaps", "swaps"), ("bridges", "bridges")):
            path = HOPS_ROOT / c / kind
            if not path.exists():
                out[key][c] = "missing"
                continue
            if (path / ".missing").exists() and not _has_parquet(path):
                out[key][c] = "missing"
            elif _has_parquet(path):
                out[key][c] = "partial"
            else:
                out[key][c] = "missing"
    return out


def query_transfer_hops(
    *,
    address: str,
    chains: list[str],
    window_from_ms: int,
    window_to_ms: int,
    limit: int = 500,
) -> tuple[list[dict], dict]:
    """Read transfer_hop rows involving address. Uses duckdb subprocess/venv when present."""
    addr = (address or "").lower()
    honesty: list[str] = []
    rows: list[dict] = []
    cov = coverage_for_chains(chains)
    paths: list[str] = []
    for c in chains:
        if cov["transfers"].get(c) == "missing":
            continue
        root = HOPS_ROOT / c / "transfers"
        # Prefer glob path for duckdb
        if root.exists():
            paths.append(str(root / "**" / "*.parquet"))
    if not paths:
        return [], {"transfers": "missing", "honesty": ["hops_empty_until_backfill"]}

    try:
        rows = _duckdb_transfers(paths, addr, window_from_ms, window_to_ms, limit)
    except Exception as e:
        honesty.append(f"hops_read_error:{type(e).__name__}")
        return [], {"transfers": "partial", "honesty": honesty}

    status = "ok" if rows else "partial"
    return rows, {"transfers": status, "honesty": honesty, "n": len(rows)}


def _duckdb_transfers(paths, addr, t0, t1, limit) -> list[dict]:
    """Read hop parquet without pandas (fetchall + describe)."""
    import duckdb
    from hops_to_chart_markers import _to_ms

    con = duckdb.connect()
    out: list[dict] = []
    for g in paths:
        for q in (
            f"""SELECT * FROM read_parquet('{g}', union_by_name=true)
                WHERE lower(CAST("from" AS VARCHAR)) = ? OR lower(CAST("to" AS VARCHAR)) = ?
                LIMIT {int(limit) * 4}""",
            f"""SELECT * FROM read_parquet('{g}', union_by_name=true)
                WHERE lower(CAST(from_addr AS VARCHAR)) = ? OR lower(CAST("to" AS VARCHAR)) = ?
                LIMIT {int(limit) * 4}""",
        ):
            try:
                cur = con.execute(q, [addr, addr])
                cols = [d[0] for d in cur.description]
                for tup in cur.fetchall():
                    rec = dict(zip(cols, tup))
                    ms = _to_ms(rec.get("ts") or rec.get("timestamp"))
                    if ms is None:
                        continue
                    if t0 and ms < t0:
                        continue
                    if t1 and ms > t1:
                        continue
                    row = dict(rec)
                    row["from"] = str(rec.get("from") or rec.get("from_addr") or "").lower()
                    row["to"] = str(rec.get("to") or "").lower()
                    row["ts"] = ms
                    if "chain" not in row or not row["chain"]:
                        # infer from path segment …/hops/{chain}/transfers/
                        for part in str(g).split("/"):
                            if part in ("arbitrum","robinhood","base","polygon","ethereum","solana","optimism","linea","arc","unichain"):
                                row["chain"] = part
                                break
                    out.append(row)
                    if len(out) >= limit:
                        out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
                        return out
                break  # first successful query shape
            except Exception:
                continue
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def perceive_track(
    *,
    seed: str,
    chains: list[str] | None = None,
    window_from_ms: int | None = None,
    window_to_ms: int | None = None,
    kinds: list[str] | None = None,
    limit: int = 500,
    label_fn: Callable[[str], Optional[dict]] | None = None,
    transfers_override: list[dict] | None = None,
    swaps_override: list[dict] | None = None,
    bridges_override: list[dict] | None = None,
) -> dict[str, Any]:
    chains = chains or ["arbitrum", "robinhood", "base", "polygon", "ethereum"]
    t1 = window_to_ms or _now_ms()
    t0 = window_from_ms or (t1 - 7 * 86400_000)
    kinds = kinds or ["transfer", "swap", "bridge"]

    honesty: list[str] = []
    cov_fs = coverage_for_chains(chains)

    transfers: list[dict] = list(transfers_override or [])
    swaps: list[dict] = list(swaps_override or [])
    bridges: list[dict] = list(bridges_override or [])

    if transfers_override is None and "transfer" in kinds:
        rows, meta = query_transfer_hops(
            address=seed, chains=chains, window_from_ms=t0, window_to_ms=t1, limit=limit
        )
        transfers = rows
        honesty.extend(meta.get("honesty") or [])
        # Sol/Arb/RH priority: tip→hot fills when hops remote-only / empty
        if not transfers and tip_hot_fills_for_address is not None:
            hot_chains = [c for c in chains if c in ("arbitrum", "robinhood", "solana")]
            if hot_chains:
                hrows, hmeta = tip_hot_fills_for_address(
                    seed, chains=hot_chains, window_from_ms=t0, window_to_ms=t1, limit=limit
                )
                if hrows:
                    transfers = hrows
                    honesty.append("hops_via_tip_hot_fallback")
                    honesty.extend(hmeta.get("honesty") or [])
                else:
                    honesty.extend(hmeta.get("honesty") or [])

    # swaps/bridges: only if override or we later add readers — honest missing default
    cov = {
        "hops": {
            "transfers": "ok"
            if transfers
            else ("partial" if any(v == "partial" for v in cov_fs["transfers"].values()) else "missing"),
            "swaps": "ok" if swaps else "missing",
            "bridges": "ok" if bridges else "missing",
        },
        "perChain": cov_fs,
    }
    if not swaps:
        honesty.append("swaps_missing")
    if not bridges:
        honesty.append("bridges_missing")
    if not transfers:
        honesty.append("hops_empty_window")

    return {
        "schema": SCHEMA_PERCEIVE,
        "subject": {"kind": "wallet", "id": (seed or "").lower()},
        "window": {"from": t0, "to": t1},
        "chains": chains,
        "hops": {"transfers": transfers, "swaps": swaps, "bridges": bridges},
        "coverage": cov,
        "honesty": honesty,
        "labelFnBound": bool(label_fn),
    }


def plan_track(perceive: dict, *, intent: dict | None = None) -> dict[str, Any]:
    """Autonomous judgment: prefer hops paint; suspect filter; asset vs wallet route."""
    intent = intent or {}
    hops = perceive.get("hops") or {}
    n = len(hops.get("transfers") or []) + len(hops.get("swaps") or []) + len(hops.get("bridges") or [])
    strategy = "paint_hops" if n else "honest_empty"
    # If only dump/suspect asked and hops thin, still paint with suspectOnly filter
    return {
        "schema": SCHEMA_PLAN,
        "mode": MODE,
        "strategy": strategy,
        "suspectOnly": bool(intent.get("suspectOnly")),
        "replay": bool(intent.get("replay")),
        "periodMs": int(intent.get("periodMs") or 60_000),
        "workspace": {
            "seed": (perceive.get("subject") or {}).get("id"),
            "route": f"#/console/workspace/wallet:{(perceive.get('subject') or {}).get('id')}",
        },
        "reasons": [
            f"hop_rows={n}",
            f"coverage={(perceive.get('coverage') or {}).get('hops')}",
        ],
    }


def act_track(
    perceive: dict,
    plan: dict,
    *,
    label_fn=None,
    asset_symbol: str | None = None,
) -> dict[str, Any]:
    hops = perceive.get("hops") or {}
    paint = hops_to_chart_markers(
        seed=(perceive.get("subject") or {}).get("id") or "",
        transfers=hops.get("transfers"),
        swaps=hops.get("swaps"),
        bridges=hops.get("bridges"),
        period_ms=int(plan.get("periodMs") or 60_000),
        label_fn=label_fn,
        suspect_only=bool(plan.get("suspectOnly")),
        coverage=(perceive.get("coverage") or {}).get("hops"),
    )
    route = (plan.get("workspace") or {}).get("route")
    if asset_symbol:
        route = f"#/console/workspace/{asset_symbol}"
    act = build_track_act(
        paint,
        workspace_route=route or "#/console/workspace",
        intent=plan,
    )
    act["plan"] = {k: plan[k] for k in ("strategy", "suspectOnly", "replay", "reasons") if k in plan}
    act["perceiveRef"] = {
        "schema": perceive.get("schema"),
        "honesty": perceive.get("honesty"),
        "coverage": perceive.get("coverage"),
        "nTransfers": len(hops.get("transfers") or []),
    }
    return act


def run_track_loop(
    *,
    text: str | None = None,
    seed: str | None = None,
    chains: list[str] | None = None,
    window_from_ms: int | None = None,
    window_to_ms: int | None = None,
    label_fn=None,
    asset_symbol: str | None = None,
    transfers_override=None,
    swaps_override=None,
    bridges_override=None,
) -> dict[str, Any]:
    intent = detect_track_intent(text or "") or {}
    if seed:
        intent["seedAddress"] = seed
    addr = intent.get("seedAddress") or seed
    if not addr:
        return {
            "ok": False,
            "error": "track_seed_required",
            "hint": "Pass a wallet seed (0x…) or include one in the ask.",
            "mode": MODE,
        }
    perc = perceive_track(
        seed=addr,
        chains=chains,
        window_from_ms=window_from_ms,
        window_to_ms=window_to_ms,
        label_fn=label_fn,
        transfers_override=transfers_override,
        swaps_override=swaps_override,
        bridges_override=bridges_override,
    )
    plan = plan_track(perc, intent=intent)
    act = act_track(perc, plan, label_fn=label_fn, asset_symbol=asset_symbol)
    return {"ok": True, "mode": MODE, "intent": intent, "perceive": perc, "plan": plan, "act": act}
