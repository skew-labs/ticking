//! ticking-engine: LIVE exact state engine + in-process quote core for Robinhood Chain (Rust).
//! - Cold start / gap / reorg / dirty: full snapshot via `harness/rebuild_all.py <block>` (Python RPC fetcher), then
//!   catch-up by replaying logs from snapshot block to head.
//! - Per poll: eth_getLogs(all tracked venue addresses + V4 PoolManager) for (block, head]; exact incremental apply:
//!   V3-family/Algebra Swap (sqrtP, L, tick from event), Mint/Burn (liquidityNet at ticks + active L),
//!   V4 Swap / ModifyLiquidity by PoolId. Ignorable events (Collect/Flash/...) skipped. Anything else -> pool DIRTY,
//!   published with liquidity=0 (router can't use it) until the next rebuild.
//! - Reorg: stored hash of last applied block re-checked each poll; mismatch -> rebuild. Gap > MAX_GAP -> rebuild.
//! - Serves: /quote (best|fast), /pool_quote (single pool exact sim), /health, /metrics (Prometheus), /freshness.
use core::*; use core::router::*; use ruint::aliases::U256; use serde_json::{json, Value};
use std::{collections::{HashMap, HashSet}, io::{Read, Write}, net::TcpListener, sync::{Arc, Mutex, RwLock, atomic::{AtomicU64, Ordering::Relaxed}}, time::{Duration, Instant}};

const T_SWAP: &str = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67";
const T_MINT: &str = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde";
const T_BURN: &str = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c";
const T_V4SWAP: &str = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f";
const T_V4MOD: &str = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec";
// state-irrelevant for exact-in swap math (balances only / admin / oracle)
const IGNORE: [&str; 6] = ["0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0", // Collect
    "0xbdbdb71d7860376ba52b25a5028beea23581364a40522f6bcfb86bb1f2dca633", // Flash
    "0x596b573906218d3411850b26a6b437d6c4522fdb43d2d2386263f86d50b8b151", // CollectProtocol
    "0x973d8d92bb299f4af6ce49b52a8adb85ae46b9f214c4c4fc06ac77401237b133", // SetFeeProtocol
    "0xac49e518f90a358f652e4400164f05a5d8f7e35e7747279bc3a93dbf584e125a", // IncreaseObservationCardinalityNext
    "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95"]; // Initialize
const PM: &str = "0x8366a39cc670b4001a1121b8f6a443a643e40951";
const WETH: &str = "0x0bd7d308f8e1639fab988df18a8011f41eacad73";

struct St { block: u64, pools: Vec<Pool>, toks: HashMap<String, JTok>, epoch: u64 }
static PATHS: RwLock<Option<(u64, HashMap<(String, String, usize), Arc<Vec<Path>>>)>> = RwLock::new(None);
#[derive(Default)] struct M { head: AtomicU64, block: AtomicU64, logs: AtomicU64, swaps: AtomicU64, liq: AtomicU64, unknown: AtomicU64, rebuilds: AtomicU64,
    reorgs: AtomicU64, gaps: AtomicU64, errors: AtomicU64, publishes: AtomicU64, dirty: AtomicU64, quotes: AtomicU64, qerr: AtomicU64, apply_us: AtomicU64, verified: AtomicU64, diverged: AtomicU64, fee_refresh: AtomicU64, rollbacks: AtomicU64, stale_rejects: AtomicU64 }
static MET: std::sync::LazyLock<M> = std::sync::LazyLock::new(M::default);
static LAT: Mutex<Vec<(bool, u64)>> = Mutex::new(vec![]);
static UPD: Mutex<Vec<u64>> = Mutex::new(vec![]); // head-seen -> published (ms)
static DIVLOG: Mutex<Vec<Value>> = Mutex::new(vec![]);
static FRESH: std::sync::LazyLock<Mutex<HashMap<String, u64>>> = std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));

fn rpc(c: &reqwest::blocking::Client, url: &str, m: &str, p: Value) -> Result<Value, String> {
    for i in 0..4 { match c.post(url).json(&json!({"jsonrpc":"2.0","id":1,"method":m,"params":p})).send().and_then(|r| r.json::<Value>()) {
        Ok(v) => { if let Some(e) = v.get("error") { if i < 3 && (e.to_string().contains("rate") || e.to_string().contains("429") || e.to_string().contains("Too Many")) { std::thread::sleep(Duration::from_millis(500 * (i + 1))); continue; } return Err(e.to_string()); } return Ok(v["result"].clone()); }
        Err(e) => { if i == 3 { return Err(e.to_string()); } std::thread::sleep(Duration::from_millis(200 * (i + 1))); } } }
    Err("rpc".into())
}
fn rpc_batch(c: &reqwest::blocking::Client, url: &str, calls: &[(String, Value)]) -> Result<Vec<Option<Value>>, String> {
    let mut out = vec![]; for ch in calls.chunks(50) {
        let body: Vec<Value> = ch.iter().enumerate().map(|(i, (m, p))| json!({"jsonrpc":"2.0","id":i,"method":m,"params":p})).collect();
        let mut ok = None; for i in 0..4 { match c.post(url).json(&body).send().and_then(|r| r.json::<Value>()) {
            Ok(Value::Array(mut v)) => { v.sort_by_key(|x| x["id"].as_u64().unwrap_or(0)); ok = Some(v.into_iter().map(|x| x.get("result").cloned()).collect::<Vec<_>>()); break; }
            _ => std::thread::sleep(Duration::from_millis(400 * (i + 1))) } }
        out.extend(ok.ok_or("batch failed")?); }
    Ok(out)
}
const MC3: &str = "0xca11bde05977b3631167028862be2a173976ca11";
fn w256(x: u64) -> String { format!("{x:064x}") }
/// Multicall3.tryAggregate(false, (target, 4-byte selector)[]) in ONE eth_call; returns per-call return hex (None on failure)
fn multicall(c: &reqwest::blocking::Client, url: &str, calls: &[(String, &str)], blk: u64) -> Result<Vec<Option<String>>, String> {
    let n = calls.len() as u64; let mut heads = String::new(); let mut tails = String::new(); let mut off = 32 * n;
    for (a, sel) in calls { heads += &w256(off); let t = format!("{:0>64}{}{}{}", a.trim_start_matches("0x"), w256(64), w256(4), format!("{:0<64}", sel.trim_start_matches("0x"))); off += (t.len() / 2) as u64; tails += &t; }
    let data = format!("0xbce38bd7{}{}{}{}{}", w256(0), w256(64), w256(n), heads, tails);
    let r = rpc(c, url, "eth_call", json!([{"to": MC3, "data": data, "gas": "0x3b9aca00"}, format!("0x{blk:x}")]))?; let r = r.as_str().ok_or("mc")?.trim_start_matches("0x").to_string();
    let g = |i: usize| -> usize { if r.len() < i * 2 + 64 { return 0; } usize::from_str_radix(&r[i * 2 + 48..i * 2 + 64], 16).unwrap_or(0) };
    let base = g(0); let m = g(base); let mut out = vec![];
    for i in 0..m { let eo = base + 32 + g(base + 32 + 32 * i); let ok = g(eo) == 1; let ro = eo + g(eo + 32); let ln = g(ro);
        out.push(if ok { Some(format!("0x{}", &r[(ro + 32) * 2..(ro + 32 + ln) * 2])) } else { None }); }
    Ok(out)
}
fn hx(v: &Value) -> u64 { u64::from_str_radix(v.as_str().unwrap_or("0x0").trim_start_matches("0x"), 16).unwrap_or(0) }
fn word(d: &str, i: usize) -> U256 { let d = d.trim_start_matches("0x"); U256::from_str_radix(&d[i * 64..i * 64 + 64], 16).unwrap() }
fn sword(w: U256) -> i128 { if w.bit(255) { -((U256::ZERO.wrapping_sub(w)).to::<u128>() as i128) } else { w.to::<u128>() as i128 } }
fn stick(w: U256) -> i32 { sword(w) as i32 }
fn log(msg: &str, extra: Value) { let mut v = json!({"svc":"engine","msg":msg,"ts":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64}); if let (Some(o), Some(e)) = (v.as_object_mut(), extra.as_object()) { for (k, x) in e { o.insert(k.clone(), x.clone()); } } eprintln!("{v}"); }

struct Eng { pools: Vec<Pool>, idx: HashMap<String, usize>, toks: HashMap<String, JTok>, block: u64, dirty: HashSet<usize>, hash: Option<(u64, String)>, epoch: u64, zero_sig: Vec<bool>, touched_alg: HashSet<usize>, ring: std::collections::VecDeque<(u64, String, Vec<Pool>)> }
impl Eng {
    fn from_snapshot(path: &str) -> Eng { let (b, pools, toks) = load(path); let idx = pools.iter().enumerate().map(|(i, p)| (p.addr.clone(), i)).collect();
        Eng { pools, idx, toks, block: b, dirty: HashSet::new(), hash: None, epoch: 0, zero_sig: vec![], touched_alg: HashSet::new(), ring: Default::default() } }
    fn set_liq_ticks(p: &mut Pool, lo: i32, hi: i32, dl: i128) {
        let mut t: Vec<(i32, i128)> = (*p.ticks).clone();
        for (tk, d) in [(lo, dl), (hi, -dl)] { match t.binary_search_by_key(&tk, |x| x.0) { Ok(i) => { t[i].1 += d; if t[i].1 == 0 { t.remove(i); } } Err(i) => if d != 0 { t.insert(i, (tk, d)) } } }
        p.ticks = Arc::new(t);
        if lo <= p.tick && p.tick < hi { p.liq = if dl < 0 { p.liq - dl.unsigned_abs() } else { p.liq + dl as u128 }; }
    }
    fn apply(&mut self, lg: &Value) {
        MET.logs.fetch_add(1, Relaxed);
        let a = lg["address"].as_str().unwrap_or("").to_lowercase(); let t: Vec<String> = lg["topics"].as_array().map(|x| x.iter().map(|v| v.as_str().unwrap_or("").to_lowercase()).collect()).unwrap_or_default();
        let d = lg["data"].as_str().unwrap_or("0x"); let bn = hx(&lg["blockNumber"]); if t.is_empty() { return; }
        if a == PM { let Some(&i) = t.get(1).and_then(|id| self.idx.get(id)) else { return };
            let p = &mut self.pools[i];
            if t[0] == T_V4SWAP { p.sqrt_p = word(d, 2); p.liq = word(d, 3).to::<u128>(); p.tick = stick(word(d, 4)); MET.swaps.fetch_add(1, Relaxed); }
            else if t[0] == T_V4MOD { let (lo, hi, dl) = (stick(word(d, 0)), stick(word(d, 1)), sword(word(d, 2))); Self::set_liq_ticks(p, lo, hi, dl); MET.liq.fetch_add(1, Relaxed); }
            else { return; }
            FRESH.lock().unwrap().insert(p.addr.clone(), bn); return; }
        let Some(&i) = self.idx.get(&a) else { return };
        if self.pools[i].venue == "alandale" { self.touched_alg.insert(i); }
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| { let p = &mut self.pools[i];
            if t[0] == T_SWAP { let (a0, a1) = (sword(word(d, 0)), sword(word(d, 1)));
                p.sqrt_p = word(d, 2); p.liq = word(d, 3).to::<u128>(); p.tick = stick(word(d, 4));
                p.bal0 = if a0 >= 0 { p.bal0 + U256::from(a0 as u128) } else { p.bal0.saturating_sub(U256::from(a0.unsigned_abs())) };
                p.bal1 = if a1 >= 0 { p.bal1 + U256::from(a1 as u128) } else { p.bal1.saturating_sub(U256::from(a1.unsigned_abs())) };
                MET.swaps.fetch_add(1, Relaxed); true }
            else if t[0] == T_MINT && t.len() == 4 { Self::set_liq_ticks(p, stick(U256::from_str_radix(&t[2][2..], 16).unwrap()), stick(U256::from_str_radix(&t[3][2..], 16).unwrap()), word(d, 1).to::<u128>() as i128); MET.liq.fetch_add(1, Relaxed); true }
            else if t[0] == T_BURN && t.len() == 4 { Self::set_liq_ticks(p, stick(U256::from_str_radix(&t[2][2..], 16).unwrap()), stick(U256::from_str_radix(&t[3][2..], 16).unwrap()), -(word(d, 0).to::<u128>() as i128)); MET.liq.fetch_add(1, Relaxed); true }
            else { IGNORE.contains(&t[0].as_str()) } }));
        match r { Ok(true) => { FRESH.lock().unwrap().insert(a, bn); } Ok(false) | Err(_) => { if !IGNORE.contains(&t[0].as_str()) { self.dirty.insert(i); MET.unknown.fetch_add(1, Relaxed); } } }
    }
    fn publish(&mut self, shared: &RwLock<Arc<St>>) {
        let mut pools = self.pools.clone(); for &i in &self.dirty { pools[i].liq = 0; }
        let sig: Vec<bool> = pools.iter().map(|p| p.liq == 0).collect(); if sig != self.zero_sig { self.epoch += 1; self.zero_sig = sig; }
        *shared.write().unwrap() = Arc::new(St { block: self.block, pools, toks: self.toks.iter().map(|(k, v)| (k.clone(), JTok { decimals: v.decimals, symbol: v.symbol.clone() })).collect(), epoch: self.epoch });
        MET.block.store(self.block, Relaxed); MET.dirty.store(self.dirty.len() as u64, Relaxed); MET.publishes.fetch_add(1, Relaxed);
    }
    fn addrs(&self) -> Vec<String> { let mut v: Vec<String> = self.idx.keys().filter(|k| k.len() == 42).cloned().collect(); v.push(PM.into()); v }
}
fn rebuild(c: &reqwest::blocking::Client, url: &str, py: &str, reason: &str) -> Option<Eng> {
    let head = rpc(c, url, "eth_blockNumber", json!([])).ok().map(|v| hx(&v))?; let t = Instant::now();
    let ok = std::process::Command::new(py).args(["harness/rebuild_all.py", &format!("0x{head:x}")]).stdout(std::process::Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    if !ok { MET.errors.fetch_add(1, Relaxed); log("rebuild_failed", json!({"reason": reason})); return None; }
    let p = std::fs::read_to_string("data/state_all_latest.txt").ok()?; let mut e = Eng::from_snapshot(p.trim());
    let freshb = e.block; { let mut f = FRESH.lock().unwrap(); for p in &e.pools { f.insert(p.addr.clone(), freshb); } }
    MET.rebuilds.fetch_add(1, Relaxed); e.epoch = 1_000_000 * MET.rebuilds.load(Relaxed);
    log("rebuild", json!({"reason": reason, "block": e.block, "secs": t.elapsed().as_secs_f64(), "pools": e.pools.len()})); Some(e)
}
fn verify(c: &reqwest::blocking::Client, url: &str, e: &mut Eng, blk: u64) {
    let ids: Vec<usize> = (0..e.pools.len()).filter(|&i| e.pools[i].addr.len() == 42 && !e.dirty.contains(&i)).collect();
    let mut calls = vec![]; for &i in &ids { let p = &e.pools[i]; calls.push((p.addr.clone(), if p.venue == "alandale" { "0xe76c01e4" } else { "0x3850c7bd" })); calls.push((p.addr.clone(), "0x1a686502")); }
    let Ok(r) = multicall(c, url, &calls, blk) else { MET.errors.fetch_add(1, Relaxed); return };
    for (k, &i) in ids.iter().enumerate() { let (Some(Some(s0)), Some(Some(l))) = (r.get(2 * k), r.get(2 * k + 1)) else { continue };
        if s0.len() < 130 || l.len() < 66 { continue; }
        let sq = word(s0, 0); let tk = stick(word(s0, 1)); let lq = word(l, 0).to::<u128>(); let p = &e.pools[i]; MET.verified.fetch_add(1, Relaxed);
        if sq != p.sqrt_p || tk != p.tick || lq != p.liq { MET.diverged.fetch_add(1, Relaxed);
            DIVLOG.lock().unwrap().push(json!({"block": blk, "pool": p.addr, "venue": p.venue, "sqrt_ok": sq == p.sqrt_p, "tick_ok": tk == p.tick, "liq_ok": lq == p.liq}));
            log("diverged", json!({"block": blk, "pool": p.addr, "venue": p.venue})); e.dirty.insert(i); } }
}
fn refresh_dynamic_fees(c: &reqwest::blocking::Client, url: &str, e: &mut Eng, blk: u64) {
    let ids: Vec<usize> = (0..e.pools.len()).filter(|&i| matches!(e.pools[i].venue.as_str(), "up" | "alandale")).collect(); if ids.is_empty() { return; }
    let calls: Vec<(String, &str)> = ids.iter().map(|&i| (e.pools[i].addr.clone(), "0xddca3f43")).collect();
    match multicall(c, url, &calls, blk) { Ok(r) => for (k, &i) in ids.iter().enumerate() { match r.get(k).cloned().flatten() { Some(v) if v.len() >= 66 => { e.pools[i].fee = word(&v, 0).to::<u64>() as u32; MET.fee_refresh.fetch_add(1, Relaxed); } _ => { e.dirty.insert(i); } } },
        Err(_) => { for &i in &ids { e.dirty.insert(i); } } }
}
fn engine_loop(shared: Arc<RwLock<Arc<St>>>, mut e: Eng, url: String, py: String) {
    let c = reqwest::blocking::Client::builder().timeout(Duration::from_secs(20)).build().unwrap();
    let env = |k: &str, d: u64| std::env::var(k).ok().and_then(|x| x.parse().ok()).unwrap_or(d);
    let poll = Duration::from_millis(env("ENGINE_POLL_MS", 300)); let max_gap = env("ENGINE_MAX_GAP", 20_000);
    let resync_s = env("ENGINE_RESYNC_S", 90); let verify_every = env("ENGINE_VERIFY_EVERY", 20);
    let mut last_rebuild = Instant::now(); let mut n: u64 = 0;
    let (tx, rx) = std::sync::mpsc::channel::<Eng>(); let mut rebuilding = false;
    loop { let t = Instant::now(); n += 1;
        if let Ok(ne) = rx.try_recv() { rebuilding = false; last_rebuild = Instant::now();
            // keep newer incremental state for pools that are clean; take snapshot for dirty ones only if snapshot is not older... simplest exact: adopt snapshot, replay from its block
            e = ne; }
        let r: Result<(), String> = (|| {
            let head = hx(&rpc(&c, &url, "eth_blockNumber", json!([]))?); let seen = Instant::now(); MET.head.store(head, Relaxed);
            let want = if head > e.block + max_gap { MET.gaps.fetch_add(1, Relaxed); Some("gap") } else if !e.dirty.is_empty() && last_rebuild.elapsed().as_secs() > resync_s { Some("resync_dirty") } else { None };
            if let (Some(why), false) = (want, rebuilding) { rebuilding = true; let (tx, url, py) = (tx.clone(), url.clone(), py.clone());
                std::thread::spawn(move || { let c = reqwest::blocking::Client::new(); if let Some(ne) = rebuild(&c, &url, &py, why) { let _ = tx.send(ne); } }); }
            // reorg: verify our tip hash every 4 polls; on mismatch roll back to newest ring entry whose hash still matches
            if n % 4 == 0 { if let Some((b, h)) = e.hash.clone() { let cur = rpc(&c, &url, "eth_getBlockByNumber", json!([format!("0x{b:x}"), false]))?["hash"].as_str().unwrap_or("").to_string();
                if cur != h { MET.reorgs.fetch_add(1, Relaxed); log("reorg", json!({"block": b}));
                    let mut restored = false;
                    while let Some((rb, rh, pools)) = e.ring.pop_back() { let ch = rpc(&c, &url, "eth_getBlockByNumber", json!([format!("0x{rb:x}"), false]))?["hash"].as_str().unwrap_or("").to_string();
                        if ch == rh { e.pools = pools; e.block = rb; e.hash = Some((rb, rh)); restored = true; MET.rollbacks.fetch_add(1, Relaxed); log("rollback", json!({"to": rb})); break; } }
                    if !restored { e.hash = None; if !rebuilding { rebuilding = true; let (tx, url, py) = (tx.clone(), url.clone(), py.clone()); std::thread::spawn(move || { let c = reqwest::blocking::Client::new(); if let Some(ne) = rebuild(&c, &url, &py, "reorg_deep") { let _ = tx.send(ne); } }); } }
                    return Ok(()); } } }
            if head <= e.block { return Ok(()); }
            let to = head.min(e.block + 1000);
            let logs = rpc(&c, &url, "eth_getLogs", json!([{"fromBlock": format!("0x{:x}", e.block + 1), "toBlock": format!("0x{to:x}"), "address": e.addrs()}]))?;
            let ta = Instant::now();
            let mut v: Vec<Value> = logs.as_array().cloned().unwrap_or_default(); v.sort_by_key(|l| (hx(&l["blockNumber"]), hx(&l["logIndex"])));
            let tip_hash = v.iter().rev().find(|l| hx(&l["blockNumber"]) == to).and_then(|l| l["blockHash"].as_str()).map(|x| x.to_string());
            for l in v.iter().filter(|l| !l["removed"].as_bool().unwrap_or(false)) { e.apply(l); }
            e.block = to; e.touched_alg.clear();
            refresh_dynamic_fees(&c, &url, &mut e, to);
            if n % verify_every == 0 { verify(&c, &url, &mut e, to); }
            let h = match tip_hash { Some(h) => h, None => if n % 4 == 3 { rpc(&c, &url, "eth_getBlockByNumber", json!([format!("0x{to:x}"), false]))?["hash"].as_str().unwrap_or("").to_string() } else { String::new() } };
            if !h.is_empty() { e.hash = Some((to, h.clone())); e.ring.push_back((to, h, e.pools.clone())); while e.ring.len() > 64 { e.ring.pop_front(); } }
            e.publish(&shared); MET.apply_us.store(ta.elapsed().as_micros() as u64, Relaxed);
            let mut u = UPD.lock().unwrap(); u.push(seen.elapsed().as_millis() as u64); if u.len() > 50000 { u.drain(0..25000); }
            Ok(())
        })();
        if let Err(x) = r { MET.errors.fetch_add(1, Relaxed); log("step_error", json!({"err": x.chars().take(200).collect::<String>()})); }
        if let Some(s) = poll.checked_sub(t.elapsed()) { std::thread::sleep(s); }
    }
}
fn cached_paths(st: &St, a: &str, b: &str, h: usize) -> Arc<Vec<Path>> {
    let key = (a.to_string(), b.to_string(), h);
    if let Some((ep, m)) = PATHS.read().unwrap().as_ref() { if *ep == st.epoch { if let Some(p) = m.get(&key) { return p.clone(); } } }
    let p = Arc::new(paths(&st.pools, a, b, h)); let mut g = PATHS.write().unwrap();
    if g.as_ref().map(|x| x.0 != st.epoch).unwrap_or(true) { *g = Some((st.epoch, HashMap::new())); }
    g.as_mut().unwrap().1.insert(key, p.clone()); p
}
fn quote(q: &HashMap<String, String>, st: &St) -> Result<Value, String> {
    let tin = q.get("tin").ok_or("tin")?.to_lowercase(); let tout = q.get("tout").ok_or("tout")?.to_lowercase(); if tin == tout { return Err("same token".into()); }
    let amt = U256::from_str_radix(q.get("amt").ok_or("amt")?, 10).map_err(|_| "amt")?; if amt.is_zero() { return Err("zero amount".into()); }
    let fast = q.get("mode").map(|m| m == "fast").unwrap_or(false);
    let gp = U256::from(q.get("gp").and_then(|x| x.parse::<u64>().ok()).unwrap_or(20_000_000));
    let (chunks, hops, topk) = if fast { (4u32, 2usize, 4usize) } else { (32, 3, 12) };
    let t = Instant::now(); let ps = cached_paths(st, &tin, &tout, hops); if ps.is_empty() { return Err("no path".into()); }
    let eth_to_out = |wei: U256| -> U256 { if tout == WETH { return wei; } let pw = cached_paths(st, WETH, &tout, 2); let pr = U256::from(10u64.pow(16));
        pw.iter().filter_map(|p| quote_path(&st.pools, p, pr)).max().unwrap_or(U256::ZERO) * wei / pr };
    let gh = eth_to_out(U256::from(120_000u64) * gp); let gb = eth_to_out(U256::from(60_000u64) * gp);
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| route_k(&st.pools, &ps, amt, chunks, gh, gb, topk))).map_err(|_| "route panic")?;
    let us = t.elapsed().as_micros() as u64; LAT.lock().unwrap().push((fast, us));
    let nh: usize = r.legs.iter().map(|l| l.0.len()).sum(); let gross = r.out + gh * U256::from(nh) + gb;
    let legs: Vec<_> = r.legs.iter().map(|(path, la)| json!({"amountIn": la.to_string(), "hops": path.iter().map(|h| { let p = &st.pools[h.pool];
        json!({"pool": p.addr, "venue": p.venue, "fee": p.fee, "zfo": h.zfo, "tokenIn": if h.zfo {&p.token0} else {&p.token1}, "tokenOut": if h.zfo {&p.token1} else {&p.token0}}) }).collect::<Vec<_>>()})).collect();
    Ok(json!({"block": st.block, "tokenIn": tin, "tokenOut": tout, "amountIn": amt.to_string(), "grossOut": gross.to_string(), "netOut": r.out.to_string(),
        "gasUnits": 60_000 + 120_000 * nh, "legs": legs, "mode": if fast {"fast"} else {"best"}, "compute_us": us}))
}
fn pool_quote(q: &HashMap<String, String>, st: &St) -> Result<Value, String> {
    let a = q.get("pool").ok_or("pool")?.to_lowercase(); let zfo = q.get("zfo").map(|x| x == "1" || x == "true").unwrap_or(true);
    let amt = U256::from_str_radix(q.get("amt").ok_or("amt")?, 10).map_err(|_| "amt")?;
    let p = st.pools.iter().find(|p| p.addr == a).ok_or("unknown pool")?;
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| { let mut c = p.clone(); c.swap_exact_in(zfo, amt, None) })).map_err(|_| "sim panic")?;
    Ok(json!({"block": st.block, "pool": a, "venue": p.venue, "zfo": zfo, "amountIn": amt.to_string(), "used": r.0.to_string(), "out": r.1.to_string(), "liq": p.liq.to_string()}))
}
fn pct(v: &mut Vec<u64>, p: f64) -> u64 { if v.is_empty() { return 0; } v.sort(); v[((v.len() - 1) as f64 * p) as usize] }
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let bind = std::env::var("ENGINE_BIND").unwrap_or("127.0.0.1:8782".into());
    let url = std::env::var("RH_RPC").unwrap_or("https://rpc.mainnet.chain.robinhood.com".into());
    let py = std::env::var("PYTHON").unwrap_or(".venv/bin/python".into());
    let c = reqwest::blocking::Client::new();
    let mut e = rebuild(&c, &url, &py, "cold_start").expect("cold start snapshot");
    let shared = Arc::new(RwLock::new(Arc::new(St { block: 0, pools: vec![], toks: HashMap::new(), epoch: 0 }))); e.publish(&shared);
    { let s = shared.clone(); std::thread::spawn(move || engine_loop(s, e, url, py)); }
    let l = TcpListener::bind(&bind).expect("bind"); log("listening", json!({"bind": bind}));
    for conn in l.incoming() { let Ok(mut c) = conn else { continue }; let sh = shared.clone(); std::thread::spawn(move || { let _ = c.set_nodelay(true); let mut buf = [0u8; 8192];
        loop { let n = match c.read(&mut buf) { Ok(0) | Err(_) => break, Ok(n) => n };
            let req = String::from_utf8_lossy(&buf[..n]); let path = req.lines().next().unwrap_or("").split_whitespace().nth(1).unwrap_or("/").to_string();
            let (route, qs) = path.split_once('?').unwrap_or((&path, ""));
            let q: HashMap<String, String> = qs.split('&').filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.into(), v.into())).collect();
            let st = sh.read().unwrap().clone(); let head = MET.head.load(Relaxed);
            let (code, body, ct) = match route {
                "/quote" | "/pool_quote" if head.saturating_sub(st.block) > std::env::var("ENGINE_STALE_BLOCKS").ok().and_then(|x| x.parse().ok()).unwrap_or(30u64) => {
                    MET.stale_rejects.fetch_add(1, Relaxed); (503, json!({"error": "stale state", "block": st.block, "head": head}).to_string(), "application/json") }
                "/quote" | "/pool_quote" => { MET.quotes.fetch_add(1, Relaxed); match if route == "/quote" { quote(&q, &st) } else { pool_quote(&q, &st) } {
                    Ok(mut v) => { v["headBlock"] = json!(head); v["lagBlocks"] = json!(head.saturating_sub(st.block)); (200, v.to_string(), "application/json") }
                    Err(x) => { MET.qerr.fetch_add(1, Relaxed); (400, json!({"error": x}).to_string(), "application/json") } } }
                "/health" => { let lag = head.saturating_sub(st.block); (if lag < 50 { 200 } else { 503 }, json!({"ok": lag < 50, "block": st.block, "head": head, "lagBlocks": lag, "pools": st.pools.len(), "dirty": MET.dirty.load(Relaxed)}).to_string(), "application/json") }
                "/freshness" => (200, json!(*FRESH.lock().unwrap()).to_string(), "application/json"),
                "/metrics" => { let l = LAT.lock().unwrap(); let mut f: Vec<u64> = l.iter().filter(|x| x.0).map(|x| x.1).collect(); let mut b: Vec<u64> = l.iter().filter(|x| !x.0).map(|x| x.1).collect(); drop(l);
                    let m = &*MET; let mut s = String::new();
                    for (k, v) in [("head", m.head.load(Relaxed)), ("state_block", st.block), ("lag_blocks", head.saturating_sub(st.block)), ("logs_total", m.logs.load(Relaxed)), ("swaps_applied_total", m.swaps.load(Relaxed)),
                        ("liquidity_events_total", m.liq.load(Relaxed)), ("unknown_events_total", m.unknown.load(Relaxed)), ("dirty_pools", m.dirty.load(Relaxed)), ("rebuilds_total", m.rebuilds.load(Relaxed)),
                        ("reorgs_total", m.reorgs.load(Relaxed)), ("gaps_total", m.gaps.load(Relaxed)), ("errors_total", m.errors.load(Relaxed)), ("publishes_total", m.publishes.load(Relaxed)),
                        ("quotes_total", m.quotes.load(Relaxed)), ("quote_errors_total", m.qerr.load(Relaxed)), ("last_apply_us", m.apply_us.load(Relaxed)), ("pools", st.pools.len() as u64), ("verified_total", m.verified.load(Relaxed)), ("diverged_total", m.diverged.load(Relaxed)), ("fee_refresh_total", m.fee_refresh.load(Relaxed)), ("rollbacks_total", m.rollbacks.load(Relaxed)), ("stale_rejects_total", m.stale_rejects.load(Relaxed))] { s += &format!("ticking_{k} {v}\n"); }
                    for (mode, v) in [("fast", &mut f), ("best", &mut b)] { for q in [0.5, 0.99] { s += &format!("ticking_quote_compute_us{{mode=\"{mode}\",q=\"{q}\"}} {}\n", pct(v, q)); } }
                    { let mut u = UPD.lock().unwrap().clone(); for q in [0.5, 0.99] { s += &format!("ticking_head_to_publish_ms{{q=\"{q}\"}} {}\n", pct(&mut u, q)); } }
                    (200, s, "text/plain") }
                "/divergences" => (200, json!(*DIVLOG.lock().unwrap()).to_string(), "application/json"),
                _ => (404, "{}".into(), "application/json") };
            if c.write_all(format!("HTTP/1.1 {code} X\r\nContent-Type: {ct}\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes()).is_err() { break; } } }); }
}
