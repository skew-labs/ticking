#!/usr/bin/env python3
"""Track Perceive→Plan→Act HTTP (chart-only tracker).

Default bind 127.0.0.1:19480 (replaces junk assistant-wallet-preview).

Endpoints:
  GET  /healthz
  GET  /v1/perceive?subject=wallet&id=0x…&chains=arbitrum,robinhood,solana&window=from,to
  POST /v1/plan   { workspace_id?, intent, perceive_ref? | perceive }
  POST /v1/act    { perceive, plan } or { seed, intent }
  POST /v1/track  { text?, seed?, chains?, suspectOnly?, assetSymbol? }  # full loop
  GET  /v1/chartMarkers?seed=0x…&chains=…   # paint-only shortcut

Chat contract: act.muse.listAddresses=false; muse.chat = museSummary only.
"""
from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from em_track_autonomy import act_track, perceive_track, plan_track, run_track_loop
from hops_to_chart_markers import hops_to_chart_markers
from muse_track_mode import detect_track_intent, follow_next_hop_act

HOST = os.environ.get("TRACK_BIND", "127.0.0.1")
PORT = int(os.environ.get("TRACK_PORT", "19480"))


def _json(handler: BaseHTTPRequestHandler, code: int, body: dict):
    raw = json.dumps(body, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode() or "{}")
    except Exception:
        return {}


def _parse_window(qs) -> tuple[int | None, int | None]:
    w = (qs.get("window") or [None])[0]
    if not w:
        return None, None
    if "," in w:
        a, b = w.split(",", 1)
        try:
            return int(float(a)), int(float(b))
        except Exception:
            return None, None
    return None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # keep journal quiet; systemd captures stderr
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path in ("/healthz", "/"):
                return _json(self, 200, {
                    "ok": True,
                    "service": "em-track-chart",
                    "schema": "ticking.perceive.track.v1",
                    "paint": "ticking.chartMarkers.track.v1",
                    "product": "chart_only_tracker",
                    "forbidden": [
                        "bubble_map", "wallet_map_main", "flow_only_page",
                        "connected_equals_same_person", "chat_address_wall",
                    ],
                })
            if u.path == "/v1/perceive":
                seed = (qs.get("id") or qs.get("seed") or [""])[0]
                if not seed:
                    return _json(self, 400, {"ok": False, "error": "id_required"})
                chains = [c.strip() for c in ((qs.get("chains") or ["arbitrum,robinhood,solana"])[0]).split(",") if c.strip()]
                t0, t1 = _parse_window(qs)
                limit = int((qs.get("limit") or ["500"])[0])
                body = perceive_track(seed=seed, chains=chains, window_from_ms=t0, window_to_ms=t1, limit=limit)
                return _json(self, 200, body)
            if u.path == "/v1/chartMarkers":
                seed = (qs.get("seed") or qs.get("id") or [""])[0]
                if not seed:
                    return _json(self, 400, {"ok": False, "error": "seed_required"})
                chains = [c.strip() for c in ((qs.get("chains") or ["arbitrum,robinhood,solana"])[0]).split(",") if c.strip()]
                suspect = (qs.get("suspectOnly") or ["0"])[0] in ("1", "true", "yes")
                perc = perceive_track(seed=seed, chains=chains)
                hops = perc.get("hops") or {}
                paint = hops_to_chart_markers(
                    seed=seed,
                    transfers=hops.get("transfers"),
                    swaps=hops.get("swaps"),
                    bridges=hops.get("bridges"),
                    suspect_only=suspect,
                    coverage=(perc.get("coverage") or {}).get("hops"),
                )
                paint["perceiveHonesty"] = perc.get("honesty")
                return _json(self, 200, paint)
            return _json(self, 404, {"ok": False, "error": "not_found"})
        except Exception as e:
            return _json(self, 500, {"ok": False, "error": type(e).__name__, "detail": str(e), "trace": traceback.format_exc()[-800:]})

    def do_POST(self):
        u = urlparse(self.path)
        data = _read_json(self)
        try:
            if u.path == "/v1/plan":
                perc = data.get("perceive")
                if not perc and data.get("seed"):
                    perc = perceive_track(seed=data["seed"], chains=data.get("chains"))
                if not perc:
                    return _json(self, 400, {"ok": False, "error": "perceive_or_seed_required"})
                intent = data.get("intent") or detect_track_intent(data.get("text") or "") or {}
                return _json(self, 200, plan_track(perc, intent=intent))
            if u.path == "/v1/act":
                if data.get("perceive") and data.get("plan"):
                    act = act_track(data["perceive"], data["plan"], asset_symbol=data.get("assetSymbol"))
                    return _json(self, 200, act)
                # follow next hop
                if data.get("followHop") and data.get("paint") and data.get("current"):
                    nxt = follow_next_hop_act(data["paint"], data["current"])
                    return _json(self, 200, nxt or {"ok": False, "error": "no_next_hop"})
                loop = run_track_loop(
                    text=data.get("text"),
                    seed=data.get("seed"),
                    chains=data.get("chains"),
                    asset_symbol=data.get("assetSymbol"),
                    transfers_override=data.get("transfers"),
                    swaps_override=data.get("swaps"),
                    bridges_override=data.get("bridges"),
                )
                return _json(self, 200, loop.get("act") if loop.get("ok") else loop)
            if u.path == "/v1/track":
                intent = data.get("intent") or {}
                if data.get("suspectOnly") is not None:
                    intent["suspectOnly"] = data["suspectOnly"]
                # allow text to set suspect
                det = detect_track_intent(data.get("text") or "")
                if det:
                    intent = {**det, **intent}
                loop = run_track_loop(
                    text=data.get("text"),
                    seed=data.get("seed") or intent.get("seedAddress"),
                    chains=data.get("chains") or ["arbitrum", "robinhood", "solana"],
                    asset_symbol=data.get("assetSymbol"),
                    transfers_override=data.get("transfers"),
                    swaps_override=data.get("swaps"),
                    bridges_override=data.get("bridges"),
                )
                # force intent flags onto act if present
                if loop.get("ok") and intent.get("suspectOnly") and not (loop.get("intent") or {}).get("suspectOnly"):
                    # re-run with override transfers from perceive
                    perc = loop["perceive"]
                    plan = plan_track(perc, intent={**intent, "suspectOnly": True})
                    loop["plan"] = plan
                    loop["act"] = act_track(perc, plan, asset_symbol=data.get("assetSymbol"))
                return _json(self, 200, loop)
            return _json(self, 404, {"ok": False, "error": "not_found"})
        except Exception as e:
            return _json(self, 500, {"ok": False, "error": type(e).__name__, "detail": str(e), "trace": traceback.format_exc()[-800:]})


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"em-track-chart listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
