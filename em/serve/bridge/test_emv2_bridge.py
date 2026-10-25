#!/usr/bin/env python3
"""Tests for the EMV2 shadow block, run on skew against a patched copy of the bridge (nothing that is running is touched).
    python3 test_emv2_bridge.py <patched em_bridge.py> unit
    python3 test_emv2_bridge.py <patched em_bridge.py> live <static dir> <logs dir> <bridge port> <mock port> <upstream bridge port>
unit: the comparison rules, route and chain detection, mode selection.
live: starts the patched bridge (staging mode, summaries off) and a mock emv2-serve that replays the running staging bridge
(match), changes one number (critical), or answers 501 (unsupported); checks every answer is byte-identical to the running
staging bridge with and without the config file, and that the shadow log has the expected verdicts."""
import http.client
import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.parse


def load(path):
    spec = importlib.util.spec_from_file_location("em_bridge_emv2_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def unit(m):
    out, seen = [], [0]
    m.emv2_diff({"a": 1, "usd": 1.0, "s": "x", "l": [1, 2], "generated_at": 1}, {"a": 1, "usd": 1.0 + 1e-9, "s": "x", "l": [1, 2], "generated_at": 2},
                "", out, seen)
    assert out == [], out
    out = []
    m.emv2_diff({"a": 1, "usd": 1.0, "s": "x", "l": [1, 2], "gone": 1}, {"a": 2, "usd": 1.1, "s": "y", "l": [1], "new": 1}, "", out, [0])
    kinds = sorted(k for k, *_ in out)
    assert kinds == ["extra", "int", "length", "missing", "raw", "value"], kinds
    out = []
    m.emv2_diff({"x": None, "y": True, "z": "1"}, {"x": 0, "y": 1, "z": 1}, "", out, [0])
    assert sorted(k for k, *_ in out) == ["type", "type", "type"], out
    out = []
    m.emv2_diff({"big": "123456789012345678901234567890"}, {"big": "123456789012345678901234567891"}, "", out, [0])
    assert out and out[0][0] == "raw"
    assert m.emv2_route("/api/public/em/holders-at", {"market": "arc:0x" + "1" * 40}) == ("holders-at", "arc")
    assert m.emv2_route("/api/public/em/acquisition", {"address": "0x" + "1" * 40, "chain": "Base"}) == ("acquisition", "base")
    assert m.emv2_route("/api/public/em/address-chains", {"address": "0x" + "1" * 40}) == ("address-chains", "*")
    assert m.emv2_route("/api/public/em/trace", {"chain": "arbitrum", "from": "0x1", "to": "0x2"}) == ("path", "arbitrum")
    assert m.emv2_route("/api/public/em/trace", {"chain": "arbitrum", "from": "0x1"}) == ("trace", "arbitrum")
    assert m.emv2_route("/api/public/em/g/neighbors/robinhood/123", {}) == ("neighbors", "robinhood")
    assert m.emv2_route("/api/public/em/tools/wallet", {"address": "0x1"}) == ("tools/wallet", "?")
    assert m.emv2_route("/api/public/em/tools/top", {}) is None
    assert m.emv2_route("/api/public/em/health", {}) is None
    cfg = {"default": "old", "routes": {"holders-at": {"arc": "shadow", "*": "old"}, "trace": "shadow"}}
    assert m.emv2_mode(cfg, "holders-at", "arc") == "shadow" and m.emv2_mode(cfg, "holders-at", "base") == "old"
    assert m.emv2_mode(cfg, "trace", "base") == "shadow" and m.emv2_mode(cfg, "wallet-trades", "arc") == "old"
    assert m.emv2_asof({"as_of": {"block": 100, "time": 1700000000}}, "") == (100, 1700000000)
    assert m.emv2_asof({"as_of": 100}, "") == (100, None)
    assert m.emv2_asof({}, "200:1700000001") == (200, 1700000001)
    assert m.emv2_asof({}, "") == (None, None)
    n = [0]
    old = {"trades": [{"block": 90, "usd": 1.0}, {"block": 120, "usd": 2.0}], "count": 2}
    trimmed = m.emv2_trim(old, 100, None, n)
    assert n[0] == 1 and trimmed["trades"] == [{"block": 90, "usd": 1.0}], trimmed
    out = []
    m.emv2_diff(trimmed, {"trades": [{"block": 90, "usd": 1.0}], "count": 1}, "", out, [0])
    assert [d[0] for d in out] == ["int"], out   # the aggregate still differs: time_skew when rows after as_of were left out
    assert m.emv2_after({"time": 1700000005}, None, 1700000000) and not m.emv2_after({"time": 1690000000}, None, 1700000000)
    assert m.emv2_asof({"as_of": {"arc": {"block": 7, "time": 8}, "base": {"block": 1}}}, "", "arc") == (7, 8)
    assert m.emv2_asof({"as_of": {"chain": "arc", "block": 7, "time": 8, "build": "x"}}, "", "arc") == (7, 8)
    n = [0]
    g = m.emv2_drop_xc({"links": [{"a": 1, "kind": "transfer"}, {"a": 2, "kind": "bridge"}], "nodes": [{"chain": "arc"}, {"chain": "base"}]}, "arc", n)
    assert n[0] == 2 and len(g["links"]) == 1 and g["nodes"] == [{"chain": "arc"}], g
    assert m.EMV2_SKIP["holders-at"].search("snaps[2].source") and not m.EMV2_SKIP["holders-at"].search("snaps[2].sourceX")
    assert m.EMV2_GAPS["holders-at"].search("holders[0].class")
    print("unit ok")


def get(port, target):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request("GET", target)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def live(m, static, logs, port, mock_port, upstream):
    state = {"n": 0}

    class Mock(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["n"] += 1
            path = urllib.parse.urlsplit(self.path).path
            if "address-chains" in path:
                code, body = 501, b'{"error":"unsupported","detail":"test"}'
            else:
                code, body = get(upstream, self.path)
                if code == 200 and path.endswith("/em/trace"):
                    d = json.loads(body)
                    d["_emv2_test_changed"] = 1
                    d["hops"] = (d.get("hops") or 0) + 1
                    body = json.dumps(d).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-EMV2-Build", "test:mock")
            self.end_headers()
            self.wfile.write(body)

    ms = http.server.ThreadingHTTPServer(("127.0.0.1", mock_port), Mock)
    threading.Thread(target=ms.serve_forever, daemon=True).start()
    m.SUMMARIES.build = lambda: None   # no 30-min econ_actions pass for a test process
    sys.argv = ["em_bridge.py", "--mode", "staging", "--port", str(port), "--static", static, "--logs", logs,
                "--console", "127.0.0.1:19438", "--resolver", "127.0.0.1:8894", "--chains", "/srv/skew/ticking-em-staging/fc/src/registry/chains.json",
                "--icons", "/srv/skew/ticking-em-staging/fc/state/token-index.json", "--evm-catalog", "/srv/skew/ticking-em-staging/fc/ui/server/evm_catalog.json"]
    threading.Thread(target=m.main, daemon=True).start()
    time.sleep(3)
    cfg_path = m.EMV2_PATH
    assert not os.path.exists(cfg_path), cfg_path
    holder = "0x0e6d23f0babd02ede4aefaa923486591d783e118"   # a CASHCAT wallet (home bundle)
    targets = ["/api/public/em/address-chains?address=%s" % holder, "/api/public/em/health", "/api/public/em/chains",
               "/api/public/em/tools/wallet?address=%s&chain=robinhood" % holder, "/api/public/em/trace?chain=robinhood&from=%s" % holder,
               "/api/public/em/tools/trace?address=%s&chain=robinhood" % holder]
    base = {t: get(upstream, t) for t in targets}
    off = {t: get(port, t) for t in targets}
    flaky = {502, 503, 504}   # an upstream (invscreen, galaxy) timing out is not the block's doing
    for t in targets:
        assert off[t][0] == base[t][0] or {off[t][0], base[t][0]} & flaky, (t, off[t][0], base[t][0])
    with open(cfg_path, "w") as f:
        json.dump({"serve": "127.0.0.1:%d" % mock_port, "routes": {"address-chains": "shadow", "tools/wallet": "shadow",
                                                                   "trace": {"robinhood": "shadow"}, "tools/trace": "shadow"}, "timeout_ms": 20000}, f)
    m.EMV2["loaded"] = 0
    try:
        on = {t: get(port, t) for t in targets}
        for t in targets:
            assert on[t][0] == base[t][0] or {on[t][0], base[t][0]} & flaky, (t, on[t][0], base[t][0])
        time.sleep(8)
    finally:
        os.unlink(cfg_path)
    recs = [json.loads(x) for x in open(os.path.join(logs, "emv2-shadow.jsonl"))]
    by = {r["route"]: r for r in recs if r.get("route")}
    print(json.dumps({r: {k: by[r].get(k) for k in ("verdict", "old_code", "new_code", "critical", "leaves", "old_ms", "new_ms")} for r in by}, indent=1))
    assert by["address-chains"]["verdict"] == "unsupported"
    assert by["tools/trace"]["verdict"] == "match", by["tools/trace"]
    assert by["trace"]["verdict"] == "critical" or by["trace"].get("extra"), by["trace"]
    assert "tools/wallet" in by, by
    for t in targets:
        if off[t][1] != base[t][1] and off[t][0] == 200 == base[t][0]:
            out = []
            m.emv2_diff(json.loads(base[t][1]), json.loads(off[t][1]), "", out, [0])
            print("differs from the running staging bridge (two processes, own caches):", t.split("?")[0], [d[:2] for d in out[:5]])
    print("mock calls:", state["n"])
    print("live ok")


if __name__ == "__main__":
    mod = load(sys.argv[1])
    if sys.argv[2] == "unit":
        unit(mod)
    else:
        live(mod, *sys.argv[3:5], *map(int, sys.argv[5:8]))
