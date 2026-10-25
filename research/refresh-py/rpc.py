#!/usr/bin/env python3
"""Minimal JSON-RPC + Multicall3 client (stdlib only) over a pool of public endpoints.

Endpoints: configure(urls) or the RPC env var (comma-separated). Each HTTP request goes to the next endpoint in turn
that is not cooling down. A 429 / 5xx / timeout / rate-limit JSON-RPC error puts that endpoint on a cooldown
(exponential in its failure streak, jittered, at least Retry-After) and the call moves on to another endpoint; when all
are cooling down it sleeps until the first one is free. A call gives up after RPC_MAX_TRIES requests (RpcError).

Budgets: RPC_BUDGET caps top-level calls per process. RPC_RUN_FILE + RPC_RUN_BUDGET (set by refresh.py) cap HTTP
requests, retries included, across all processes of one refresh run (RpcBudgetExceeded) and collect per-host counters
in that file. Only host names are logged or recorded (endpoint URLs may carry keys).

Multicall3 (0xcA11bde05977b3631167028862bE2a173976CA11) aggregate3((address,bool,bytes)[])
packs up to `chunk` sub-calls into ONE eth_call.
"""
import fcntl
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MC3 = "0xca11bde05977b3631167028862be2a173976ca11"
CALLS = {"n": 0}
BUDGET = int(os.environ.get("RPC_BUDGET", "1000"))
MAX_TRIES = int(os.environ.get("RPC_MAX_TRIES", "10"))
BACKOFF_BASE, BACKOFF_CAP = 1.0, 60.0
RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
RATE_WORDS = ("rate limit", "rate-limit", "too many", "limit exceeded", "exceeded the quota", "capacity", "timeout",
              "timed out", "try again", "temporarily", "overloaded", "busy")

SEL = {  # 4-byte selectors (keccak of signature)
    "decimals()": "313ce567", "symbol()": "95d89b41", "name()": "06fdde03",
    "totalSupply()": "18160ddd", "owner()": "8da5cb5b", "description()": "7284e416",
    "latestAnswer()": "50d25bcd", "aggregator()": "245a7bfc", "baseToken()": "c55dae63",
    "underlying()": "6f307dc3", "asset()": "38d52e0f", "token0()": "0dfe1681",
    "token1()": "d21220a7", "balanceOf(address)": "70a08231", "getReserves()": "0902f1ac",
    "UNDERLYING_ASSET_ADDRESS()": "b16a19de", "factory()": "c45a0155",
}


class RpcError(RuntimeError):
    """The pool gave up on a call (every retry failed, or the run budget is spent)."""


class RpcBudgetExceeded(RpcError):
    pass


POOL = {"urls": [], "next_ok": {}, "streak": {}, "i": 0}


def host(url):
    return urllib.parse.urlsplit(url).hostname or "?"


def configure(urls):
    urls = [u.strip() for u in (urls.split(",") if isinstance(urls, str) else urls) if u and u.strip()]
    POOL.update({"urls": urls, "next_ok": {u: 0.0 for u in urls}, "streak": {u: 0 for u in urls}, "i": 0})


configure(os.environ.get("RPC", "http://127.0.0.1:8547"))


def _log(msg):
    print(f"rpc: {msg}", file=sys.stderr, flush=True)


def _account(h, field, secs=0.0):
    """Per-run counters shared by every process of a refresh run (flock'd JSON file)."""
    path = os.environ.get("RPC_RUN_FILE")
    if not path:
        return
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        d = json.loads(raw) if raw.strip() else {}
        d.setdefault("requests", 0)
        d["budget"] = int(os.environ.get("RPC_RUN_BUDGET", "0") or 0)
        hs = d.setdefault("by_host", {}).setdefault(h, {"req": 0, "ok": 0, "rate_limited": 0, "errors": 0, "cooldown_s": 0.0})
        if field == "req":
            if d["budget"] and d["requests"] >= d["budget"]:
                d["budget_exhausted"] = True
                f.seek(0)
                f.truncate()
                f.write(json.dumps(d))
                raise RpcBudgetExceeded(f"run RPC budget of {d['budget']} requests spent")
            d["requests"] += 1
        hs[field] = hs.get(field, 0) + 1
        hs["cooldown_s"] = round(hs.get("cooldown_s", 0) + secs, 1)
        f.seek(0)
        f.truncate()
        f.write(json.dumps(d))


def _pick():
    urls = POOL["urls"]
    if not urls:
        raise RpcError("no RPC endpoints configured")
    now = time.time()
    for k in range(len(urls)):
        u = urls[(POOL["i"] + k) % len(urls)]
        if POOL["next_ok"][u] <= now:
            POOL["i"] = (POOL["i"] + k + 1) % len(urls)
            return u
    u = min(urls, key=lambda x: POOL["next_ok"][x])
    time.sleep(max(0.0, POOL["next_ok"][u] - now))
    POOL["i"] = (urls.index(u) + 1) % len(urls)
    return u


def _cool(u, retry_after=None):
    POOL["streak"][u] += 1
    d = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (POOL["streak"][u] - 1)) * random.uniform(0.5, 1.0)
    if retry_after:
        d = max(d, min(float(retry_after), 120.0))
    POOL["next_ok"][u] = time.time() + d
    return d


def _rate_error(d):
    """JSON-RPC error object(s) that mean 'slow down / try elsewhere' -> message, a hard error -> ('hard', message)."""
    errs = [x.get("error") for x in (d if isinstance(d, list) else [d]) if isinstance(x, dict) and x.get("error")]
    if not errs:
        return None
    e = errs[0]
    msg = (e.get("message") if isinstance(e, dict) else str(e)) or "?"
    code = e.get("code") if isinstance(e, dict) else None
    if code in (-32005, 429) or any(w in msg.lower() for w in RATE_WORDS):
        return ("rate", f"JSON-RPC {code}: {msg[:160]}")
    return ("hard", f"JSON-RPC {code}: {msg[:160]}")


def post(payload, timeout=120):
    """POST one JSON-RPC request (or batch) through the pool; returns the decoded response."""
    body = json.dumps(payload).encode()
    hard = {}
    last = "?"
    for attempt in range(MAX_TRIES):
        u = _pick()
        h = host(u)
        _account(h, "req")
        retry_after = None
        kind = "errors"
        try:
            req = urllib.request.Request(u, body, {"Content-Type": "application/json", "User-Agent": "em-refresh/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            err = _rate_error(d)
            if err is None:
                POOL["streak"][u] = 0
                _account(h, "ok")
                return d
            if err[0] == "rate":
                kind, last = "rate_limited", f"{h}: {err[1]}"
            else:
                last = f"{h}: {err[1]}"
                hard[h] = last
        except urllib.error.HTTPError as e:
            last = f"{h}: HTTP {e.code}"
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            if e.code == 429:
                kind = "rate_limited"
            elif e.code not in RETRY_HTTP:
                hard[h] = last          # 4xx: this endpoint will not take the request (size / method / auth)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = f"{h}: {type(e).__name__}: {str(getattr(e, 'reason', e))[:120]}"
        secs = _cool(u, retry_after)
        _account(h, kind, secs)
        if len(hard) >= len(set(host(x) for x in POOL["urls"])):
            raise RpcError(f"every endpoint rejected the request: {'; '.join(hard.values())}")
        if attempt < MAX_TRIES - 1:
            _log(f"{last}; retry {attempt + 1}/{MAX_TRIES - 1} (cooldown {secs:.1f}s on {h})")
    raise RpcError(f"gave up after {MAX_TRIES} requests: {last}")


def rpc(method, params):
    CALLS["n"] += 1
    if CALLS["n"] > BUDGET:
        raise RpcBudgetExceeded("process RPC budget exceeded")
    d = post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "result" not in d:
        raise RpcError(f"no result in response: {str(d)[:160]}")
    return d["result"]


def enc_u(v):
    return "%064x" % v


def enc_addr(a):
    return "0" * 24 + a.lower().replace("0x", "")


def aggregate3(calls, block="latest", chunk=150):
    """calls: list of (target, calldata_hex_no0x). returns list of (ok, bytes)."""
    out = []
    for i in range(0, len(calls), chunk):
        part = calls[i:i + chunk]
        # abi encode aggregate3((address target,bool allowFailure,bytes callData)[])
        n = len(part)
        head = "0000000000000000000000000000000000000000000000000000000000000020" + enc_u(n)
        elems = []
        for t, cd in part:
            cdb = bytes.fromhex(cd)
            pad = (32 - len(cdb) % 32) % 32
            e = enc_addr(t) + enc_u(1) + enc_u(0x60) + enc_u(len(cdb)) + (cdb + b"\0" * pad).hex()
            elems.append(e)
        offs = []
        pos = 32 * n
        for e in elems:
            offs.append(enc_u(pos))
            pos += len(e) // 2
        data = "0x82ad56cb" + head + "".join(offs) + "".join(elems)
        res = rpc("eth_call", [{"to": MC3, "data": data}, block])
        b = bytes.fromhex(res[2:])
        # decode (bool success, bytes returnData)[]
        base = int.from_bytes(b[0:32], "big")
        cnt = int.from_bytes(b[base:base + 32], "big")
        arr = base + 32
        for j in range(cnt):
            eo = arr + int.from_bytes(b[arr + 32 * j:arr + 32 * j + 32], "big")
            ok = int.from_bytes(b[eo:eo + 32], "big") == 1
            ro = eo + int.from_bytes(b[eo + 32:eo + 64], "big")
            ln = int.from_bytes(b[ro:ro + 32], "big")
            out.append((ok, b[ro + 32:ro + 32 + ln]))
    return out


def dec_string(b):
    if not b:
        return None
    try:
        if len(b) >= 64:
            off = int.from_bytes(b[0:32], "big")
            if off == 32:
                ln = int.from_bytes(b[32:64], "big")
                return b[64:64 + ln].decode("utf-8", "replace").replace("\x00", "")
        # bytes32 style (e.g. MKR)
        return b[:32].rstrip(b"\0").decode("utf-8", "replace")
    except Exception:
        return None


def dec_uint(b):
    return int.from_bytes(b[:32], "big") if len(b) >= 32 else None


def dec_addr(b):
    return "0x" + b[12:32].hex() if len(b) >= 32 else None


if __name__ == "__main__":
    for u in POOL["urls"]:
        configure([u])
        print(host(u), rpc("eth_blockNumber", []), rpc("eth_getCode", [MC3, "latest"])[:20], file=sys.stderr)
