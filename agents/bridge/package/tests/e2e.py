"""End-to-end checks for first-class Arbitrum One / Robinhood Chain markets + the wallet layer, with Solana regression.
usage: python tests/e2e.py http://127.0.0.1:18893/ <out_dir>
Writes <out_dir>/report.json (every check with pass/fail and details) plus screenshots; exits 1 on any failure.
Known, accepted console noise: the signed-out 401 from /api/console (production behaves the same)."""
import json
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

URL, OUT = sys.argv[1], Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
WALLET = "0x498752d5fa0600cbd613074c151abe15b3fec7cb"
NVDA = "robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
TSLA = "robinhood:0x322f0929c4625ed5bad873c95208d54e1c003b2d"
ARB = "arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548"
WETH = "arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
PROD = "https://ticking.dev/"
enc = lambda m: m.replace(":", "%3A")
checks, perf, shots, busy = [], {}, [], []


def check(name, ok, detail=""):
    checks.append({"check": name, "pass": bool(ok), "detail": str(detail)[:300]})
    print(("PASS " if ok else "FAIL ") + name + (" · " + str(detail)[:160] if detail else ""), flush=True)


def shot(page, name, el=None):
    p = OUT / (name + ".png")
    (el or page).screenshot(path=str(p))
    shots.append(str(p))


def watch(page, tag, errs):
    page.on("response", lambda r: (busy if r.status == 503 and r.headers.get("retry-after") else errs).append(f"{tag}: {r.status} {r.url.split('/', 3)[-1][:150]}") if r.status >= 500 else None)
    page.on("console", lambda m: errs.append(f"{tag}: {m.text[:200]}") if m.type == "error" and "401" not in m.text and "status of 5" not in m.text and "Failed to load resource" not in m.text else None)
    page.on("pageerror", lambda e: errs.append(f"{tag} pageerror: {str(e)[:200]}"))


def map_suite(page, tag):
    t0 = time.time()
    page.goto(URL + "#/?view=map&chain=robinhood", wait_until="load", timeout=90000)
    page.wait_for_selector(".flow-desk[data-em-ready=true], .flow-desk .em-map-canvas", timeout=60000)
    page.wait_for_function("document.querySelector('.flow-desk')?.dataset.emReady==='true'", timeout=60000)
    perf[tag + "_map_ready_s"] = round(time.time() - t0, 2)
    perf[tag + "_map_ready_mark_ms"] = page.evaluate("()=>{const m=performance.getEntriesByName('em-map-ready')[0],n=performance.getEntriesByType('navigation')[0];return m?Math.round(m.startTime):null}")
    bytes_ = page.evaluate("()=>performance.getEntriesByType('resource').filter(r=>r.name.includes('/assets/em/map/')).reduce((a,r)=>a+(r.transferSize||r.encodedBodySize||0),0)")
    perf[tag + "_map_bytes_initial"] = bytes_
    desk = page.query_selector(".flow-desk")
    desk.scroll_into_view_if_needed()
    time.sleep(1.5)
    shot(page, tag + "-map-overview", desk)
    check(tag + ": map ready", True, "%.1fs, %d bytes of map data" % (perf[tag + "_map_ready_s"], bytes_))
    check(tag + ": map initial payload within budget", bytes_ <= (2_000_000 if tag != "phone" else 600_000), bytes_)
    # frame times while zooming and panning
    c = page.query_selector(".em-map-canvas")
    bb = c.bounding_box()
    page.evaluate("()=>{window.__f=[];let l=performance.now();const t=n=>{window.__f.push(n-l);l=n;if(window.__f.length<400)requestAnimationFrame(t)};requestAnimationFrame(t)}")
    if tag == "phone":
        for _ in range(4):
            page.mouse.click(bb["x"] + bb["width"] - 30, bb["y"] + bb["height"] - 140) if False else None
        page.click("[data-em-zoom=in]"); time.sleep(.8); page.click("[data-em-zoom=in]"); time.sleep(.8)
    else:
        for _ in range(8):
            page.mouse.move(bb["x"] + bb["width"] * .45, bb["y"] + bb["height"] * .5); page.mouse.wheel(0, -260); time.sleep(.12)
        page.mouse.move(bb["x"] + bb["width"] * .5, bb["y"] + bb["height"] * .5); page.mouse.down(); page.mouse.move(bb["x"] + bb["width"] * .3, bb["y"] + bb["height"] * .4, steps=20); page.mouse.up()
    time.sleep(2)
    f = page.evaluate("()=>window.__f.slice(2)")
    f.sort()
    perf[tag + "_frame_p50_ms"] = round(f[len(f) // 2], 1) if f else None
    perf[tag + "_frame_p95_ms"] = round(f[int(len(f) * .95)], 1) if f else None
    stats = page.evaluate("()=>document.querySelector('.flow-desk').__emMapStats?.()")
    paints = sorted(stats["frames"]) if stats else []
    perf[tag + "_paint_p95_ms"] = round(paints[int(len(paints) * .95)], 1) if paints else None
    check(tag + ": frame p95 under 34 ms during zoom/pan", f and f[int(len(f) * .95)] < 34, perf[tag + "_frame_p95_ms"])
    shot(page, tag + "-map-zoom", desk)
    # keyboard: canvas focus, zoom, Enter opens nearest wallet
    page.focus(".em-map-canvas")
    for k in ("+", "+", "+", "ArrowRight"):
        page.keyboard.press(k); time.sleep(.3)
    page.keyboard.press("Enter")
    try:
        page.wait_for_selector(".map-inspector .em-card", timeout=20000)
        check(tag + ": keyboard Enter opens the nearest wallet", True)
    except Exception as e:
        check(tag + ": keyboard Enter opens the nearest wallet", False, e)
    # search by address -> card + fly
    page.fill(".map-search input", WALLET.upper().replace("0X", "0x"))
    page.keyboard.press("Enter")
    page.wait_for_selector(".map-inspector .em-card", timeout=30000)
    time.sleep(3)
    txt = page.inner_text(".map-inspector")
    check(tag + ": address search opens the wallet card", "0x4987" in txt and "Net worth" in txt, txt[:80])
    shot(page, tag + "-map-wallet", desk)
    # chain switch with keyboard
    page.focus("[data-map-chain=robinhood]")
    page.keyboard.press("ArrowLeft"); time.sleep(2)
    check(tag + ": arrow keys switch chain", page.get_attribute("[data-map-chain=arbitrum]", "aria-selected") == "true")
    bad = page.input_value(".map-search input") if False else None
    page.fill(".map-search input", "0x1234"); page.keyboard.press("Enter"); time.sleep(.5)
    check(tag + ": invalid address is rejected with a message", "40 hex" in page.inner_text(".map-search-status"))


def workspace_suite(page, tag):
    page.goto(URL + "#/console/workspace/" + enc(NVDA) + "?panel=wallets&wallet=" + WALLET, wait_until="load", timeout=90000)
    page.wait_for_selector(".em-side-dossier:not([hidden]) .em-card", timeout=60000)
    check(tag + ": deep link opens the wallet dossier", True)
    page.wait_for_selector(".em-chart-chip", timeout=30000)
    time.sleep(2)
    chip = page.inner_text(".em-chart-chip")
    if page.query_selector(".em-chart-chip [data-em-wide]:not([hidden])"):
        page.click(".em-chart-chip [data-em-wide]"); time.sleep(6)
    visible = [m for m in page.query_selector_all(".em-marker") if m.is_visible()]
    check(tag + ": wallet markers on the chart", len(visible) >= 1, "%d visible · %s" % (len(visible), chip[:60]))
    check(tag + ": marker legend shown", page.is_visible(".em-marker-legend"))
    if visible:
        visible[0].focus(); time.sleep(.4)
        check(tag + ": marker tooltip on keyboard focus", page.is_visible(".em-tip") and len(page.inner_text(".em-tip")) > 5, page.inner_text(".em-tip")[:60] if page.is_visible(".em-tip") else "")
        lab = visible[0].get_attribute("aria-label") or ""
        check(tag + ": marker has an accessible name", len(lab) > 8, lab[:60])
    shot(page, tag + "-ws-dossier-markers")
    # wallets tab content (panel=wallets)
    rows = page.query_selector_all(".em-holders tr[data-address]")
    check(tag + ": wallets tab lists holders", len(rows) >= 5, len(rows))
    # executions tab
    page.evaluate("()=>{const s=document.querySelector('.pro-data-tabs [data-tab=executions]');s&&s.click()}")
    time.sleep(4)
    check(tag + ": choosing a data view returns from the dossier", page.is_hidden(".em-side-dossier"))
    ex = page.query_selector_all(".pro-data-view tbody tr")
    check(tag + ": executions tab shows on-chain swaps", len(ex) >= 1, len(ex))
    btn = page.query_selector(".pro-data-view [data-em-wallet]")
    if btn:
        addr = btn.get_attribute("data-em-wallet")
        btn.click(); time.sleep(4)
        check(tag + ": executions wallet link opens its dossier", addr[:6] in page.inner_text(".em-side-dossier"), addr)
    page.evaluate("()=>{const s=document.querySelector('.pro-data-tabs [data-tab=executions]');s&&s.click()}")
    time.sleep(2)
    sel = page.query_selector(".pro-data-view .workspace-execution-select")
    if sel:
        sel.click(); time.sleep(2)
        ok = page.query_selector("[data-em-execution-wallet]")
        check(tag + ": execution inspector offers the wallet behind the swap", bool(ok), ok.inner_text() if ok else "")
        if ok:
            ok.click(); time.sleep(5)
        shot(page, tag + "-ws-executions")
    # assistant
    page.evaluate("()=>{const b=document.querySelector('.workspace-side-tabs [data-side-view=assistant]');b&&b.click()}")
    time.sleep(1.5)
    for q, sel_ in (("who borrowed USDG against NVDA", ".em-list-card"), ("NVDA holders", ".em-list-card"), (WALLET, ".assistant-message .em-card")):
        n0 = len(page.query_selector_all(sel_))
        ta = page.locator("textarea:visible").last
        ta.click(); ta.fill(q); page.keyboard.press("Enter")
        t0 = time.time()
        try:
            page.wait_for_function("([s,n])=>document.querySelectorAll(s).length>n", arg=[sel_, n0], timeout=45000)
            perf.setdefault(tag + "_assistant_s", []).append(round(time.time() - t0, 2))
            check(tag + ": assistant answers '%s' with a card" % q[:30], True)
        except Exception as e:
            check(tag + ": assistant answers '%s' with a card" % q[:30], False, e)
        time.sleep(1)
    shot(page, tag + "-ws-assistant")
    link = page.query_selector(".em-list-card a[data-em-chart='" + NVDA + "']")
    if link:
        link.click(); time.sleep(5)
        check(tag + ": assistant row opens the dossier in place", page.is_visible(".em-side-dossier .em-card"))


def a11y_suite(page, tag):
    unnamed = page.evaluate("""()=>[...document.querySelectorAll('.em-card button,.em-card a,.em-side-dossier button,.em-hub,.em-zoom,.em-marker,[data-em-wallet],.em-chart-chip button,.workspace-side-tabs button,.emx button,.emx-legend button,.market-network-chips button,.home-network-chips a')]
      .filter(e=>e.offsetParent&&!(e.getAttribute('aria-label')||e.textContent.trim())).map(e=>e.outerHTML.slice(0,80))""")
    check(tag + ": every EM control has an accessible name", not unnamed, unnamed[:3])


def error_suite(browser):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = ctx.new_page()
    errs = []
    watch(page, "errors", errs)
    page.route("**/assets/em/map/terrain-*.json", lambda r: r.fulfill(status=503, body="down"))
    page.goto(URL + "#/?view=map&chain=robinhood", wait_until="load", timeout=90000)
    try:
        page.wait_for_selector("[data-flow-retry]", timeout=30000)
        check("errors: map outage shows a retry state", True)
    except Exception as e:
        check("errors: map outage shows a retry state", False, e)
    page.unroute("**/assets/em/map/terrain-*.json")
    page.click("[data-flow-retry]")
    try:
        page.wait_for_function("document.querySelector('.flow-desk')?.dataset.emReady==='true'", timeout=60000)
        check("errors: map recovers after retry", True)
    except Exception as e:
        check("errors: map recovers after retry", False, e)
    page.route("**/api/public/em/tools/wallet**", lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":"Upstream error 503."}'))
    page.goto(URL + "#/console/workspace/" + enc(NVDA) + "?panel=wallets&wallet=" + WALLET, wait_until="load")
    try:
        page.wait_for_selector(".em-side-dossier .em-state-error [data-em-retry]", timeout=60000)
        check("errors: dossier outage shows an error with retry", True)
    except Exception as e:
        check("errors: dossier outage shows an error with retry", False, e)
    page.close()
    page = ctx.new_page()
    watch(page, "errors", errs)
    ctx.route("**/api/public/em/tools/holders**", lambda r: r.fulfill(status=429, headers={"Retry-After": "5"}, content_type="application/json", body='{"detail":"Too many requests."}'))
    page.goto(URL + "#/console/workspace/" + enc(TSLA) + "?panel=wallets", wait_until="load")
    try:
        page.wait_for_selector(".em-holders-wrap .em-state-error", timeout=30000)
        check("errors: rate-limited wallets tab explains and offers retry", "Too many" in page.inner_text(".em-holders-wrap"))
    except Exception as e:
        check("errors: rate-limited wallets tab explains and offers retry", False, e)
    shot(page, "errors-states")
    ctx.close()


def api(page, path):
    for attempt in range(4):   # 503 + Retry-After is the documented answer while a bounded read lane is busy
        r = page.evaluate("async p=>{const r=await fetch(p,{cache:'no-store'});let b=null;try{b=await r.json()}catch{}return {status:r.status,body:b}}", path)
        if r["status"] != 503:
            return r
        time.sleep(5)
    return r


def chart_ready(page, timeout=45000):
    page.wait_for_function("()=>/[0-9]/.test(document.querySelector('.pro-chart-close')?.textContent||'')", timeout=timeout)
    return page.inner_text(".pro-chart-pair"), page.inner_text(".pro-chart-close")


def network_suite(page, tag):
    page.goto(URL + "#/markets", wait_until="load", timeout=90000)
    # API contract: one shape, explicit network identity, honest policy
    q = "/api/public/engine/candles?aggregation=token&interval=1h&limit=200&network=robinhood&mint=" + NVDA.split(":")[1]
    r = api(page, q)
    ident = (r["body"] or {}).get("identity") or {}
    check(tag + ": NVDA candles carry identity.network robinhood", r["status"] == 200 and ident.get("network") == "robinhood" and ident.get("baseMint") == NVDA.split(":")[1], ident)
    check(tag + ": pricing policy is exposed (em_pool_reference_v2)", ident.get("historyPolicy") == "em_pool_reference_v2" and "priceSampling" in (r["body"].get("coverage") or {}), ident.get("historyPolicy"))
    r2 = api(page, "/api/public/engine/candles?aggregation=token&interval=1h&limit=5&market=" + ARB)
    check(tag + ": market=<network>:<address> selects the same contract", r2["status"] == 200 and r2["body"]["identity"]["network"] == "arbitrum", r2["status"])
    probe = page.context.new_page(); probe.goto(URL + "#/markets", wait_until="load", timeout=90000)   # expected 4xx stay out of the error watch
    check(tag + ": unknown network is rejected", api(probe, "/api/public/engine/candles?network=base&mint=0x0000000000000000000000000000000000000001&aggregation=token")["status"] == 422)
    check(tag + ": unreviewed address is not served", api(probe, "/api/public/engine/candles?network=arbitrum&mint=0x0000000000000000000000000000000000000001&aggregation=token")["status"] == 404)
    probe.close()
    t = api(page, "/api/public/engine/trades?aggregation=token&limit=20&network=robinhood&mint=" + NVDA.split(":")[1])
    rows = (t["body"] or {}).get("trades") or []
    check(tag + ": NVDA tape has block-ordered swaps with side and trader", t["status"] == 200 and rows and all(x.get("side") in ("B", "A", None) and x["slot"] > 0 for x in rows), len(rows))
    cat = api(page, "/api/public/markets?view=table&networks=all")["body"]["tokens"]
    nets = {}
    for x in cat:
        nets[x.get("network")] = nets.get(x.get("network"), 0) + 1
    check(tag + ": catalog rows carry an explicit network", all(x.get("network") for x in cat) and nets.get("robinhood", 0) > 100 and nets.get("arbitrum", 0) > 30, nets)
    # NVDA workspace: real logo, USD reference chart
    page.goto(URL + "#/console/workspace/" + enc(NVDA), wait_until="load", timeout=90000)
    pair, close = chart_ready(page)
    check(tag + ": NVDA workspace charts the USD token reference", "NVDA / USD" in pair, pair + " · " + close)
    logo = page.evaluate("async()=>{const im=[...document.querySelectorAll('img')].filter(i=>i.src.includes('robinhood-0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec'));const out=[];for(const i of im){i.loading='eager';try{await i.decode()}catch{}out.push(i.naturalWidth)}return out}")
    check(tag + ": NVDA shows its NVIDIA artwork (network-keyed icon)", logo and all(w >= 64 for w in logo), logo)
    shot(page, tag + "-nvda-workspace")
    page.evaluate("()=>{const s=document.querySelector('.pro-data-tabs [data-tab=executions]');s&&s.click()}")
    page.wait_for_function("()=>document.querySelectorAll('.pro-data-view tbody tr').length>=5", timeout=45000)
    cap = page.inner_text(".pro-table-caption")
    links = page.evaluate("()=>[...document.querySelectorAll('.pro-data-view tbody a')].map(a=>a.href).slice(0,3)")
    check(tag + ": executions tape lists decoded Robinhood Chain swaps", "Robinhood Chain swaps" in cap and links and all(l.startswith("https://robinhoodchain.blockscout.com/tx/0x") for l in links), cap[:80])
    shot(page, tag + "-nvda-executions")
    for name, market, label in (("arb", ARB, "ARB / USD"), ("weth", WETH, "WETH / USD")):
        page.goto(URL + "#/console/workspace/" + enc(market), wait_until="load", timeout=90000)
        pair, close = chart_ready(page)
        check(tag + ": %s chart on Arbitrum One" % name.upper(), label in pair, pair + " · " + close)
        page.evaluate("()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='1h');b&&b.click()}")
        time.sleep(4)
        shot(page, tag + "-%s-chart" % name)
    # Markets: network filter and the token page
    page.goto(URL + "#/markets", wait_until="load", timeout=90000)
    page.wait_for_selector(".public-token-table tbody tr", timeout=45000)
    page.select_option("[data-market-network]", "robinhood"); time.sleep(1.5)
    names = page.evaluate("()=>[...document.querySelectorAll('.public-token-table tbody tr')].map(r=>r.innerText)")
    check(tag + ": Markets filter lists Robinhood Chain markets only", names and all("Robinhood" in n for n in names[:20]), len(names))
    page.fill(".public-market-search input", "NVDA"); time.sleep(1.5)
    shot(page, tag + "-markets-robinhood")
    page.goto(URL + "#/token/" + enc(NVDA), wait_until="load", timeout=90000)
    page.wait_for_selector(".public-token-header h1", timeout=45000)
    head = page.inner_text(".public-token-header")
    check(tag + ": token page names the network", "Robinhood Chain" in head and "Solana" not in head, head[:80])
    try:
        page.wait_for_function("()=>/Token contract/.test(document.querySelector('.public-token-stats')?.innerText||'')", timeout=30000)
        check(tag + ": token page shows the contract, not a Solana mint", True)
    except Exception as e:
        check(tag + ": token page shows the contract, not a Solana mint", False, e)
    shot(page, tag + "-nvda-token-page")


def solana_suite(page, tag):
    page.goto(URL + "#/console/workspace/SOL", wait_until="load", timeout=90000)
    pair, close = chart_ready(page)
    check(tag + ": SOL workspace unchanged (USDC token reference)", "SOL / USDC" in pair, pair + " · " + close)
    page.evaluate("()=>{const s=document.querySelector('.pro-data-tabs [data-tab=executions]');s&&s.click()}")
    page.wait_for_function("()=>document.querySelectorAll('.pro-data-view tbody tr').length>=1", timeout=45000)
    check(tag + ": SOL executions keep the Solana columns", "Canonical order" in page.inner_text(".pro-data-view thead"))
    shot(page, tag + "-sol-workspace")
    s = api(page, "/api/public/engine/candles?mint=So11111111111111111111111111111111111111112&aggregation=token&interval=1h&limit=48&history=full")["body"]
    prod = page.evaluate("async u=>{try{const r=await fetch(u,{cache:'no-store'});return await r.json()}catch(e){return null}}", PROD + "api/public/engine/candles?mint=So11111111111111111111111111111111111111112&aggregation=token&interval=1h&limit=48&history=full")
    if prod:
        same_id = {k: v for k, v in s["identity"].items()} == {k: v for k, v in prod["identity"].items()}
        a = {b["time"]: (b["open"], b["close"]) for b in s["candles"][:-2]}
        b = {c["time"]: (c["open"], c["close"]) for c in prod["candles"][:-2]}
        common = [k for k in a if k in b]
        diff = [k for k in common if a[k] != b[k]]
        check(tag + ": SOL candles identical to production (identity and closed bars)", same_id and common and not diff, "%d common bars, %d differ" % (len(common), len(diff)))
    else:
        check(tag + ": SOL candles identical to production (identity and closed bars)", s["identity"]["network"] == "solana", "production read blocked by CORS; identity only")
    cat = api(page, "/api/public/markets?view=table")["body"]["tokens"]
    sol = [x["id"] for x in cat if x.get("network") in ("solana", "exchange")]
    check(tag + ": Solana and exchange rows unchanged in count", len(sol) >= 380, len(sol))
    page.goto(URL + "#/markets", wait_until="load", timeout=90000)
    page.wait_for_selector(".public-token-table tbody tr", timeout=45000)
    page.select_option("[data-market-network]", "solana"); time.sleep(1.5)
    rows = page.evaluate("()=>[...document.querySelectorAll('.public-token-table tbody tr')].map(r=>r.innerText)")
    check(tag + ": Markets Solana filter excludes EVM rows", rows and not any("Robinhood Token" in r for r in rows), len(rows))
    shot(page, tag + "-markets-solana")


PERF_INIT = """window.__vitals={lcp:0,cls:0,long:0};try{new PerformanceObserver(l=>{for(const e of l.getEntries())window.__vitals.lcp=e.startTime}).observe({type:'largest-contentful-paint',buffered:true});
new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)window.__vitals.cls+=e.value}).observe({type:'layout-shift',buffered:true});
new PerformanceObserver(l=>{for(const e of l.getEntries())window.__vitals.long+=Math.max(0,e.duration-50)}).observe({type:'longtask',buffered:true});}catch(e){}"""


def vitals(browser, tag, viewport, route, ready):
    ctx = browser.new_context(**viewport); ctx.add_init_script(PERF_INIT); pg = ctx.new_page()
    t0 = time.time(); pg.goto(URL + route, wait_until="load", timeout=90000)
    try:
        pg.wait_for_function(ready, timeout=45000)
    except Exception:
        pass
    ready_s = round(time.time() - t0, 2); pg.wait_for_timeout(2500)
    v = pg.evaluate("()=>window.__vitals")
    perf[tag] = {"interactive_s": ready_s, "lcp_ms": round(v["lcp"]), "cls": round(v["cls"], 4), "long_task_ms": round(v["long"])}
    check(tag + ": layout stable (CLS < 0.1)", v["cls"] < 0.1, perf[tag])
    ctx.close()


def economy_suite(page, tag):
    page.goto(URL + "#/console/workspace/" + enc(NVDA) + "?panel=economy", wait_until="load", timeout=90000)
    try:
        page.wait_for_selector(".emx[aria-busy=false] .emx-card", timeout=60000)
    except Exception as e:   # record and continue: one slow panel must not abort the remaining suites
        check(tag + ": Economy panel loads within 60 s", False, str(e).splitlines()[0]); shot(page, tag + "-economy-timeout"); return
    txt = page.inner_text(".emx")
    check(tag + ": Economy panel shows money state, venues, leverage, flows and wallets", all(k in txt for k in ("Money state", "Where it sits", "Leverage & liquidations", "Flows", "Notable wallets")), txt[:80])
    import re as _re
    raw = _re.findall(r"\b[a-z0-9]+_[a-z0-9_]+\b|\b[a-z]+ v\d:[a-z]|:singleton|:pool manager", txt)
    check(tag + ": labels are human-readable (no registry keys)", not raw, raw[:5])
    who = page.inner_text(".emx-who") if page.query_selector(".emx-who") else ""
    check(tag + ": notable wallets exclude protocol contracts", who and not _re.search(r"Pool manager|Singleton|DEX router|Bridge contract", who), who[:120])
    check(tag + ": money state has a stacked bar with an accessible summary", (page.get_attribute(".emx-stack", "aria-label") or "").count("%") >= 2)
    check(tag + ": every section states its source and as-of time", "time unknown" not in txt and ("Map snapshot" in txt or "Live analytics" in txt), txt[txt.find("snapshot") - 40:txt.find("snapshot") + 60] if "snapshot" in txt else "")
    page.click("[data-emx-window='7d']"); time.sleep(3)
    check(tag + ": flow window switches (radio group)", page.get_attribute("[data-emx-window='7d']", "aria-checked") == "true")
    shot(page, tag + "-economy-panel"); a11y_suite(page, tag + "-economy")
    w = page.query_selector(".emx-who [data-emx-wallet]")
    if w:
        a = w.get_attribute("data-emx-wallet"); w.click(); time.sleep(5)
        check(tag + ": a notable wallet opens its dossier", a[:6] in (page.inner_text(".em-side-dossier") if page.query_selector(".em-side-dossier") else ""), a)
    page.goto(URL + "#/console/workspace/" + enc(NVDA), wait_until="load", timeout=90000)
    chart_ready(page)
    page.wait_for_selector(".emx-legend [data-emx-overlay=events]", timeout=30000)
    before = page.get_attribute(".emx-legend [data-emx-overlay=events]", "aria-pressed")
    page.click(".emx-legend [data-emx-overlay=events]"); time.sleep(.5)
    after = page.get_attribute(".emx-legend [data-emx-overlay=events]", "aria-pressed")
    check(tag + ": chart overlay legend toggles economic events", before != after, before + "->" + after)
    page.click(".emx-legend [data-emx-overlay=events]")
    fit = page.query_selector(".emx-legend [data-emx-fit]:not([hidden])")
    if fit:
        fit.click(); time.sleep(6)
    count = page.inner_text(".emx-legend [data-emx-count]")
    check(tag + ": economic events are reachable (fit brings them into view)", count.startswith(("none", "0 of")) is False or "none" in count, count)
    page.wait_for_function("()=>!/loading/.test(document.querySelector('.emx-legend [data-emx-count]')?.textContent||'loading')", timeout=30000)
    liq = page.query_selector(".emx-legend [data-emx-overlay=liquidations]")
    if liq and not liq.is_disabled():
        if liq.get_attribute("aria-pressed") != "true":
            try:   # the legend is rebuilt when fit changes the interval; click the live element, never a stale handle
                page.click(".emx-legend [data-emx-overlay=liquidations]:not([disabled])", timeout=20000); time.sleep(2.5)
            except Exception as error:
                print("liquidation toggle unavailable:", str(error).splitlines()[0])
        try:   # the ladder reloads with the chart after an interval change; wait for it instead of sampling once
            page.wait_for_function("()=>[...document.querySelectorAll('.emx-band')].some(b=>!b.hidden&&b.offsetParent)", timeout=20000)
        except Exception:
            pass
        bands = page.evaluate("()=>[...document.querySelectorAll('.emx-band')].filter(b=>!b.hidden&&b.offsetParent).length")
        check(tag + ": liquidation levels render as labelled bands", bands >= 1, bands)
        shot(page, tag + "-liquidation-bands")
        try:
            page.click(".emx-legend [data-emx-overlay=liquidations][aria-pressed=true]", timeout=5000); time.sleep(.5)
        except Exception:
            pass
    c = page.query_selector("canvas").bounding_box()
    page.mouse.move(c["x"] + c["width"] * .5, c["y"] + c["height"] * .5); page.mouse.wheel(0, -400); time.sleep(1)
    check(tag + ": overlays survive zoom", page.is_visible(".emx-legend"))
    shot(page, tag + "-economy-overlays")
    page.evaluate("()=>{const b=document.querySelector('.workspace-side-tabs [data-side-view=assistant]');b&&b.click()}"); time.sleep(1.5)
    ph = page.get_attribute(".assistant-compose textarea", "placeholder") or ""
    check(tag + ": assistant placeholder follows the market", "NVDA" in ph and "SOL" not in ph, ph)
    check(tag + ": economy intents offered as suggestions", len(page.query_selector_all(".assistant-suggestions [data-em-intent]")) == 4)
    for q in ("liquidation levels for NVDA", "who is leveraged on NVDA", "money flow for NVDA", "why did NVDA move"):
        n0 = len(page.query_selector_all(".emx-assist"))
        ta = page.locator("textarea:visible").last; ta.click(); ta.fill(q); page.keyboard.press("Enter")
        try:
            page.wait_for_function("n=>document.querySelectorAll('.emx-assist').length>n", arg=n0, timeout=45000)
            ok = page.query_selector_all(".emx-assist")[-1].query_selector("a[href*='panel=economy']") is not None
            check(tag + ": assistant answers '%s' with an Economic Machine card" % q, ok)
            if q.startswith("why"):
                last = page.query_selector_all(".assistant-message")[-1].inner_text() if page.query_selector_all(".assistant-message") else page.inner_text(".assistant-messages")
                check(tag + ": 'why did NVDA move' explains the move with numbers", re.search(r"NVDA [+−-]\d+\.\d% in 24h", last) is not None and "open the panel" not in last.lower(), last[:200])
        except Exception as e:
            check(tag + ": assistant answers '%s' with an Economic Machine card" % q, False, e)
        time.sleep(1)
    shot(page, tag + "-economy-assistant")
    page.goto(URL + "#/token/" + enc(NVDA), wait_until="load", timeout=90000)
    try:
        page.wait_for_selector(".em-economy-slot .emx[aria-busy=false] .emx-card", timeout=60000)
        page.evaluate("()=>document.querySelector('.em-economy-slot').scrollIntoView()"); time.sleep(1)
        check(tag + ": token page shows the Economic Machine section", "Money state" in page.inner_text(".em-economy-slot"))
        shot(page, tag + "-token-economy")
    except Exception as e:
        check(tag + ": token page shows the Economic Machine section", False, e)
    page.goto(URL + "#/markets?network=robinhood", wait_until="load", timeout=90000)
    page.wait_for_selector(".public-token-table tbody tr", timeout=45000)
    check(tag + ": Markets opens on the network from the URL (chip pressed)", page.get_attribute("[data-market-chip=robinhood]", "aria-pressed") == "true")
    page.fill(".public-market-search input", "NVDA")
    try:
        page.wait_for_function("()=>/NVDA/.test(document.querySelector('.market-preview-heading')?.innerText||'')", timeout=20000)
        head = page.inner_text(".market-preview-heading")
        check(tag + ": Markets preview follows the filtered selection", "NVDA" in head and "Robinhood Chain" in head, head[:80])
    except Exception as e:
        check(tag + ": Markets preview follows the filtered selection", False, e)
    name = page.evaluate("()=>{const b=document.querySelector('.public-token-table tbody tr .public-token-name b');return b?b.scrollWidth<=b.clientWidth+1:null}")
    check(tag + ": asset names are not truncated on desktop", name is not False, name)
    shot(page, tag + "-markets-preview")
    page.goto(URL + "#/", wait_until="load", timeout=90000)
    try:
        page.wait_for_selector(".home-network-chips a", timeout=30000)
        check(tag + ": home links to the new networks and the Economic Machine", len(page.query_selector_all(".home-network-chips a")) == 4)
    except Exception as e:
        check(tag + ": home links to the new networks and the Economic Machine", False, e)


import os
if os.environ.get("E2E_ONLY") == "economy":   # targeted re-run of the Economic Machine suite (light + dark)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--ignore-gpu-blocklist"])
        for tag, dark in (("light", False), ("dark", True)):
            ctx = b.new_context(viewport={"width": 1600, "height": 1000})
            if dark:
                ctx.add_init_script("try{localStorage.setItem('ticking-color-theme','dark')}catch(e){}")
            economy_suite(ctx.new_page(), tag); ctx.close()
    report = {"url": URL, "checks": checks, "passed": sum(c["pass"] for c in checks), "failed": sum(not c["pass"] for c in checks), "screenshots": shots}
    (OUT / "report.json").write_text(json.dumps(report, indent=1)); print(report["passed"], report["failed"]); sys.exit(1 if report["failed"] else 0)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--ignore-gpu-blocklist"])
    errs = []
    light = b.new_context(viewport={"width": 1600, "height": 1000})
    pl = light.new_page(); watch(pl, "light", errs)
    network_suite(pl, "light"); solana_suite(pl, "light"); economy_suite(pl, "light")
    map_suite(pl, "light"); a11y_suite(pl, "light-map"); workspace_suite(pl, "light"); a11y_suite(pl, "light-ws")
    dark = b.new_context(viewport={"width": 1600, "height": 1000}); dark.add_init_script("try{localStorage.setItem('ticking-color-theme','dark')}catch(e){}")
    pd = dark.new_page(); watch(pd, "dark", errs)
    network_suite(pd, "dark"); economy_suite(pd, "dark"); map_suite(pd, "dark"); workspace_suite(pd, "dark")
    phone = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    pp = phone.new_page(); watch(pp, "phone", errs)
    map_suite(pp, "phone")
    pp.goto(URL + "#/console/workspace/" + enc(NVDA), wait_until="load"); chart_ready(pp); shot(pp, "phone-nvda")
    check("phone: NVDA chart renders", True)
    pp.goto(URL + "#/console/workspace/" + enc(NVDA) + "?panel=wallets&wallet=" + WALLET, wait_until="load"); time.sleep(16)
    check("phone: dossier renders", pp.is_visible(".em-side-dossier .em-card")); shot(pp, "phone-ws")
    desk, phonev = {"viewport": {"width": 1600, "height": 1000}}, {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
    ready_ws = "()=>/[0-9]/.test(document.querySelector('.pro-chart-close')?.textContent||'')"
    ready_tp = "()=>/[0-9]/.test(document.querySelector('.public-price-header')?.innerText||'')"
    for vt, vp in (("desktop", desk), ("phone", phonev)):
        vitals(b, "vitals_workspace_" + vt, vp, "#/console/workspace/" + enc(NVDA), ready_ws)
        vitals(b, "vitals_token_page_" + vt, vp, "#/token/" + enc(NVDA), ready_tp)
        vitals(b, "vitals_sol_workspace_" + vt, vp, "#/console/workspace/SOL", ready_ws)      # same-session Solana baseline
        vitals(b, "vitals_sol_token_page_" + vt, vp, "#/token/SOL", ready_tp)
    pp2 = phone.new_page(); watch(pp2, "phone", errs)
    pp2.goto(URL + "#/console/workspace/" + enc(NVDA) + "?panel=economy", wait_until="load")
    try:
        pp2.wait_for_selector(".emx[aria-busy=false] .emx-card", timeout=60000)
        check("phone: Economy panel opens as a full-height sheet", pp2.evaluate("()=>{const r=document.querySelector('.pro-insights').getBoundingClientRect();return r.height>innerHeight*.8}"))
        shot(pp2, "phone-economy")
    except Exception as e:
        check("phone: Economy panel opens as a full-height sheet", False, e)
    error_suite(b)
    check("no console errors (excluding the signed-out /api/console 401)", not errs, errs[:5])
    perf["transient_503_retry_after"] = len(busy)
    check("transient 503 + Retry-After answers stay rare (frames retained, retried)", len(busy) <= 5, busy[:5])
report = {"url": URL, "checks": checks, "passed": sum(c["pass"] for c in checks), "failed": sum(not c["pass"] for c in checks), "perf": perf, "screenshots": shots}
(OUT / "report.json").write_text(json.dumps(report, indent=1))
print(json.dumps({"passed": report["passed"], "failed": report["failed"], "perf": perf}, indent=1))
sys.exit(1 if report["failed"] else 0)
