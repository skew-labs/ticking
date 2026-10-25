#!/usr/bin/env python3
"""Traffic for the path shadow: pairs of wallets per chain, asked of the bridge's public /trace?to= (the current services answer;
the EMV2 block compares in the background when "path" is set to shadow for the chain). Pairs come from the served data:
(a) two featured entities (often far apart), (b) a featured entity and one of its hop-2 neighbours (connected, 2-3 hops),
(c) a featured entity and a neighbour (direct edge). GET only, --rps requests a second.

usage: emv2_path_replay.py [--bridge 127.0.0.1:8896] [--engine 127.0.0.1:8740] [--n 300] [--rps 4] [--chains arc,unichain,linea,robinhood]
"""
import http.client
import json
import random
import sys
import time


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def get(hp, target, timeout=30):
    h, p = hp.rsplit(":", 1)
    c = http.client.HTTPConnection(h, int(p), timeout=timeout)
    try:
        c.request("GET", target, headers={"Accept": "application/json"})
        r = c.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body)
        except ValueError:
            return r.status, None
    except OSError:
        return 0, None
    finally:
        c.close()


def main():
    bridge, engine = arg("--bridge", "127.0.0.1:8896"), arg("--engine", "127.0.0.1:8740")
    n, gap = int(arg("--n", 300)), 1.0 / float(arg("--rps", 4))
    chains = arg("--chains", "arc,unichain,linea,robinhood").split(",")
    rng = random.Random(int(time.time()))
    done = {}
    for ch in chains:
        try:
            feat = [e["address"] for e in json.load(open(f"/srv/skew/emv2/featured/{ch}.json"))["entities"] if isinstance(e.get("address"), str)]
        except (OSError, ValueError, KeyError):
            continue
        rng.shuffle(feat)
        pairs = []
        pool = {}   # address -> hop distance from the entity it came from, so pairs are mostly connected within 2-4 hops
        for a in feat:
            for lim in (25, 60):
                _, g = get(engine, f"/graph?chain={ch}&address={a}&hops=2&limit={lim}")
                for x in (g or {}).get("nodes") or []:
                    if x.get("address") and x["address"] != a and not x.get("stop"):
                        pool.setdefault(a, set()).add(x["address"])
        allp = sorted({v for vs in pool.values() for v in vs} | set(feat))
        for a, vs in pool.items():
            vs = sorted(vs)
            for _ in range(min(40, len(vs))):
                pairs.append((a, rng.choice(vs)))          # entity -> its own neighbourhood (connected)
                pairs.append((rng.choice(vs), rng.choice(vs)))   # two nodes of one neighbourhood (often 2 hops via the entity)
        for _ in range(60):
            if len(allp) > 1:
                pairs.append(tuple(rng.sample(allp, 2)))   # unrelated pairs
        pairs = list(dict.fromkeys(p for p in pairs if p[0] != p[1]))
        rng.shuffle(pairs)
        k = 0
        for a, b in pairs[: n // len(chains)]:
            st, _ = get(bridge, f"/api/public/em/trace?chain={ch}&from={a}&to={b}")
            done[(ch, st)] = done.get((ch, st), 0) + 1
            k += 1
            time.sleep(gap)
        print(ch, "asked", k, flush=True)
    print(done)


main()
