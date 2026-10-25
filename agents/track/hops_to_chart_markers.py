#!/usr/bin/env python3
"""hop rows → TickChart chartMarkers paint (bands + chips + tags).

Product lock (2026-10-23): tracker body = chart only. No hub/map/address walls.
Closed paint vocab: tag | band | chip | side_column | solid|dashed|dotted.

Input: transfer_hop / swap_leg / bridge_hop dicts (schema: skew hops-schema).
Output: ticking.chartMarkers.track.v1 — FE paints on same candle time axis.

Honesty: empty hop window → markers=[] + honesty flag. Never invent CEX/bridge.
Connection ≠ same person → chip style dashed unless label_fn confirms entity.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Literal, Optional

SCHEMA = "ticking.chartMarkers.track.v1"
POLICY = "hops_to_chart_markers_v1"

Kind = Literal[
    "buy", "sell", "transfer", "swap", "bridge", "dump", "frozen", "cex_deposit"
]


def _nz(x) -> float:
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else 0.0
    except Exception:
        return 0.0


def _short(addr: str) -> str:
    a = (addr or "").lower()
    if a.startswith("0x") and len(a) >= 10:
        return a[:6] + "…" + a[-4:]
    if len(a) > 12:
        return a[:4] + "…" + a[-4:]
    return a or "?"


def _money(n: float) -> str:
    v = abs(_nz(n))
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.0f}"


def _to_ms(ts) -> Optional[int]:
    if ts is None:
        return None
    try:
        # datetime / pandas Timestamp
        if hasattr(ts, "timestamp"):
            return int(float(ts.timestamp()) * 1000)
        v = float(ts)
    except Exception:
        return None
    if v <= 0:
        return None
    # us → ms, s → ms
    if v > 1e14:  # microseconds
        return int(v / 1000)
    if v < 1e12:  # seconds
        return int(v * 1000)
    return int(v)


def _addr(x) -> str:
    if isinstance(x, dict):
        return (x.get("hash") or x.get("address") or x.get("from") or x.get("to") or "").lower()
    return (str(x) if x is not None else "").lower()


def _floor_to_candle(ts_ms: int, period_ms: int) -> int:
    p = max(int(period_ms or 1), 1)
    return (int(ts_ms) // p) * p


def _label_style(
    addr: str,
    label_fn: Callable[[str], Optional[dict]] | None,
) -> tuple[str, str, Literal["solid", "dashed"]]:
    """Return (display, label_class, style). Never claim same-person from connection."""
    a = (addr or "").lower()
    meta = None
    if label_fn and a:
        try:
            meta = label_fn(a)
        except Exception:
            meta = None
    if isinstance(meta, dict) and (meta.get("name") or meta.get("label")):
        name = str(meta.get("name") or meta.get("label"))
        klass = str(meta.get("class") or meta.get("kind") or "entity")
        # confirmed CEX / frozen / labeled entity → solid
        if klass in ("cex", "exchange", "frozen", "entity", "institution", "labeled"):
            return name, klass, "solid"
        return name, klass, "dashed"
    if isinstance(meta, str) and meta:
        return meta, "label", "solid"
    return _short(a), "wallet", "dashed"


def transfer_to_markers(
    rows: Iterable[dict],
    *,
    seed: str,
    period_ms: int = 60_000,
    label_fn: Callable[[str], Optional[dict]] | None = None,
    suspect_only: bool = False,
    large_usd: float = 10_000.0,
    max_tags: int = 200,
) -> list[dict]:
    """Point tags + hop bands for transfers from a seed wallet trail."""
    seed_a = (seed or "").lower()
    tags: list[dict] = []
    for r in rows or []:
        frm = _addr(r.get("from") or r.get("from_addr"))
        to = _addr(r.get("to"))
        ts = _to_ms(r.get("ts") or r.get("timestamp") or r.get("time"))
        if ts is None or not frm or not to:
            continue
        usd = _nz(r.get("usd"))
        side = "out" if frm == seed_a else ("in" if to == seed_a else "hop")
        if suspect_only:
            # dump / large outflow only — hide noise buys & small hops
            if side == "in":
                continue
            if usd < large_usd:
                continue
        who_from, _, style_from = _label_style(frm, label_fn)
        who_to, klass_to, style_to = _label_style(to, label_fn)
        # tip/hot fill side A/B → sell/buy tags on candle axis
        fill_side = str(r.get("side") or "").upper()
        kind: Kind = "transfer"
        if fill_side == "A":
            kind = "sell"
        elif fill_side == "B":
            kind = "buy"
        extra_chips: list[dict] = []
        if klass_to in ("cex", "exchange"):
            kind = "cex_deposit"
            extra_chips.append({
                "kind": "cex_deposit",
                "label": f"{klass_to} deposit",
                "address": to,
                "style": "solid",
                "confirmed": True,
            })
        if klass_to == "frozen" or (isinstance(r.get("label"), str) and "frozen" in str(r.get("label")).lower()):
            extra_chips.append({
                "kind": "frozen",
                "label": "frozen",
                "address": to,
                "style": "solid",
                "confirmed": True,
            })
        t_floor = _floor_to_candle(ts, period_ms)
        tags.append({
            "kind": kind,
            "paint": "tag",
            "time": t_floor,
            "timeExact": ts,
            "tEnd": t_floor,
            "chain": r.get("chain") or "",
            "tx": r.get("tx") or r.get("tx_hash") or "",
            "from": frm,
            "to": to,
            "token": (r.get("token") or "").lower(),
            "usd": round(usd, 2) if usd else None,
            "label": f"{who_from} → {who_to}" + (f" {_money(usd)}" if usd else ""),
            "style": style_from if side == "out" else style_to,
            "confirmed": style_to == "solid" or style_from == "solid",
            "chips": [
                {
                    "kind": "wallet",
                    "label": who_from,
                    "address": frm,
                    "style": style_from,
                    "route": f"#/console/workspace/wallet:{frm}",
                },
                {
                    "kind": "wallet",
                    "label": who_to,
                    "address": to,
                    "style": style_to,
                    "route": f"#/console/workspace/wallet:{to}",
                },
                *extra_chips,
            ],
            "click": {"tx": r.get("tx") or r.get("tx_hash") or "", "from": frm, "to": to},
        })
        if len(tags) >= max_tags:
            break
    return tags


def build_hop_bands(
    tags: list[dict],
    *,
    seed: str,
    max_hops: int = 8,
) -> list[dict]:
    """A→B→C hop stack as interval bands + wallet chips on the SAME chart."""
    seed_a = (seed or "").lower()
    # order by time; walk outs from seed then successive counterparties
    ordered = sorted(tags, key=lambda m: m.get("timeExact") or m.get("time") or 0)
    path: list[str] = [seed_a] if seed_a else []
    bands: list[dict] = []
    cursor = seed_a
    for m in ordered:
        if not cursor:
            break
        if (m.get("from") or "").lower() != cursor:
            continue
        nxt = (m.get("to") or "").lower()
        if not nxt or nxt == cursor:
            continue
        path.append(nxt)
        t0 = m.get("timeExact") or m.get("time")
        # band spans this hop event; widen slightly for visibility (same candle ok)
        bands.append({
            "kind": "hop",
            "paint": "band",
            "time": m.get("time"),
            "timeExact": t0,
            "tEnd": m.get("time"),
            "from": cursor,
            "to": nxt,
            "tx": m.get("tx") or "",
            "chain": m.get("chain") or "",
            "label": f"hop {len(path)-1}: {_short(cursor)}→{_short(nxt)}",
            "style": "dashed",  # association until labeled same entity
            "confirmed": False,
            "path": list(path),
            "chips": [
                {
                    "kind": "wallet",
                    "label": _short(cursor),
                    "address": cursor,
                    "style": "dashed",
                    "route": f"#/console/workspace/wallet:{cursor}",
                },
                {
                    "kind": "wallet",
                    "label": _short(nxt),
                    "address": nxt,
                    "style": "dashed",
                    "route": f"#/console/workspace/wallet:{nxt}",
                },
            ],
            "click": {"tx": m.get("tx") or "", "from": cursor, "to": nxt},
        })
        cursor = nxt
        if len(bands) >= max_hops:
            break
    return bands


def swaps_to_markers(
    rows: Iterable[dict],
    *,
    period_ms: int = 60_000,
    max_tags: int = 100,
) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        ts = _to_ms(r.get("ts") or r.get("timestamp"))
        if ts is None:
            continue
        side = (r.get("side") or "").lower()
        kind: Kind = "buy" if side == "buy" else ("sell" if side == "sell" else "swap")
        t_floor = _floor_to_candle(ts, period_ms)
        usd = _nz(r.get("usd"))
        out.append({
            "kind": kind,
            "paint": "tag",
            "time": t_floor,
            "timeExact": ts,
            "tEnd": t_floor,
            "chain": r.get("chain") or "",
            "tx": r.get("tx") or "",
            "token": (r.get("token_out") if kind == "buy" else r.get("token_in") or r.get("token") or "").lower(),
            "usd": round(usd, 2) if usd else None,
            "label": f"{kind} {_money(usd)}" if usd else kind,
            "style": "solid",
            "confirmed": True,
            "click": {"tx": r.get("tx") or "", "from": "", "to": ""},
        })
        if len(out) >= max_tags:
            break
    return out


def bridges_to_markers(
    rows: Iterable[dict],
    *,
    period_ms: int = 60_000,
    max_tags: int = 50,
) -> tuple[list[dict], list[dict]]:
    """Dotted bridge bands + SRC→DST chips; side_column hints for dest chain charts."""
    bands: list[dict] = []
    columns: list[dict] = []
    seen_dst: set[str] = set()
    for r in rows or []:
        t0 = _to_ms(r.get("ts_src") or r.get("ts") or r.get("timestamp"))
        if t0 is None:
            continue
        t1 = _to_ms(r.get("ts_dst")) or t0
        src = (r.get("src_chain") or r.get("chain") or "").lower()
        dst = (r.get("dst_chain") or "").lower()
        status = (r.get("status") or "pending").lower()
        frm = _addr(r.get("from") or r.get("from_addr"))
        to = _addr(r.get("to"))
        label = f"{src.upper() or '?'}→{dst.upper() or '?'}"
        bands.append({
            "kind": "bridge",
            "paint": "band",
            "time": _floor_to_candle(t0, period_ms),
            "timeExact": t0,
            "tEnd": _floor_to_candle(t1, period_ms),
            "style": "dotted",
            "confirmed": status == "complete",
            "bridgeStatus": status,
            "srcChain": src,
            "dstChain": dst,
            "from": frm,
            "to": to,
            "tx": r.get("tx_src") or r.get("tx") or "",
            "txDst": r.get("tx_dst") or "",
            "label": label,
            "chips": [{
                "kind": "bridge",
                "label": label,
                "style": "dotted" if status != "complete" else "solid",
                "srcChain": src,
                "dstChain": dst,
                "route": f"#/console/workspace/wallet:{to}" if to else None,
            }],
            "click": {"tx": r.get("tx_src") or "", "from": frm, "to": to},
        })
        if dst and dst not in seen_dst:
            seen_dst.add(dst)
            columns.append({
                "paint": "side_column",
                "chain": dst,
                "label": dst.upper(),
                "reason": "bridge_dest",
                "seedTx": r.get("tx_src") or "",
            })
        if len(bands) >= max_tags:
            break
    return bands, columns


def hops_to_chart_markers(
    *,
    seed: str,
    transfers: list[dict] | None = None,
    swaps: list[dict] | None = None,
    bridges: list[dict] | None = None,
    period_ms: int = 60_000,
    label_fn: Callable[[str], Optional[dict]] | None = None,
    suspect_only: bool = False,
    large_usd: float = 10_000.0,
    coverage: dict | None = None,
) -> dict[str, Any]:
    """Assemble paint payload for one incident workspace."""
    honesty: list[str] = []
    cov = dict(coverage or {})
    tags = transfer_to_markers(
        transfers or [],
        seed=seed,
        period_ms=period_ms,
        label_fn=label_fn,
        suspect_only=suspect_only,
        large_usd=large_usd,
    )
    if swaps:
        tags.extend(swaps_to_markers(swaps, period_ms=period_ms))
    elif cov.get("swaps") in (None, "missing"):
        honesty.append("swaps_missing")
        cov.setdefault("swaps", "missing")

    bridge_bands, side_columns = [], []
    if bridges:
        bridge_bands, side_columns = bridges_to_markers(bridges, period_ms=period_ms)
    elif cov.get("bridges") in (None, "missing"):
        honesty.append("bridges_missing")
        cov.setdefault("bridges", "missing")

    if not transfers and cov.get("transfers") in (None, "missing", "partial"):
        if not tags:
            honesty.append("hops_empty_window")
        cov.setdefault("transfers", cov.get("transfers") or "missing")

    bands = build_hop_bands(tags, seed=seed)
    bands.extend(bridge_bands)

    # Prefer hop bands + tags; dump-style large outs already in tags when suspect_only
    markers = [*tags, *bands]
    markers.sort(key=lambda m: (m.get("timeExact") or m.get("time") or 0))

    return {
        "schema": SCHEMA,
        "policy": POLICY,
        "seed": (seed or "").lower(),
        "periodMs": int(period_ms),
        "suspectOnly": bool(suspect_only),
        "markers": markers,
        "tags": [m for m in markers if m.get("paint") == "tag"],
        "bands": [m for m in markers if m.get("paint") == "band"],
        "sideColumns": side_columns,
        "coverage": cov,
        "honesty": honesty,
        # chat must NOT dump addresses — FE/Muse use this one-liner only
        "museSummary": _muse_summary(markers, bands, side_columns, honesty),
    }


def _muse_summary(markers, bands, side_columns, honesty) -> str:
    n_tag = sum(1 for m in markers if m.get("paint") == "tag")
    n_band = len(bands)
    if not n_tag and not n_band:
        gaps = (", ".join(honesty) if honesty else "no hop rows")
        return f"No hop paint in this window (honest empty · {gaps})."
    parts = [f"Painted {n_tag} tags · {n_band} hop/bridge bands on chart"]
    if side_columns:
        chains = ",".join(sorted({c.get("chain") for c in side_columns if c.get("chain")}))
        parts.append(f"side columns: {chains}")
    if honesty:
        parts.append("gaps: " + ", ".join(honesty))
    return " · ".join(parts)
