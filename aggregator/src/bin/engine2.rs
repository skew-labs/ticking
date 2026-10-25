//! ticking-engine v2 (Robinhood Chain): event-driven exact state + isolated quote cores.
//! Trigger: QuickNode WSS `newHeads` (primary). Per head: ONE JSON-RPC batch to the fastest HTTP source
//!   [eth_getLogs{blockHash, topics}, Multicall3(fee() of dynamic-fee pools) @hash] -> exact apply -> publish (ArcSwap).
//! Gap filler / cross-check: independent HTTP poller (public RPC) of eth_blockNumber; if WSS goes quiet or behind,
//!   the applier back-fills by range getLogs and WSS is reconnected + resubscribed automatically.
//! Reorg: parentHash of every head checked against our tip; rollback to newest matching ring snapshot (64 blocks).
//! Universe: V3-family factory PoolCreated + V3 Initialize + V4 Initialize events are applied, so new pools are
//!   built exactly from creation (all logs are fetched by topic, not by address list).
//! Verify: every N blocks Multicall3 slot0+liquidity (V3-family) and PoolManager.extsload slot0+liquidity (V4).
//! Algebra/Alandale: DISABLED in quotes (dynamic plugin fee not reproduced to 0 wei).
//! Quotes: FAST port (dedicated pinned threads) and BEST port (bounded worker pool on other cores).
use arc_swap::ArcSwap; use core::*; use core::router::*; use ruint::aliases::U256; use serde_json::{json, Value};
use std::{collections::{HashMap, HashSet, VecDeque}, io::{BufRead, BufReader, Write}, net::{TcpListener, TcpStream}, sync::{Arc, Mutex, mpsc, atomic::{AtomicU64, AtomicBool, Ordering::Relaxed}}, time::{Duration, Instant}};

const T_SWAP: &str = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67";
const T_MINT: &str = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde";
/// PancakeV3-style Swap (giga): UniV3 fields + protocolFeesToken0/1 -> same first 5 words
const T_SWAP_PCS: &str = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83";
/// Ramses CL Mint(sender, owner idx, uint256 index, tickLower idx, tickUpper idx, amount, a0, a1) -> amount = data word 2
const T_MINT_RA: &str = "0xd78218c0d304e8893cb3200abe394bbc8d5b7804d9c51f236df9fdcf481d02d3";
/// Ramses FeeAdjustment(uint24 old, uint24 new) (verified vs fee() on chain)
const T_FEEADJ: &str = "0x0cba87189055d3b5ab05c96fbd641bc766576c9e7cf0d195bdfb58a0c6a6df24";
const T_BURN: &str = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c";
const T_V3INIT: &str = "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95";
const T_CREATED: &str = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118";
const T_V4SWAP: &str = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f";
const T_V4MOD: &str = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec";
const T_V4INIT: &str = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438";
const PM: &str = "0x8366a39cc670b4001a1121b8f6a443a643e40951";
const WETH: &str = "0x0bd7d308f8e1639fab988df18a8011f41eacad73";
const MC3: &str = "0xca11bde05977b3631167028862be2a173976ca11";
const FACTORIES: [(&str, &str); 5] = [("0x1f7d7550b1b028f7571e69a784071f0205fd2efa", "univ3"), ("0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8", "ramses"),
    ("0xe51960f1b45f1c9fb6d166e6a884f866fc70433b", "sushi"), ("0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3", "up"), ("0xece6ecd61177336ea6fb9b17937ac439d85ee20b", "giga")];

// ---------------- metrics ----------------
#[derive(Default)] struct M { ws_head: AtomicU64, poll_head: AtomicU64, block: AtomicU64, logs: AtomicU64, swaps: AtomicU64, liq: AtomicU64, created: AtomicU64,
    reorgs: AtomicU64, rollbacks: AtomicU64, backfill_blocks: AtomicU64, ws_reconnects: AtomicU64, ws_heads: AtomicU64, poll_triggers: AtomicU64,
    verified: AtomicU64, diverged: AtomicU64, errors: AtomicU64, fq: AtomicU64, bq: AtomicU64, qerr: AtomicU64, stale: AtomicU64, dirty: AtomicU64, rebuilds: AtomicU64, pushchk: AtomicU64, ws_retries: AtomicU64, bloom_skip: AtomicU64, verify_runs: AtomicU64, verify_err: AtomicU64, verify_ms: AtomicU64, fee_applied: AtomicU64, pushmis: AtomicU64, patched: AtomicU64, rb_patched: AtomicU64 }
static MET: std::sync::LazyLock<M> = std::sync::LazyLock::new(M::default);
static H2Q: Mutex<Vec<u64>> = Mutex::new(vec![]);     // head received (ws) -> state published, microseconds
static FLAT: Mutex<Vec<u64>> = Mutex::new(vec![]); static BLAT: Mutex<Vec<u64>> = Mutex::new(vec![]);
static LAGH: [AtomicU64; 10] = [const { AtomicU64::new(0) }; 10];
static CHECKQ: Mutex<VecDeque<(String, usize, Instant)>> = Mutex::new(VecDeque::new());
static DIVLOG: Mutex<Vec<Value>> = Mutex::new(vec![]);
fn push(v: &Mutex<Vec<u64>>, x: u64) { let mut g = v.lock().unwrap(); g.push(x); if g.len() > 200_000 { g.drain(0..100_000); } }
fn pct(v: &Mutex<Vec<u64>>, p: f64) -> u64 { let mut s = v.lock().unwrap().clone(); if s.is_empty() { return 0; } s.sort_unstable(); s[((s.len() - 1) as f64 * p) as usize] }
fn redact(s: &str) -> String { let mut o = s.to_string(); for k in ["RH_QN_HTTP", "RH_WSS", "RH_QN_HTTP2", "RH_WSS2"] { if let Ok(u) = std::env::var(k) { if !u.is_empty() { o = o.replace(&u, "<redacted>"); let h = u.split("://").nth(1).unwrap_or("").split('/').next().unwrap_or(""); if !h.is_empty() { o = o.replace(h, "<redacted-host>"); } } } } o }
fn log(msg: &str, extra: Value) { let mut v = json!({"svc":"ticking-engine","msg":msg,"ts":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64});
    if let (Some(o), Some(e)) = (v.as_object_mut(), extra.as_object()) { for (k, x) in e { o.insert(k.clone(), x.clone()); } } eprintln!("{}", redact(&v.to_string())); }

// ---------------- rpc ----------------
struct Rpc { c: reqwest::blocking::Client, urls: Vec<String> } // urls[0] = primary (QuickNode), others fallback (public)
impl Rpc {
    fn call(&self, m: &str, p: Value) -> Result<Value, String> { let r = self.batch(&[(m.to_string(), p)])?; r.into_iter().next().flatten().ok_or("null".into()) }
    fn batch(&self, calls: &[(String, Value)]) -> Result<Vec<Option<Value>>, String> {
        let body: Vec<Value> = calls.iter().enumerate().map(|(i, (m, p))| json!({"jsonrpc":"2.0","id":i,"method":m,"params":p})).collect();
        let mut last = String::new();
        for (ui, u) in self.urls.iter().enumerate() { for att in 0..3 {
            match self.c.post(u).json(&body).send().and_then(|r| r.json::<Value>()) {
                Ok(Value::Array(mut v)) => { if v.iter().any(|x| x.get("error").map(|e| e.to_string().contains("429") || e.to_string().contains("ate limit") || e.to_string().contains("-32007") || e.to_string().contains("limit reached")).unwrap_or(false)) { last = "429".into(); std::thread::sleep(Duration::from_millis(100 * (att + 1))); continue; }
                    v.sort_by_key(|x| x["id"].as_u64().unwrap_or(0));
                    if let Some(e) = v.iter().find_map(|x| x.get("error")) { if e.to_string().contains("not found") || e.to_string().contains("unknown block") || e.to_string().contains("unsupported block") { last = e.to_string(); std::thread::sleep(Duration::from_millis(30)); continue; } }
                    return Ok(v.into_iter().map(|x| x.get("result").cloned()).collect()); }
                Ok(o) => { last = o.to_string(); std::thread::sleep(Duration::from_millis(50)); }
                Err(e) => { last = e.to_string(); std::thread::sleep(Duration::from_millis(50)); } } } let _ = ui; }
        Err(redact(&last))
    }
}
fn hx(v: &Value) -> u64 { u64::from_str_radix(v.as_str().unwrap_or("0x0").trim_start_matches("0x"), 16).unwrap_or(0) }
fn word(d: &str, i: usize) -> U256 { let d = d.trim_start_matches("0x"); if d.len() < i * 64 + 64 { return U256::ZERO; } U256::from_str_radix(&d[i * 64..i * 64 + 64], 16).unwrap_or(U256::ZERO) }
fn sword(w: U256) -> i128 { if w.bit(255) { -((U256::ZERO.wrapping_sub(w)).to::<u128>() as i128) } else { w.to::<u128>() as i128 } }
fn stick(w: U256) -> i32 { sword(w) as i32 }
fn addr_of(w: U256) -> String { format!("0x{:040x}", w & ((U256::from(1u8) << 160usize) - U256::from(1u8))) }
fn w256(x: u64) -> String { format!("{x:064x}") }
fn keccak(b: &[u8]) -> [u8; 32] { use tiny_keccak::Hasher; let mut k = tiny_keccak::Keccak::v256(); k.update(b); let mut o = [0u8; 32]; k.finalize(&mut o); o }
/// Multicall3.tryAggregate(false, (target, calldata)[]) encoded as one eth_call params at `blk` (hash or number tag)
fn mc_params(calls: &[(String, String)], blk: &str) -> Value {
    let n = calls.len() as u64; let mut heads = String::new(); let mut tails = String::new(); let mut off = 32 * n;
    for (a, cd) in calls { let cd = cd.trim_start_matches("0x"); let ln = cd.len() / 2; let padded = format!("{:0<width$}", cd, width = ((ln + 31) / 32) * 64);
        heads += &w256(off); let t = format!("{:0>64}{}{}{}", a.trim_start_matches("0x"), w256(64), w256(ln as u64), padded); off += (t.len() / 2) as u64; tails += &t; }
    let blkp = if blk.len() == 66 { json!({"blockHash": blk}) } else { json!(blk) };
    json!([{"to": MC3, "data": format!("0xbce38bd7{}{}{}{}{}", w256(0), w256(64), w256(n), heads, tails), "gas": "0x3b9aca00"}, blkp])
}
fn mc_decode(r: &str) -> Vec<Option<String>> { let r = r.trim_start_matches("0x");
    let g = |i: usize| -> usize { if r.len() < i * 2 + 64 { return 0; } usize::from_str_radix(&r[i * 2 + 48..i * 2 + 64], 16).unwrap_or(0) };
    let base = g(0); let m = g(base); let mut out = vec![];
    for i in 0..m { let eo = base + 32 + g(base + 32 + 32 * i); let ok = g(eo) == 1; let ro = eo + g(eo + 32); let ln = g(ro);
        out.push(if ok && r.len() >= (ro + 32 + ln) * 2 { Some(format!("0x{}", &r[(ro + 32) * 2..(ro + 32 + ln) * 2])) } else { None }); }
    out }

// ---------------- state ----------------
struct St { block: u64, hash: String, pools: Arc<Vec<Pool>>, topo: Arc<Vec<Pool>>, epoch: u64, published: Instant }
struct Eng { pools: Vec<Pool>, idx: HashMap<String, usize>, block: u64, hash: String, dirty: HashSet<usize>, ring: VecDeque<(u64, String, Vec<Pool>)>,
    epoch: u64, enabled_sig: Vec<bool>, topo: Arc<Vec<Pool>>, pending_v3: HashMap<String, (String, String, u32, i32, String)>, fee_stale: HashSet<usize> }
impl Eng {
    fn from_snapshot(path: &str) -> Eng { let (b, pools, _) = load(path); let idx = pools.iter().enumerate().map(|(i, p)| (p.addr.clone(), i)).collect();
        Eng { pools, idx, block: b, hash: String::new(), dirty: HashSet::new(), ring: VecDeque::new(), epoch: 0, enabled_sig: vec![], topo: Arc::new(vec![]), pending_v3: HashMap::new(), fee_stale: HashSet::new() } }
    fn set_liq_ticks(p: &mut Pool, lo: i32, hi: i32, dl: i128) {
        let mut t: Vec<(i32, i128)> = (*p.ticks).clone();
        for (tk, d) in [(lo, dl), (hi, -dl)] { match t.binary_search_by_key(&tk, |x| x.0) { Ok(i) => { t[i].1 += d; if t[i].1 == 0 { t.remove(i); } } Err(i) => if d != 0 { t.insert(i, (tk, d)) } } }
        p.ticks = Arc::new(t);
        if lo <= p.tick && p.tick < hi { p.liq = if dl < 0 { p.liq.saturating_sub(dl.unsigned_abs()) } else { p.liq + dl as u128 }; }
    }
    fn new_pool(&mut self, addr: String, t0: String, t1: String, fee: u32, spacing: i32, venue: String) {
        if self.idx.contains_key(&addr) { return; }
        let big = U256::from_str_radix("1606938044258990275541962092341162602522202993782792835301376", 10).unwrap();
        self.pools.push(Pool { addr: addr.clone(), token0: t0, token1: t1, fee, protocol_fee: 0, spacing, sqrt_p: U256::ZERO, tick: 0, liq: 0, bal0: big, bal1: big, crossed: 0, ticks: Arc::new(vec![]), venue });
        self.idx.insert(addr, self.pools.len() - 1); MET.created.fetch_add(1, Relaxed);
    }
    fn apply(&mut self, lg: &Value) {
        MET.logs.fetch_add(1, Relaxed);
        let a = lg["address"].as_str().unwrap_or("").to_lowercase(); let t: Vec<String> = lg["topics"].as_array().map(|x| x.iter().map(|v| v.as_str().unwrap_or("").to_lowercase()).collect()).unwrap_or_default();
        let d = lg["data"].as_str().unwrap_or("0x"); if t.is_empty() { return; } let tp = |i: usize| U256::from_str_radix(t.get(i).map(|x| &x[2..]).unwrap_or("0"), 16).unwrap_or(U256::ZERO);
        if t[0] == T_CREATED { if let Some((_, v)) = FACTORIES.iter().find(|f| f.0 == a) { if t.len() == 4 {
                let pool = addr_of(word(d, 1)); let (t0, t1) = (addr_of(tp(1)), addr_of(tp(2)));
                self.new_pool(pool.clone(), t0, t1, tp(3).to::<u64>() as u32, stick(word(d, 0)), v.to_string()); log("pool_created", json!({"pool": pool, "venue": v})); } } return; }
        if a == PM {
            if t[0] == T_V4INIT && t.len() == 4 { let hooks = addr_of(word(d, 2)); if hooks != "0x0000000000000000000000000000000000000000" { return; } // zero-hook only (exact)
                let id = t[1].clone(); let norm = |x: String| if x == "0x0000000000000000000000000000000000000000" { WETH.to_string() } else { x };
                if addr_of(tp(2)) == "0x0000000000000000000000000000000000000000" { NATIVE_V4.write().unwrap().insert(id.clone()); }
                let (c0, c1) = (norm(addr_of(tp(2))), norm(addr_of(tp(3))));
                self.new_pool(id.clone(), c0, c1, word(d, 0).to::<u64>() as u32, stick(word(d, 1)), "v4".into());
                if let Some(&i) = self.idx.get(&id) { self.pools[i].sqrt_p = word(d, 3); self.pools[i].tick = stick(word(d, 4)); } return; }
            let Some(&i) = t.get(1).and_then(|id| self.idx.get(id)) else { return }; let p = &mut self.pools[i];
            if t[0] == T_V4SWAP { p.sqrt_p = word(d, 2); p.liq = word(d, 3).to::<u128>(); p.tick = stick(word(d, 4)); MET.swaps.fetch_add(1, Relaxed); }
            else if t[0] == T_V4MOD { Self::set_liq_ticks(p, stick(word(d, 0)), stick(word(d, 1)), sword(word(d, 2))); MET.liq.fetch_add(1, Relaxed); }
            return; }
        let Some(&i) = self.idx.get(&a) else { return }; let p = &mut self.pools[i];
        if p.venue == "alandale" { return; } // disabled venue: not tracked
        if t[0] == T_SWAP || t[0] == T_SWAP_PCS { let (a0, a1) = (sword(word(d, 0)), sword(word(d, 1)));
            p.sqrt_p = word(d, 2); p.liq = word(d, 3).to::<u128>(); p.tick = stick(word(d, 4));
            p.bal0 = if a0 >= 0 { p.bal0 + U256::from(a0 as u128) } else { p.bal0.saturating_sub(U256::from(a0.unsigned_abs())) };
            p.bal1 = if a1 >= 0 { p.bal1 + U256::from(a1 as u128) } else { p.bal1.saturating_sub(U256::from(a1.unsigned_abs())) }; MET.swaps.fetch_add(1, Relaxed); }
        else if t[0] == T_MINT && t.len() == 4 { Self::set_liq_ticks(p, stick(tp(2)), stick(tp(3)), word(d, 1).to::<u128>() as i128); MET.liq.fetch_add(1, Relaxed); }
        else if t[0] == T_MINT_RA && t.len() == 4 { Self::set_liq_ticks(p, stick(tp(2)), stick(tp(3)), word(d, 2).to::<u128>() as i128); MET.liq.fetch_add(1, Relaxed); }
        else if t[0] == T_FEEADJ { p.fee = word(d, 1).to::<u64>() as u32; }
        else if t[0] == T_BURN && t.len() == 4 { Self::set_liq_ticks(p, stick(tp(2)), stick(tp(3)), -(word(d, 0).to::<u128>() as i128)); MET.liq.fetch_add(1, Relaxed); }
        else if t[0] == T_V3INIT { p.sqrt_p = word(d, 0); p.tick = stick(word(d, 1)); }
    }
    fn enabled(&self, i: usize) -> bool { let p = &self.pools[i]; p.venue != "alandale" && !self.dirty.contains(&i) && !self.fee_stale.contains(&i) && !p.sqrt_p.is_zero() && !p.ticks.is_empty() }
    fn publish(&mut self, cell: &ArcSwap<St>) {
        let sig: Vec<bool> = (0..self.pools.len()).map(|i| self.enabled(i)).collect();
        // topology = every structurally usable pool (independent of per-block dirty/fee-stale/liquidity) -> path cache only invalidates on new pools
        let tsig: Vec<bool> = (0..self.pools.len()).map(|i| { let p = &self.pools[i]; p.venue != "alandale" && !p.sqrt_p.is_zero() && !p.ticks.is_empty() }).collect();
        if tsig != self.enabled_sig { let sig = &tsig; self.epoch += 1; let mut topo = self.pools.clone(); for (i, p) in topo.iter_mut().enumerate() { p.liq = if sig[i] { 1 } else { 0 }; } self.topo = Arc::new(topo); self.enabled_sig = tsig.clone(); build_bloom(&self.pools); }
        let mut pools = self.pools.clone(); for (i, p) in pools.iter_mut().enumerate() { if !sig[i] { p.liq = 0; } }
        cell.store(Arc::new(St { block: self.block, hash: self.hash.clone(), pools: Arc::new(pools), topo: self.topo.clone(), epoch: self.epoch, published: Instant::now() }));
        MET.block.store(self.block, Relaxed); MET.dirty.store(self.dirty.len() as u64, Relaxed);
    }
    fn dyn_fee_ids(&self) -> Vec<usize> { (0..self.pools.len()).filter(|&i| self.pools[i].venue == "up").collect() }
}
fn topics() -> Value { json!([[T_SWAP, T_SWAP_PCS, T_MINT, T_MINT_RA, T_FEEADJ, T_BURN, T_V3INIT, T_CREATED, T_V4SWAP, T_V4MOD, T_V4INIT]]) }
/// Fetch + apply one block by hash: ONE batch [getLogs(blockHash), Multicall fee()@hash]
fn apply_block(rpc: &Rpc, e: &mut Eng, num: u64, hash: &str, pre: Option<(Vec<Value>, Option<String>)>) -> Result<(), String> {
    let ids = e.dyn_fee_ids(); let calls: Vec<(String, String)> = ids.iter().map(|&i| (e.pools[i].addr.clone(), "0xddca3f43".to_string())).collect();
    let r: Vec<Option<Value>> = if let Some((l, f)) = pre { vec![Some(Value::Array(l)), f.map(Value::String)] } else {
    let mut b = vec![("eth_getLogs".to_string(), json!([{"blockHash": hash, "topics": topics()}]))];
    if !calls.is_empty() { b.push(("eth_call".to_string(), mc_params(&calls, hash))); }
    rpc.batch(&b)? }; let logs = r.first().cloned().flatten().ok_or("logs null")?;
    let mut v: Vec<Value> = logs.as_array().cloned().unwrap_or_default(); v.sort_by_key(|l| hx(&l["logIndex"]));
    for l in &v { e.apply(l); }
    *DYNCALLS.lock().unwrap() = calls.clone();
    if let Some(Some(m)) = r.get(1) { e.fee_stale.clear(); for (k, x) in mc_decode(m.as_str().unwrap_or("")).into_iter().enumerate() { if let (Some(&i), Some(x)) = (ids.get(k), x) { e.pools[i].fee = word(&x, 0).to::<u64>() as u32; } } }
    else if !ids.is_empty() { e.fee_stale = ids.iter().cloned().collect(); }
    e.block = num; e.hash = hash.to_string(); e.ring.push_back((num, hash.to_string(), e.pools.clone())); while e.ring.len() > 64 { e.ring.pop_front(); }
    Ok(())
}
/// Gap catch-up in ONE batch: range getLogs + both boundary headers (parent link checked at `from`, hash bound at `to`).
fn backfill(rpc: &Rpc, e: &mut Eng, from: u64, to: u64) -> Result<(), String> {
    let r = rpc.batch(&[("eth_getLogs".into(), json!([{"fromBlock": format!("0x{from:x}"), "toBlock": format!("0x{to:x}"), "topics": topics()}])),
        ("eth_getBlockByNumber".into(), json!([format!("0x{from:x}"), false])), ("eth_getBlockByNumber".into(), json!([format!("0x{to:x}"), false]))])?;
    let (Some(l), Some(hf), Some(ht)) = (r[0].clone(), r[1].clone(), r[2].clone()) else { return Err("backfill null".into()) };
    if !e.hash.is_empty() && hf["parentHash"].as_str() != Some(e.hash.as_str()) { return Err("backfill parent mismatch (reorg)".into()); }
    let toh = ht["hash"].as_str().unwrap_or("").to_string();
    let mut v = l.as_array().cloned().unwrap_or_default(); v.sort_by_key(|x| (hx(&x["blockNumber"]), hx(&x["logIndex"])));
    if v.iter().any(|x| hx(&x["blockNumber"]) == to && x["blockHash"].as_str() != Some(toh.as_str())) { return Err("backfill hash race".into()); }
    let fh = hf["hash"].as_str().unwrap_or("").to_string();
    if v.iter().any(|x| hx(&x["blockNumber"]) == from && x["blockHash"].as_str() != Some(fh.as_str())) { return Err("backfill hash race".into()); }
    if to > from { for x in &v { if hx(&x["blockNumber"]) < to { e.apply(x); } } e.block = to - 1; }
    let last: Vec<Value> = v.into_iter().filter(|x| hx(&x["blockNumber"]) == to).collect();
    apply_block(rpc, e, to, &toh, Some((last, None)))?; // fees: refreshed by the next live head (dyn-fee pools marked dirty until then)
    MET.backfill_blocks.fetch_add(to - from + 1, Relaxed); Ok(())
}
fn apply_fees(e: &mut Eng, m: &str) { let ids = e.dyn_fee_ids(); let d = mc_decode(m); if d.len() != ids.len() { return; }
    for (k, x) in d.into_iter().enumerate() { match x { Some(x) => e.pools[ids[k]].fee = word(&x, 0).to::<u64>() as u32, None => return } } e.fee_stale.clear(); MET.fee_applied.fetch_add(1, Relaxed); }
type Fix = (usize, U256, i32, u128);
/// TickingRouter v1, deployed on RH mainnet (contracts/DEPLOYED.md); Sourcify exact match
const ROUTER: &str = "0x9745ff56a6fc085b3e70a7509cd49ab6b97dcbd6"; // v1 (no fee, no native V4) - still accepted via ?router=v1
/// TickingRouter v2: 5 bps protocol fee on gross output (floor), native-ETH V4 hops (op flag 0x20). Set via ROUTER_V2 env until hard-coded.
fn router_v2() -> Option<String> { std::env::var("ROUTER_V2").ok().filter(|a| a.len() == 42).map(|a| a.to_lowercase()) }
const FEE_BPS: u64 = 5;
/// V4 pool ids keyed on native ETH (currency0 = address(0)); the engine trades them as WETH.
static NATIVE_V4: std::sync::LazyLock<std::sync::RwLock<HashSet<String>>> = std::sync::LazyLock::new(|| {
    let mut h = HashSet::new();
    if let Ok(t) = std::fs::read_to_string("data/rh_v4_keys.json") { if let Ok(v) = serde_json::from_str::<Value>(&t) { for k in v.as_array().cloned().unwrap_or_default() {
        if k["currency0"].as_str().map(|c| c.trim_start_matches("0x").chars().all(|x| x == '0')).unwrap_or(false) { if let Some(id) = k["id"].as_str() { h.insert(id.to_lowercase()); } } } } }
    std::sync::RwLock::new(h) });
/// abi.encodeWithSelector(swap(bytes) = 0x627dd56a, program)
fn swap_calldata(prog: &str) -> String { let h = prog.trim_start_matches("0x"); let n = h.len() / 2; let pad = (64 - h.len() % 64) % 64;
    format!("0x627dd56a{:064x}{:064x}{}{}", 32, n, h, "0".repeat(pad)) }
fn verify(rpc: &Rpc, e: &mut Eng, count: bool) -> Option<Vec<Fix>> { let mut fixes: Vec<Fix> = vec![];
    let blk = e.hash.clone(); let mut calls = vec![]; let mut which = vec![];
    for i in 0..e.pools.len() { if !e.enabled(i) { continue; } let p = &e.pools[i];
        if p.addr.len() == 42 { calls.push((p.addr.clone(), "0x3850c7bd".into())); calls.push((p.addr.clone(), "0x1a686502".into())); which.push((i, false)); }
        else { let mut k = hex_bytes(&p.addr); k.extend_from_slice(&[0u8; 31]); k.push(6); let s0 = keccak(&k); let mut l = U256::from_be_bytes(s0); l += U256::from(3u8);
            calls.push((PM.into(), format!("0x1e2eaeaf{}", hex(&s0)))); calls.push((PM.into(), format!("0x1e2eaeaf{:064x}", l))); which.push((i, true)); } }
    let t = Instant::now(); let mut r: Vec<Option<String>> = vec![];
    for ch in calls.chunks(600) { match rpc.call("eth_call", mc_params(ch, &blk)) { Ok(x) => r.extend(mc_decode(x.as_str().unwrap_or(""))), Err(x) => { MET.verify_err.fetch_add(1, Relaxed); log("verify_error", json!({"err": x.chars().take(160).collect::<String>()})); return None } } }
    if count { MET.verify_runs.fetch_add(1, Relaxed); } MET.verify_ms.store(t.elapsed().as_millis() as u64, Relaxed);
    for (k, &(i, v4)) in which.iter().enumerate() { let (Some(Some(s0)), Some(Some(l))) = (r.get(2 * k), r.get(2 * k + 1)) else { continue };
        let (sq, tk, lq) = if v4 { let w = word(s0, 0); let sq = w & ((U256::from(1u8) << 160usize) - U256::from(1u8)); let raw = ((w >> 160usize) & U256::from(0xffffffu32)).to::<u64>() as i32; let tk = if raw & 0x800000 != 0 { raw - 0x1000000 } else { raw };
                (sq, tk, (word(l, 0) & ((U256::from(1u8) << 128usize) - U256::from(1u8))).to::<u128>()) }
            else { (word(s0, 0), stick(word(s0, 1)), word(l, 0).saturating_to::<u128>()) };
        let p = &e.pools[i]; if count { MET.verified.fetch_add(1, Relaxed); }
        if sq != p.sqrt_p || tk != p.tick || lq != p.liq { fixes.push((i, sq, tk, lq)); if !count { continue; } MET.diverged.fetch_add(1, Relaxed);
            DIVLOG.lock().unwrap().push(json!({"block": e.block, "pool": p.addr, "venue": p.venue, "sqrt_ok": sq == p.sqrt_p, "tick_ok": tk == p.tick, "liq_ok": lq == p.liq}));
            log("diverged", json!({"block": e.block, "pool": p.addr, "venue": p.venue})); } }
    Some(fixes) }
/// patch exact on-chain sqrt/tick; a liquidity mismatch means the tick table may be off too -> dirty (excluded) until rebuild
fn patch(e: &mut Eng, f: &[Fix]) { for &(i, sq, tk, lq) in f { let p = &mut e.pools[i]; p.sqrt_p = sq; p.tick = tk; if p.liq != lq { p.liq = lq; e.dirty.insert(i); } } }
fn hex_bytes(s: &str) -> Vec<u8> { let s = s.trim_start_matches("0x"); (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap_or(0)).collect() }
fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }

// ---------------- head sources ----------------
#[derive(Clone)] struct Head { num: u64, hash: String, parent: String, at: Instant, src: &'static str, logs: Option<Vec<Value>>, fees: Option<String> }
static DYNCALLS: Mutex<Vec<(String, String)>> = Mutex::new(vec![]);
/// Header logsBloom pre-filter: a block whose bloom contains none of our pool addresses / V4 pool ids / factories / V4 Initialize
/// provably has no log we apply (bloom has no false negatives) -> apply instantly with zero RPC.
#[derive(Default)] struct BloomSet { v3: Vec<[u16; 3]>, v4: Vec<[u16; 3]>, fac: Vec<[u16; 3]>, pm: [u16; 3], v4init: [u16; 3], ready: bool }
fn bbits(b: &[u8]) -> [u16; 3] { let h = keccak(b); [0, 1, 2].map(|i| (((h[2 * i] as u16) << 8) | h[2 * i + 1] as u16) & 2047) }
fn bhas(bloom: &[u8], t: &[u16; 3]) -> bool { t.iter().all(|&i| bloom[255 - (i as usize) / 8] & (1u8 << (i % 8)) != 0) }
static BLOOM: std::sync::LazyLock<ArcSwap<BloomSet>> = std::sync::LazyLock::new(|| ArcSwap::from_pointee(BloomSet::default()));
fn build_bloom(pools: &[Pool]) { let mut b = BloomSet { pm: bbits(&hex_bytes(PM)), v4init: bbits(&hex_bytes(T_V4INIT)), ready: true, ..Default::default() };
    for p in pools { if p.addr.len() == 42 { b.v3.push(bbits(&hex_bytes(&p.addr))) } else { b.v4.push(bbits(&hex_bytes(&p.addr))) } }
    for (f, _) in FACTORIES { b.fac.push(bbits(&hex_bytes(f))); } BLOOM.store(Arc::new(b)); }
fn relevant(bloom_hex: Option<&str>) -> bool { let bs = BLOOM.load(); if !bs.ready { return true; } let Some(h) = bloom_hex else { return true }; let bl = hex_bytes(h); if bl.len() != 256 { return true; }
    bs.v3.iter().any(|t| bhas(&bl, t)) || bs.fac.iter().any(|t| bhas(&bl, t)) || (bhas(&bl, &bs.pm) && (bhas(&bl, &bs.v4init) || bs.v4.iter().any(|t| bhas(&bl, t)))) }
static PUSHED: std::sync::LazyLock<Mutex<HashMap<String, usize>>> = std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));
/// One persistent WSS: subscribe newHeads + logs(topics). On each head, pipeline eth_getLogs{blockHash} and the
/// dynamic-fee multicall @blockHash over the SAME socket (no HTTP handshake, ~1 RTT). Pushed logs are counted per
/// blockHash as an independent completeness cross-check (QN pushes ~3% of logs after the next head, so push alone
/// cannot finalize a block). Reader uses a short socket timeout so one thread both reads and writes.
/// Second WSS connection dedicated to the dynamic-fee multicall, so QN processes it in parallel with getLogs.
fn fee_ws(url: String, rx: mpsc::Receiver<(u64, String, Vec<(String, String)>)>, out: mpsc::Sender<(u64, Option<String>)>) {
    loop { let r: Result<(), String> = (|| {
        let (mut s, _) = tungstenite::connect(url.as_str()).map_err(|e| redact(&e.to_string()))?;
        if let tungstenite::stream::MaybeTlsStream::Rustls(t) = s.get_mut() { let _ = t.get_mut().set_nodelay(true); let _ = t.get_mut().set_read_timeout(Some(Duration::from_millis(1))); }
        loop {
            while let Ok((key, hash, calls)) = rx.try_recv() { let mut p = mc_params(&calls, &hash); p[0].as_object_mut().unwrap().remove("gas");
                s.send(tungstenite::Message::Text(json!({"jsonrpc":"2.0","id":key,"method":"eth_call","params":p}).to_string().into())).map_err(|e| e.to_string())?; }
            match s.read() { Ok(tungstenite::Message::Text(t)) => { let v: Value = serde_json::from_str(&t).unwrap_or(Value::Null);
                    if let Some(id) = v["id"].as_u64() { let _ = out.send((id, v["result"].as_str().map(|x| x.to_string()))); } }
                Ok(tungstenite::Message::Ping(p)) => { let _ = s.send(tungstenite::Message::Pong(p)); }
                Ok(_) => {}
                Err(tungstenite::Error::Io(e)) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => {}
                Err(e) => return Err(e.to_string()) } } })();
        log("fee_ws_down", json!({"err": r.err().unwrap_or_default().chars().take(120).collect::<String>()})); std::thread::sleep(Duration::from_millis(100)); }
}
static SEEN: std::sync::LazyLock<Mutex<(HashSet<String>, VecDeque<String>)>> = std::sync::LazyLock::new(|| Mutex::new((HashSet::new(), VecDeque::new())));
/// dual-endpoint race: first WSS to deliver a head hash wins and fetches its logs; the other drops it (dedupe by hash)
fn first_seen(h: &str) -> bool { let mut g = SEEN.lock().unwrap(); if !g.0.insert(h.to_string()) { return false; } g.1.push_back(h.to_string()); if g.1.len() > 4096 { let o = g.1.pop_front().unwrap(); g.0.remove(&o); } true }
static WINS: [AtomicU64; 2] = [AtomicU64::new(0), AtomicU64::new(0)];
fn ws_thread(url: String, tx: mpsc::Sender<Head>, alive: Arc<AtomicU64>, idx: usize, do_fees: bool) {
    let (ftx, frx) = mpsc::channel(); let (otx, orx) = mpsc::channel::<(u64, Option<String>)>();
    if do_fees { let u = url.clone(); std::thread::spawn(move || fee_ws(u, frx, otx)); }
    loop {
        let r: Result<(), String> = (|| {
            let (mut s, _) = tungstenite::connect(url.as_str()).map_err(|e| redact(&e.to_string()))?;
            if let tungstenite::stream::MaybeTlsStream::Rustls(t) = s.get_mut() { let _ = t.get_mut().set_nodelay(true); let _ = t.get_mut().set_read_timeout(Some(Duration::from_millis(1))); }
            let send = |s: &mut tungstenite::WebSocket<_>, v: Value| s.send(tungstenite::Message::Text(v.to_string().into())).map_err(|e: tungstenite::Error| e.to_string());
            send(&mut s, json!({"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]}))?;
            if std::env::var("WS_LOG_PUSH").map(|x| x == "1").unwrap_or(false) { send(&mut s, json!({"jsonrpc":"2.0","id":2,"method":"eth_subscribe","params":["logs", {"topics": topics()[0].clone().as_array().map(|a| json!([a])).unwrap()}]}))?; }
            let (mut head_sub, mut log_sub) = (String::new(), String::new());
            let mut pend: HashMap<u64, (Head, Option<Vec<Value>>, Option<Option<String>>, usize, u32)> = HashMap::new(); // id -> partial
            let mut nid: u64 = 10; let mut last_rx = Instant::now(); let mut last_ping = Instant::now(); let mut feehash: HashMap<u64, String> = HashMap::new();
            loop {
                while let Ok((id, res)) = orx.try_recv() { if let Some(hash) = feehash.remove(&id) { let _ = tx.send(Head { num: 0, hash, parent: String::new(), at: Instant::now(), src: "fee", logs: None, fees: res }); } }
                if last_rx.elapsed() > Duration::from_millis(1000) && last_ping.elapsed() > Duration::from_millis(1000) { last_ping = Instant::now(); let _ = s.send(tungstenite::Message::Ping(vec![].into())); }
                let m = match s.read() { Ok(m) => m, Err(tungstenite::Error::Io(e)) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => {
                        if last_rx.elapsed() > Duration::from_millis(4000) { return Err("ws silent >4s (ping unanswered)".into()); } continue }
                    Err(e) => return Err(e.to_string()) };
                let at = Instant::now(); last_rx = at;
                let tungstenite::Message::Text(t) = m else { if let tungstenite::Message::Ping(p) = m { let _ = s.send(tungstenite::Message::Pong(p)); } continue };
                let v: Value = serde_json::from_str(&t).unwrap_or(Value::Null);
                if let Some(id) = v.get("id").and_then(|x| x.as_u64()) {
                    if id == 1 { head_sub = v["result"].as_str().unwrap_or("").into(); log("ws_subscribed", json!({"what":"newHeads"})); continue; }
                    if id == 2 { log_sub = v["result"].as_str().unwrap_or("").into(); log("ws_subscribed", json!({"what":"logs"})); continue; }
                    let key = id & !1; let Some(e) = pend.get_mut(&key) else { continue };
                    if v.get("error").is_some() { MET.ws_retries.fetch_add(1, Relaxed); let h = e.0.clone(); e.4 += 1;
                        if e.4 > 20 { log("ws_rpc_give_up", json!({"err": redact(&v["error"].to_string()), "block": h.num})); pend.remove(&key); continue; }
                        std::thread::sleep(Duration::from_millis(20));
                        send(&mut s, json!({"jsonrpc":"2.0","id":key,"method":"eth_getLogs","params":[{"blockHash": h.hash, "topics": topics()}]}))?; continue; }
                    e.1 = Some(v["result"].as_array().cloned().unwrap_or_default());
                    if e.1.is_some() { let (mut h, l, f, _, _) = pend.remove(&key).unwrap(); h.logs = l; h.fees = f.flatten(); let _ = tx.send(h); }
                    continue; }
                let sub = v["params"]["subscription"].as_str().unwrap_or(""); let x = &v["params"]["result"];
                if sub == log_sub { *PUSHED.lock().unwrap().entry(x["blockHash"].as_str().unwrap_or("").to_string()).or_default() += 1; continue; }
                if sub != head_sub || !x.is_object() { continue; }
                let num = hx(&x["number"]); MET.ws_head.fetch_max(num, Relaxed);
                let hh = x["hash"].as_str().unwrap_or("").to_string();
                if do_fees { let calls = DYNCALLS.lock().unwrap().clone(); if !calls.is_empty() { let key = nid | (1u64 << 62); nid += 2; feehash.insert(key + 1, hh.clone()); let _ = ftx.send((key + 1, hh.clone(), calls)); } }
                if !first_seen(&hh) { continue; } WINS[idx].fetch_add(1, Relaxed); MET.ws_heads.fetch_add(1, Relaxed);
                alive.store(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64, Relaxed);
                let h = Head { num, hash: x["hash"].as_str().unwrap_or("").into(), parent: x["parentHash"].as_str().unwrap_or("").into(), at, src: "wss", logs: None, fees: None };
                let key = nid; nid += 2;
                if !relevant(x["logsBloom"].as_str()) { MET.bloom_skip.fetch_add(1, Relaxed); let mut h = h; h.logs = Some(vec![]); let _ = tx.send(h); }
                else { send(&mut s, json!({"jsonrpc":"2.0","id":key,"method":"eth_getLogs","params":[{"blockHash": h.hash, "topics": topics()}]}))?; pend.insert(key, (h, None, None, 0, 0)); }
                pend.retain(|k, _| *k + 200 > key); if feehash.len() > 400 { feehash.clear(); }
            }
        })();
        MET.ws_reconnects.fetch_add(1, Relaxed); log("ws_down", json!({"err": r.err().unwrap_or_default().chars().take(160).collect::<String>()}));
        std::thread::sleep(Duration::from_millis(50 + (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos() % 250) as u64)); // jitter
    }
}
fn poll_thread(rpc: Rpc, tx: mpsc::Sender<Head>, ws_alive: Arc<AtomicU64>) {
    loop { if let Ok(b) = rpc.call("eth_getBlockByNumber", json!(["latest", false])) { let num = hx(&b["number"]); MET.poll_head.store(num, Relaxed);
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64;
            // only inject when WSS is silent (>700ms) or behind: independent gap-filler
            if now.saturating_sub(ws_alive.load(Relaxed)) > 700 || num > MET.ws_head.load(Relaxed) + 2 { MET.poll_triggers.fetch_add(1, Relaxed);
                let _ = tx.send(Head { num, hash: b["hash"].as_str().unwrap_or("").into(), parent: b["parentHash"].as_str().unwrap_or("").into(), at: Instant::now(), src: "poll", logs: None, fees: None }); } }
        std::thread::sleep(Duration::from_millis(400)); }
}
fn rebuild(py: &str) -> Option<Eng> { let t = Instant::now();
    let ok = std::process::Command::new(py).args(["harness/rebuild_all.py"]).env_remove("RH_QN_HTTP").stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    if !ok { log("rebuild_failed", json!({})); return None; } let p = std::fs::read_to_string("data/state_all_latest.txt").ok()?; let e = Eng::from_snapshot(p.trim());
    MET.rebuilds.fetch_add(1, Relaxed); log("rebuild", json!({"block": e.block, "secs": t.elapsed().as_secs_f64(), "pools": e.pools.len()})); Some(e) }
fn applier(cell: Arc<ArcSwap<St>>, mut e: Eng, rpc: Rpc, rx: mpsc::Receiver<Head>, py: String) {
    let verify_every: u64 = std::env::var("ENGINE_VERIFY_EVERY").ok().and_then(|x| x.parse().ok()).unwrap_or(20);
    let resync_s: u64 = std::env::var("ENGINE_RESYNC_S").ok().and_then(|x| x.parse().ok()).unwrap_or(120);
    let mut queue: std::collections::BTreeMap<u64, Head> = Default::default(); let mut pfees: HashMap<String, String> = HashMap::new();
    fn qadd(q: &mut std::collections::BTreeMap<u64, Head>, n: Head) { match q.get(&n.num) { Some(o) if o.logs.is_some() || n.logs.is_none() => {} _ => { q.insert(n.num, n); } } }
    let (vtx, vrx) = mpsc::channel::<(u64, Vec<Fix>)>(); let (vjtx, vjrx) = mpsc::channel::<Eng>();
    { let vr = Rpc { c: rpc.c.clone(), urls: [std::env::var("RH_QN_HTTP2").unwrap_or_default(), std::env::var("RH_QN_HTTP").unwrap_or_default()].into_iter().filter(|u| !u.is_empty()).chain(rpc.urls.clone()).collect() }; std::thread::spawn(move || { while let Ok(mut j) = vjrx.recv() { while let Ok(n) = vjrx.try_recv() { j = n; }
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| verify(&vr, &mut j, true))) { Err(_) => { MET.verify_err.fetch_add(1, Relaxed); log("verify_panic", json!({"block": j.block})); }
            Ok(Some(f)) if !f.is_empty() => { let _ = vtx.send((j.block, f)); } _ => {} } } }); }
    let vr_sync = Rpc { c: rpc.c.clone(), urls: [std::env::var("RH_QN_HTTP2").unwrap_or_default(), std::env::var("RH_QN_HTTP").unwrap_or_default()].into_iter().filter(|u| !u.is_empty()).chain(rpc.urls.clone()).collect() };
    let (rtx, rrx) = mpsc::channel::<Eng>(); let mut rebuilding = false; let mut need_rec = false; let mut last_rb = Instant::now(); let mut nblk = 0u64;
    // hash of snapshot block
    if let Ok(b) = rpc.call("eth_getBlockByNumber", json!([format!("0x{:x}", e.block), false])) { e.hash = b["hash"].as_str().unwrap_or("").into(); }
    // initial snapshot gets the same exact reconcile as rebuild swap-ins (startup divergence 2026-10-25 03:47 SGT)
    if !e.hash.is_empty() { if let Ok(Some(f)) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| verify(&vr_sync, &mut e, false))) { MET.rb_patched.fetch_add(f.len() as u64, Relaxed); log("startup_reconciled", json!({"block": e.block, "patched": f.len()})); patch(&mut e, &f); } }
    loop {
        // in-order queue: WSS heads carry prefetched logs, so we never discard them; poll heads only fill holes
        let mut take = |n: Head, q: &mut std::collections::BTreeMap<u64, Head>, pf: &mut HashMap<String, String>| { if n.src == "fee" { if let Some(f) = n.fees { pf.insert(n.hash, f); } } else { qadd(q, n); } };
        if let Ok(n) = rx.recv_timeout(Duration::from_millis(50)) { take(n, &mut queue, &mut pfees); } while let Ok(n) = rx.try_recv() { take(n, &mut queue, &mut pfees); }
        if let Some(f) = pfees.remove(&e.hash) { if !e.fee_stale.is_empty() { apply_fees(&mut e, &f); e.publish(&cell); } }
        if pfees.len() > 256 { pfees.clear(); }
        while let Ok((b, f)) = vrx.try_recv() { if b == e.block { patch(&mut e, &f); MET.patched.fetch_add(f.len() as u64, Relaxed); e.publish(&cell); } else if b + 64 > e.block { for x in &f { e.dirty.insert(x.0); } } }
        if need_rec && queue.is_empty() { need_rec = false; // any range catch-up -> exact on-chain reconcile at our tip before trusting it
            if let Ok(Some(f)) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| verify(&vr_sync, &mut e, false))) { MET.rb_patched.fetch_add(f.len() as u64, Relaxed); if !f.is_empty() { log("catchup_reconciled", json!({"block": e.block, "patched": f.len()})); } patch(&mut e, &f); e.publish(&cell); } }
        let Some((&k, _)) = queue.iter().next() else { continue }; let h = queue.remove(&k).unwrap();
        // small hole while the missing WSS fetch is still in flight/retrying: wait for it instead of racing the slower public backfill
        if h.num > e.block + 1 && h.num <= e.block + 4 && h.at.elapsed() < Duration::from_millis(600) && e.block > 0 && !e.hash.is_empty() { queue.insert(h.num, h); std::thread::sleep(Duration::from_millis(2)); continue; }
        if queue.len() > 2000 { let keep: Vec<u64> = queue.keys().rev().take(1000).cloned().collect(); queue.retain(|k, _| keep.contains(k)); }
        if let Ok(ne) = rrx.try_recv() { rebuilding = false; last_rb = Instant::now(); let mut ne = ne;
            if ne.hash.is_empty() { if let Ok(b) = rpc.call("eth_getBlockByNumber", json!([format!("0x{:x}", ne.block), false])) { ne.hash = b["hash"].as_str().unwrap_or("").into(); } }
            if ne.block + 64 < e.block { log("rebuild_too_old", json!({"block": ne.block, "tip": e.block})); } else {
            // rebuild snapshots are assembled over minutes from public RPC -> not block-consistent; reconcile against chain at its exact hash before use
            if let Ok(Some(f)) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| verify(&vr_sync, &mut ne, false))) { MET.rb_patched.fetch_add(f.len() as u64, Relaxed); log("rebuild_reconciled", json!({"block": ne.block, "patched": f.len()})); patch(&mut ne, &f); if ne.block >= e.block { e = ne; } else { // keep our tip; take only exact-reconciled state when it is at/after our block
                    log("rebuild_behind", json!({"block": ne.block, "tip": e.block})); let mut ne = ne; while ne.block < e.block { let to = (ne.block + 100).min(e.block); let fr = ne.block + 1; if backfill(&rpc, &mut ne, fr, to).is_err() { break; } } if ne.block == e.block && ne.hash == e.hash { e = ne; } } } else { log("rebuild_reconcile_failed", json!({"block": ne.block})); } } }
        if h.num <= e.block && !(h.num == e.block && h.hash != e.hash) { continue; }
        let r: Result<(), String> = (|| {
            // reorg: parent of h must be our tip if contiguous
            if h.num == e.block + 1 && !e.hash.is_empty() && h.parent != e.hash || (h.num <= e.block) {
                MET.reorgs.fetch_add(1, Relaxed); log("reorg", json!({"at": h.num, "tip": e.block}));
                let mut ok = false;
                while let Some((rb, rh, pools)) = e.ring.pop_back() { if rb >= h.num { continue; }
                    let ch = rpc.call("eth_getBlockByNumber", json!([format!("0x{rb:x}"), false]))?["hash"].as_str().unwrap_or("").to_string();
                    if ch == rh { e.pools = pools; e.block = rb; e.hash = rh.clone(); e.ring.push_back((rb, rh, e.pools.clone())); ok = true; MET.rollbacks.fetch_add(1, Relaxed); break; } }
                if !ok { return Err("deep reorg".into()); } }
            // backfill missing blocks by number->hash batch, then apply in order
            if h.num > e.block + 1 { let from = e.block + 1; let to = (h.num - 1).min(e.block + 100); backfill(&rpc, &mut e, from, to)?; need_rec = true;
                if h.num > e.block + 1 { e.publish(&cell); return Ok(()); } }
            let pre = h.logs.clone().map(|l| (l, h.fees.clone())); let np = pre.as_ref().map(|p| p.0.len());
            apply_block(&rpc, &mut e, h.num, &h.hash, pre)?; if let Some(f) = pfees.remove(&h.hash) { apply_fees(&mut e, &f); } if let (Some(n), true) = (np, std::env::var("WS_LOG_PUSH").map(|x| x == "1").unwrap_or(false)) { CHECKQ.lock().unwrap().push_back((h.hash.clone(), n, Instant::now())); } nblk += 1;
            if nblk % verify_every == 0 { let _ = vjtx.send(Eng { pools: e.pools.clone(), idx: HashMap::new(), block: e.block, hash: e.hash.clone(), dirty: e.dirty.clone(), ring: VecDeque::new(), epoch: 0, enabled_sig: vec![], topo: Arc::new(vec![]), pending_v3: HashMap::new(), fee_stale: HashSet::new() }); }
            e.publish(&cell); if h.src == "wss" { push(&H2Q, h.at.elapsed().as_micros() as u64); }
            Ok(()) })();
        if let Err(x) = r { MET.errors.fetch_add(1, Relaxed); log("apply_error", json!({"err": redact(&x).chars().take(200).collect::<String>()}));
            if x.contains("mismatch") || x.contains("deep reorg") { // is OUR tip still canonical? if not -> roll back via ring; if yes the source is just behind -> retry later
                if let Ok(b) = rpc.call("eth_getBlockByNumber", json!([format!("0x{:x}", e.block), false])) { if b["hash"].as_str().map(|c| c != e.hash).unwrap_or(false) {
                    MET.reorgs.fetch_add(1, Relaxed); let mut ok = false;
                    while let Some((rb, rh, pools)) = e.ring.pop_back() { if rb >= e.block { continue; }
                        if let Ok(c) = rpc.call("eth_getBlockByNumber", json!([format!("0x{rb:x}"), false])) { if c["hash"].as_str() == Some(rh.as_str()) { e.pools = pools; e.block = rb; e.hash = rh.clone(); e.ring.push_back((rb, rh, e.pools.clone())); ok = true; MET.rollbacks.fetch_add(1, Relaxed); break; } } }
                    if !ok { log("deep_reorg_rebuild", json!({})); e.dirty.extend(0..e.pools.len()); } e.publish(&cell); } } } }
        if (!e.dirty.is_empty() && last_rb.elapsed().as_secs() > resync_s) && !rebuilding { rebuilding = true; let (t, py) = (rtx.clone(), py.clone()); let br = Rpc { c: vr_sync.c.clone(), urls: vr_sync.urls.clone() };
            std::thread::spawn(move || { if let Some(mut n) = rebuild(&py) {
                // catch the (minutes-old) snapshot up OFF the hot path in 100-block chunks, then exact-reconcile, then hand over
                if let Ok(b) = br.call("eth_getBlockByNumber", json!([format!("0x{:x}", n.block), false])) { n.hash = b["hash"].as_str().unwrap_or("").into(); }
                for _ in 0..200 { let tip = br.call("eth_blockNumber", json!([])).ok().and_then(|x| x.as_str().and_then(|s| u64::from_str_radix(s.trim_start_matches("0x"), 16).ok())).unwrap_or(0);
                    if n.block + 2 >= tip || n.hash.is_empty() { break; } let to = (n.block + 100).min(tip); let fr = n.block + 1; if backfill(&br, &mut n, fr, to).is_err() { std::thread::sleep(Duration::from_millis(200)); } }
                let _ = t.send(n); } }); }
    }
}

// ---------------- quote cores ----------------
static PATHS: std::sync::LazyLock<Mutex<HashMap<(u64, String, String, usize), Arc<Vec<Path>>>>> = std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));
fn cached_paths(st: &St, a: &str, b: &str, h: usize, cap: usize) -> Arc<Vec<Path>> {
    let key = (st.epoch, a.to_string(), b.to_string(), h);
    if let Some(p) = PATHS.lock().unwrap().get(&key) { return p.clone(); }
    let mut ps = paths(&st.topo, a, b, h);
    if ps.len() > cap { // prune: fewer hops first, then by bottleneck active liquidity (cheap, deterministic)
        let score = |p: &Path| p.iter().map(|x| st.pools[x.pool].liq).min().unwrap_or(0); ps.sort_by(|x, y| x.len().cmp(&y.len()).then(score(y).cmp(&score(x)))); ps.truncate(cap); }
    let p = Arc::new(ps); let mut g = PATHS.lock().unwrap(); if g.len() > 4096 { g.retain(|k, _| k.0 == st.epoch); } g.insert(key, p.clone()); p
}
fn quote(q: &HashMap<String, String>, st: &St, fast: bool) -> Result<Value, String> {
    let tin = q.get("tin").ok_or("tin")?.to_lowercase(); let tout = q.get("tout").ok_or("tout")?.to_lowercase(); if tin == tout { return Err("same token".into()); }
    let amt = U256::from_str_radix(q.get("amt").ok_or("amt")?, 10).map_err(|_| "amt")?; if amt.is_zero() { return Err("zero amount".into()); }
    let gp = U256::from(q.get("gp").and_then(|x| x.parse::<u64>().ok()).unwrap_or(20_000_000));
    let (chunks, hops, topk, cap) = if fast { (4u32, 2usize, 4usize, 32usize) } else { (32, 3, 12, 384) };
    let t = Instant::now(); let ps = cached_paths(st, &tin, &tout, hops, cap); if ps.is_empty() { return Err("no path".into()); }
    let eth_to_out = |wei: U256| -> U256 { if tout == WETH { return wei; } let pw = cached_paths(st, WETH, &tout, 2, 32); let pr = U256::from(10u64.pow(16));
        pw.iter().filter_map(|p| quote_path(&st.pools, p, pr)).max().unwrap_or(U256::ZERO) * wei / pr };
    let gh = eth_to_out(U256::from(120_000u64) * gp); let gb = eth_to_out(U256::from(60_000u64) * gp);
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| route_kb(&st.pools, &ps, amt, chunks, gh, gb, topk, if fast { 0 } else { 12 }, Some(t + Duration::from_millis(if fast { 1 } else { 20 }))))).map_err(|_| "route panic")?;
    if r.legs.is_empty() { return Err("no route".into()); }
    let us = t.elapsed().as_micros() as u64; push(if fast { &FLAT } else { &BLAT }, us);
    let nh: usize = r.legs.iter().map(|l| l.0.len()).sum(); let gross = r.out + gh * U256::from(nh) + gb;
    let legs: Vec<_> = r.legs.iter().map(|(path, la)| json!({"amountIn": la.to_string(), "hops": path.iter().map(|h| { let p = &st.pools[h.pool];
        json!({"pool": p.addr, "venue": p.venue, "fee": p.fee, "zfo": h.zfo, "tokenIn": if h.zfo {&p.token0} else {&p.token1}, "tokenOut": if h.zfo {&p.token1} else {&p.token0}}) }).collect::<Vec<_>>()})).collect();
    let slip: u64 = q.get("slippageBps").and_then(|x| x.parse().ok()).unwrap_or(50).min(5000);
    // router: v2 (default once ROUTER_V2 is set) charges FEE_BPS of the gross swap output; v1 kept for old integrations
    let v2addr = router_v2(); let use_v2 = v2addr.is_some() && q.get("router").map(|x| x != "v1").unwrap_or(true);
    let swap_out = r.out + gh * U256::from(nh) + gb; // actual token output of the route (== on-chain swap output, pre-fee)
    let fee = if use_v2 { swap_out * U256::from(FEE_BPS) / U256::from(10_000u64) } else { U256::ZERO };
    let receive = swap_out - fee; let net_after = r.out.saturating_sub(fee);
    let min_out = receive * U256::from(10_000 - slip) / U256::from(10_000u64); // minOut is NET of the protocol fee
    let dl = q.get("deadline").and_then(|x| x.parse::<u32>().ok()).unwrap_or((std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() + 60) as u32);
    let program = q.get("recipient").filter(|r| r.len() == 42).map(|rc| { let (p, ok, why) = encode_program(st, &r.legs, &tin, &tout, &rc.to_lowercase(), min_out, dl, use_v2); let data = swap_calldata(&p); json!({"to": if use_v2 { v2addr.clone().unwrap() } else { ROUTER.to_string() }, "router": if use_v2 {"v2"} else {"v1"}, "data": data, "value": "0", "program": p, "executable": ok, "reason": why, "minOut": min_out.to_string(), "deadline": dl}) });
    Ok(json!({"tx": program, "slippageBps": slip, "minOut": min_out.to_string(), "block": st.block, "blockHash": st.hash, "tokenIn": tin, "tokenOut": tout, "amountIn": amt.to_string(), "grossOut": gross.to_string(), "protocolFee": fee.to_string(), "feeBps": if use_v2 { FEE_BPS } else { 0 }, "receiveOut": receive.to_string(), "netOut": net_after.to_string(), "netOutGasOnly": r.out.to_string(), "router": if use_v2 {"v2"} else {"v1"},
        "gasUnits": 60_000 + 120_000 * nh, "legs": legs, "mode": if fast {"fast"} else {"best"}, "compute_us": us, "paths": ps.len()}))
}
/// TickingRouter v1 program (contracts/ARCHITECTURE.md): header tokenIn|tokenOut|recipient|minOut u128|deadline u32, then ops.
/// Returns (hex program, executable, reason). V4 hops touching WETH are flagged non-executable (engine normalizes native ETH -> WETH, so
/// the PoolKey currency cannot be reconstructed safely here).
fn encode_program(st: &St, legs: &[(Path, U256)], tin: &str, tout: &str, recipient: &str, min_out: U256, deadline: u32, v2: bool) -> (String, bool, String) {
    let a20 = |a: &str| a.trim_start_matches("0x").to_string();
    let mut o = format!("{}{}{}{:032x}{:08x}", a20(tin), a20(tout), a20(recipient), min_out, deadline); let mut ok = true; let mut why = String::new();
    for (path, amt) in legs { for (k, h) in path.iter().enumerate() { let p = &st.pools[h.pool]; let last = k + 1 == path.len();
        let hout = if h.zfo { &p.token1 } else { &p.token0 };
        let (venue, payload) = match p.venue.as_str() {
            "univ3" => (1u8, format!("00{:06x}{}", p.fee, a20(&p.addr))), "sushi" => (1, format!("01{:06x}{}", p.fee, a20(&p.addr))), "giga" => (1, format!("02{:06x}{}", p.fee, a20(&p.addr))),
            "ramses" => (2, format!("00{:06x}{}", (p.spacing as u32) & 0xffffff, a20(&p.addr))), "up" => (2, format!("01{:06x}{}", (p.spacing as u32) & 0xffffff, a20(&p.addr))),
            "v4" => { let native = NATIVE_V4.read().unwrap().contains(&p.addr);
                if native && !v2 { ok = false; why = "v4 native-ETH hop requires router v2".into(); }
                (if native { 4 | 0x20 } else { 4 }, format!("{:06x}{:06x}{}", p.fee & 0xffffff, (p.spacing as u32) & 0xffffff, "0".repeat(40))) }
            v => { ok = false; why = format!("venue {v} not executable"); (0, String::new()) } };
        let op = venue | if k > 0 { 0x80 } else { 0 } | if last { 0x40 } else { 0 };
        o += &format!("{op:02x}"); if k == 0 { o += &format!("{:032x}", amt); } o += &a20(hout); o += &payload; } }
    (format!("0x{o}"), ok, why)
}
fn pool_quote(q: &HashMap<String, String>, st: &St) -> Result<Value, String> {
    let a = q.get("pool").ok_or("pool")?.to_lowercase(); let zfo = q.get("zfo").map(|x| x == "1" || x == "true").unwrap_or(true);
    let amt = U256::from_str_radix(q.get("amt").ok_or("amt")?, 10).map_err(|_| "amt")?;
    let p = st.pools.iter().find(|p| p.addr == a).ok_or("unknown pool")?; if p.liq == 0 { return Err("pool disabled or empty".into()); }
    // same gas-honest caps as routing: a swap needing >MAX_CROSS tick crossings can't execute within block/eth_call gas
    let mut c = p.clone(); let r = c.swap_try(zfo, amt, None, core::router::MAX_STEPS_PER_SWAP).map_err(|e| e.to_string())?;
    if c.crossed > core::router::MAX_CROSS_PER_TX { return Err(format!("exceeds gas cap: {} tick crossings", c.crossed)); }
    Ok(json!({"block": st.block, "pool": a, "venue": p.venue, "token0": p.token0, "token1": p.token1, "zfo": zfo, "amountIn": amt.to_string(), "used": r.0.to_string(), "out": r.1.to_string()}))
}
fn handle(mut c: TcpStream, cell: Arc<ArcSwap<St>>, fast_port: bool) {
    let _ = c.set_nodelay(true); let mut rd = BufReader::new(c.try_clone().unwrap()); let stale_max: u64 = std::env::var("ENGINE_STALE_BLOCKS").ok().and_then(|x| x.parse().ok()).unwrap_or(3);
    loop { let mut line = String::new(); if rd.read_line(&mut line).unwrap_or(0) == 0 { return; }
        loop { let mut h = String::new(); if rd.read_line(&mut h).unwrap_or(0) == 0 { return; } if h == "\r\n" || h == "\n" { break; } }
        let path = line.split_whitespace().nth(1).unwrap_or("/").to_string(); let (route, qs) = path.split_once('?').unwrap_or((&path, ""));
        let q: HashMap<String, String> = qs.split('&').filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.into(), v.into())).collect();
        let st = cell.load_full(); let head = MET.ws_head.load(Relaxed).max(MET.poll_head.load(Relaxed)); let lag = head.saturating_sub(st.block);
        let (code, body, ct) = match route {
            "/quote" | "/pool_quote" if lag > stale_max => { MET.stale.fetch_add(1, Relaxed); (503, json!({"error": "stale state", "block": st.block, "head": head}).to_string(), "application/json") }
            "/quote" => { let fast = fast_port || q.get("mode").map(|m| m == "fast").unwrap_or(false); if fast { MET.fq.fetch_add(1, Relaxed) } else { MET.bq.fetch_add(1, Relaxed) };
                match quote(&q, &st, fast) { Ok(mut v) => { v["headBlock"] = json!(head); v["lagBlocks"] = json!(lag); v["stateAgeUs"] = json!(st.published.elapsed().as_micros() as u64); (200, v.to_string(), "application/json") }
                    Err(x) => { MET.qerr.fetch_add(1, Relaxed); (400, json!({"error": x}).to_string(), "application/json") } } }
            "/pool_quote" => match pool_quote(&q, &st) { Ok(mut v) => { v["headBlock"] = json!(head); (200, v.to_string(), "application/json") } Err(x) => (400, json!({"error": x}).to_string(), "application/json") },
            "/health" => (if lag <= stale_max { 200 } else { 503 }, json!({"ok": lag <= stale_max, "block": st.block, "head": head, "lagBlocks": lag, "pools": st.pools.len(), "dirty": MET.dirty.load(Relaxed)}).to_string(), "application/json"),
            "/freshness" => (200, Value::Object(st.pools.iter().filter(|p| p.liq > 0).map(|p| (p.addr.clone(), json!({"venue": p.venue, "block": st.block}))).collect()).to_string(), "application/json"),
            "/divergences" => (200, json!(*DIVLOG.lock().unwrap()).to_string(), "application/json"),
            "/metrics" => { let m = &*MET; let mut s = String::new();
                for (k, v) in [("ws_head", m.ws_head.load(Relaxed)), ("poll_head", m.poll_head.load(Relaxed)), ("state_block", st.block), ("lag_blocks", lag), ("logs_total", m.logs.load(Relaxed)),
                    ("swaps_total", m.swaps.load(Relaxed)), ("liquidity_events_total", m.liq.load(Relaxed)), ("pools_created_total", m.created.load(Relaxed)), ("pools", st.pools.len() as u64),
                    ("reorgs_total", m.reorgs.load(Relaxed)), ("rollbacks_total", m.rollbacks.load(Relaxed)), ("backfill_blocks_total", m.backfill_blocks.load(Relaxed)), ("ws_reconnects_total", m.ws_reconnects.load(Relaxed)),
                    ("ws_heads_total", m.ws_heads.load(Relaxed)), ("poll_triggers_total", m.poll_triggers.load(Relaxed)), ("verified_total", m.verified.load(Relaxed)), ("diverged_total", m.diverged.load(Relaxed)),
                    ("errors_total", m.errors.load(Relaxed)), ("fast_quotes_total", m.fq.load(Relaxed)), ("best_quotes_total", m.bq.load(Relaxed)), ("quote_errors_total", m.qerr.load(Relaxed)),
                    ("stale_rejects_total", m.stale.load(Relaxed)), ("dirty_pools", m.dirty.load(Relaxed)), ("rebuilds_total", m.rebuilds.load(Relaxed)), ("ws_rpc_retries_total", m.ws_retries.load(Relaxed)), ("ws_head_wins_ep1_total", WINS[0].load(Relaxed)), ("ws_head_wins_ep2_total", WINS[1].load(Relaxed)), ("verify_runs_total", m.verify_runs.load(Relaxed)), ("verify_errors_total", m.verify_err.load(Relaxed)), ("verify_last_ms", m.verify_ms.load(Relaxed)), ("bloom_skipped_blocks_total", m.bloom_skip.load(Relaxed)), ("fee_updates_total", m.fee_applied.load(Relaxed)), ("push_checked_total", m.pushchk.load(Relaxed)), ("push_mismatch_total", m.pushmis.load(Relaxed)), ("verify_patched_total", m.patched.load(Relaxed)), ("rebuild_reconciled_pools_total", m.rb_patched.load(Relaxed))] { s += &format!("ticking_{k} {v}\n"); }
                for q in [0.5, 0.99, 0.999] { s += &format!("ticking_head_to_quotable_us{{q=\"{q}\"}} {}\nticking_fast_compute_us{{q=\"{q}\"}} {}\nticking_best_compute_us{{q=\"{q}\"}} {}\n", pct(&H2Q, q), pct(&FLAT, q), pct(&BLAT, q)); }
                for (i, a) in LAGH.iter().enumerate() { s += &format!("ticking_lag_samples{{lag=\"{i}\"}} {}\n", a.load(Relaxed)); }
                (200, s, "text/plain") }
            _ => (404, "{}".into(), "application/json") };
        if c.write_all(format!("HTTP/1.1 {code} X\r\nContent-Type: {ct}\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes()).is_err() { return; } }
}
fn serve(bind: String, cell: Arc<ArcSwap<St>>, fast: bool, cores: Vec<core_affinity::CoreId>, workers: usize) {
    let l = TcpListener::bind(&bind).expect("bind"); log("listening", json!({"bind": bind, "fast": fast, "workers": workers}));
    let (tx, rx) = mpsc::channel::<TcpStream>(); let rx = Arc::new(Mutex::new(rx));
    for w in 0..workers { let (rx, cell, cores) = (rx.clone(), cell.clone(), cores.clone()); std::thread::spawn(move || {
        if !cores.is_empty() { core_affinity::set_for_current(cores[w % cores.len()]); }
        if fast { let cap: u32 = std::env::var("FAST_STEP_CAP").ok().and_then(|x| x.parse().ok()).unwrap_or(120); core::router::STEP_CAP.with(|c| c.set(cap)); }
        loop { let c = { rx.lock().unwrap().recv() }; let Ok(c) = c else { return }; handle(c, cell.clone(), fast); } }); }
    for c in l.incoming().flatten() { let _ = tx.send(c); }
}
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let env = |k: &str, d: &str| std::env::var(k).unwrap_or(d.to_string());
    let qn = env("RH_QN_HTTP", ""); let public = env("RH_PUBLIC_HTTP", "https://rpc.mainnet.chain.robinhood.com"); let wss = env("RH_WSS", "");
    let py = env("PYTHON", ".venv/bin/python");
    let mk = |urls: Vec<String>| Rpc { c: reqwest::blocking::Client::builder().timeout(Duration::from_secs(15)).tcp_nodelay(true).pool_max_idle_per_host(8).build().unwrap(), urls: urls.into_iter().filter(|u| !u.is_empty()).collect() };
    let mut e = rebuild(&py).expect("cold start snapshot");
    let cell = Arc::new(ArcSwap::from_pointee(St { block: 0, hash: String::new(), pools: Arc::new(vec![]), topo: Arc::new(vec![]), epoch: 0, published: Instant::now() }));
    e.publish(&cell);
    let (tx, rx) = mpsc::channel::<Head>(); let alive = Arc::new(AtomicU64::new(0));
    let wss2 = env("RH_WSS2", "");
    if !wss.is_empty() { let (tx, a, f) = (tx.clone(), alive.clone(), wss2.is_empty()); std::thread::spawn(move || ws_thread(wss, tx, a, 0, f)); }
    if !wss2.is_empty() { let (tx, a) = (tx.clone(), alive.clone()); std::thread::spawn(move || ws_thread(wss2, tx, a, 1, true)); }
    { let (tx, a, r) = (tx.clone(), alive.clone(), mk(vec![public.clone(), qn.clone()])); std::thread::spawn(move || poll_thread(r, tx, a)); }
    { let (c, r, py) = (cell.clone(), mk(vec![public.clone(), qn.clone()]), py.clone()); std::thread::spawn(move || applier(c, e, r, rx, py)); }
    // core split: fast gets dedicated cores [1..=fast_n], best gets the rest (core 0 left to applier/OS)
    let all = core_affinity::get_core_ids().unwrap_or_default(); let n = all.len(); let fast_n: usize = env("FAST_CORES", "2").parse().unwrap_or(2);
    let fast_cores: Vec<_> = all.iter().skip(1).take(fast_n).cloned().collect(); let best_cores: Vec<_> = all.iter().skip(1 + fast_n).cloned().collect();
    let best_workers: usize = env("BEST_WORKERS", &((n.saturating_sub(1 + fast_n)).max(1)).to_string()).parse().unwrap_or(2);
    { let (c, b, fc) = (cell.clone(), env("ENGINE_FAST_BIND", "127.0.0.1:8783"), fast_cores.clone()); std::thread::spawn(move || serve(b, c, true, fc, fast_n.max(1) * 2)); }
    let _ = AtomicBool::new(false);
    // lag sampler (every 5ms: newest head seen by ANY source minus published state block) + push-count cross-check
    { let c = cell.clone(); std::thread::spawn(move || loop { std::thread::sleep(Duration::from_millis(5));
        let h = MET.ws_head.load(Relaxed).max(MET.poll_head.load(Relaxed)); let b = c.load().block; if b == 0 { continue; } let l = h.saturating_sub(b).min(9) as usize; LAGH[l].fetch_add(1, Relaxed);
        let mut q = CHECKQ.lock().unwrap(); while q.front().map(|x| x.2.elapsed() > Duration::from_secs(3)).unwrap_or(false) { let (hash, n, _) = q.pop_front().unwrap();
            let p = PUSHED.lock().unwrap().remove(&hash).unwrap_or(0); MET.pushchk.fetch_add(1, Relaxed); if p != n { MET.pushmis.fetch_add(1, Relaxed); log("push_count_mismatch", json!({"hash": hash, "getLogs": n, "pushed": p})); } }
        let mut pm = PUSHED.lock().unwrap(); if pm.len() > 5000 { pm.clear(); } }); }
    serve(env("ENGINE_BIND", "127.0.0.1:8782"), cell, false, best_cores, best_workers);
}
