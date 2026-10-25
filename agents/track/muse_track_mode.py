#!/usr/bin/env python3
"""Muse track mode — paint hops on TickChart; never address-list in chat.

Verbs (chart-locked):
  track | follow_hop | suspect_only | replay

Forbidden by default:
  chat address walls, Bubble/Wallet Map, Flow-only page, connected=same-person claims.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from hops_to_chart_markers import hops_to_chart_markers

MODE = "track"
SCHEMA_ACT = "ticking.muse.act.track.v1"

# NL → track intents
_TRACK_RE = re.compile(
    r"(이\s*돈|where\s*(did|has)?\s*(the\s*)?(money|funds?|loot)\b|"
    r"follow\s*(the\s*)?(hop|trail|money)|track\s*(wallet|funds?|hack)|"
    r"자금\s*추적|해킹.*어디로|어디로\s*갔|money\s*flow\s*from|"
    r"follow\s*next\s*hop|next\s*hop)",
    re.I,
)
_SUSPECT_RE = re.compile(
    r"(의심|suspect|dump\s*only|large\s*out|whale\s*only|누가\s*팔)",
    re.I,
)
_REPLAY_RE = re.compile(r"(replay|timeline|재생|story|bitquery)", re.I)
_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def detect_track_intent(text: str) -> Optional[dict[str, Any]]:
    q = (text or "").strip()
    if not q:
        return None
    if not (_TRACK_RE.search(q) or _SUSPECT_RE.search(q)):
        return None
    addrs = _ADDR_RE.findall(q)
    return {
        "mode": MODE,
        "suspectOnly": bool(_SUSPECT_RE.search(q)),
        "replay": bool(_REPLAY_RE.search(q)),
        "seedAddress": addrs[0].lower() if addrs else None,
        "raw": q[:400],
    }


def build_track_act(
    paint: dict,
    *,
    workspace_route: str,
    intent: dict | None = None,
    follow_hint: str = "follow next hop",
) -> dict[str, Any]:
    """Act payload: navigate workspace + paint markers. Chat = museSummary only."""
    intent = intent or {}
    markers = paint.get("markers") or []
    return {
        "schema": SCHEMA_ACT,
        "mode": MODE,
        "navigate": {
            "route": workspace_route,
            # one incident = one workspace
        },
        "paint": {
            "chartMarkers": markers,
            "tags": paint.get("tags") or [],
            "bands": paint.get("bands") or [],
            "sideColumns": paint.get("sideColumns") or [],
            "policy": paint.get("policy"),
            "schema": paint.get("schema"),
        },
        "muse": {
            # DEFAULT: no address list in chat
            "chat": paint.get("museSummary") or "Painted trail on chart.",
            "listAddresses": False,
            "beside": {"cta": follow_hint, "verb": "follow_hop"},
            "replay": bool(intent.get("replay")),
            "replayMs": 1500 if intent.get("replay") else 0,
        },
        "honesty": paint.get("honesty") or [],
        "coverage": paint.get("coverage") or {},
        "forbidden": [
            "bubble_map",
            "wallet_map_main",
            "flow_only_page",
            "connected_equals_same_person",
            "chat_address_wall",
        ],
    }


def follow_next_hop_act(paint: dict, current_address: str) -> dict[str, Any] | None:
    """Muse beside CTA: advance one hop chip → same workspace wallet multi-chart."""
    cur = (current_address or "").lower()
    for b in paint.get("bands") or []:
        if b.get("kind") == "hop" and (b.get("from") or "").lower() == cur:
            nxt = (b.get("to") or "").lower()
            if not nxt:
                continue
            return {
                "schema": SCHEMA_ACT,
                "mode": MODE,
                "navigate": {"route": f"#/console/workspace/wallet:{nxt}"},
                "paint": {
                    "focusChip": nxt,
                    "chartMarkers": paint.get("markers") or [],
                },
                "muse": {
                    "chat": f"Followed hop → chart for {_short(nxt)}",
                    "listAddresses": False,
                    "beside": {"cta": "follow next hop", "verb": "follow_hop"},
                },
            }
    return None


def _short(a: str) -> str:
    a = (a or "").lower()
    return a[:6] + "…" + a[-4:] if a.startswith("0x") and len(a) >= 10 else a


def compose_from_hops(
    *,
    seed: str,
    transfers=None,
    swaps=None,
    bridges=None,
    period_ms: int = 60_000,
    suspect_only: bool = False,
    label_fn=None,
    coverage=None,
    asset_symbol: str | None = None,
) -> dict[str, Any]:
    paint = hops_to_chart_markers(
        seed=seed,
        transfers=transfers,
        swaps=swaps,
        bridges=bridges,
        period_ms=period_ms,
        label_fn=label_fn,
        suspect_only=suspect_only,
        coverage=coverage,
    )
    # Prefer asset workspace if symbol known; else wallet workspace for seed
    if asset_symbol:
        route = f"#/console/workspace/{asset_symbol}"
    else:
        route = f"#/console/workspace/wallet:{(seed or '').lower()}"
    act = build_track_act(
        paint,
        workspace_route=route,
        intent={"suspectOnly": suspect_only},
    )
    return {"perceivePaint": paint, "act": act}
