//! Ticking quote core server (Rust). In-memory exact state, hot-reloaded when the state engine publishes a new snapshot.
//! HTTP/1.1 (minimal, std only): GET /quote?tin=0x..&tout=0x..&amt=<wei>&mode=fast|best&gp=<wei>  GET /health  GET /metrics
use core::*; use core::router::*; use ruint::aliases::U256;
use std::{collections::HashMap, io::{Read, Write}, net::TcpListener, sync::{Arc, RwLock, atomic::{AtomicU64, Ordering::Relaxed}}, time::{Instant, SystemTime}};
struct St { block: u64, pools: Vec<Pool>, toks: HashMap<String, JTok>, loaded: SystemTime, paths: RwLock<HashMap<(String, String, usize), Arc<Vec<Path>>>> }
static NQ: AtomicU64 = AtomicU64::new(0); static NERR: AtomicU64 = AtomicU64::new(0); static NRELOAD: AtomicU64 = AtomicU64::new(0);
static LAT_FAST: RwLock<Vec<u64>> = RwLock::new(vec![]); static LAT_BEST: RwLock<Vec<u64>> = RwLock::new(vec![]);
const WETH: &str = "0x0bd7d308f8e1639fab988df18a8011f41eacad73";
fn load_st(ptr: &str) -> Option<St> {
    let p = std::fs::read_to_string(ptr).ok()?; let p = p.trim();
    let r = std::panic::catch_unwind(|| load(p)).ok()?;
    Some(St { block: r.0, pools: r.1, toks: r.2, loaded: SystemTime::now(), paths: RwLock::new(HashMap::new()) })
}
fn q(params: &HashMap<String, String>, st: &St) -> Result<serde_json::Value, String> {
    let tin = params.get("tin").ok_or("tin")?.to_lowercase(); let tout = params.get("tout").ok_or("tout")?.to_lowercase();
    if tin == tout { return Err("same token".into()); }
    let amt = U256::from_str_radix(params.get("amt").ok_or("amt")?, 10).map_err(|_| "amt")?;
    if amt.is_zero() { return Err("zero amount".into()); }
    let fast = params.get("mode").map(|m| m == "fast").unwrap_or(false);
    let gp = U256::from(params.get("gp").and_then(|x| x.parse::<u64>().ok()).unwrap_or(20_000_000));
    let (chunks, hops, topk) = if fast { (4u32, 2usize, 4usize) } else { (32, 3, 12) };
    let t = Instant::now();
    let key = (tin.clone(), tout.clone(), hops);
    let ps = { let c = st.paths.read().unwrap().get(&key).cloned(); match c { Some(p) => p, None => { let p = Arc::new(paths(&st.pools, &tin, &tout, hops)); st.paths.write().unwrap().insert(key, p.clone()); p } } };
    if ps.is_empty() { return Err("no path".into()); }
    let eth_to_out = |wei: U256| -> U256 { if tout == WETH { return wei; }
        let key = (WETH.to_string(), tout.clone(), 2usize);
        let pw = { let c = st.paths.read().unwrap().get(&key).cloned(); match c { Some(p) => p, None => { let p = Arc::new(paths(&st.pools, WETH, &tout, 2)); st.paths.write().unwrap().insert(key, p.clone()); p } } };
        let probe = U256::from(10u64.pow(16));
        pw.iter().filter_map(|p| quote_path(&st.pools, p, probe)).max().unwrap_or(U256::ZERO) * wei / probe };
    let gh = eth_to_out(U256::from(120_000u64) * gp); let gb = eth_to_out(U256::from(60_000u64) * gp);
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| route_k(&st.pools, &ps, amt, chunks, gh, gb, topk))).map_err(|_| "route panic")?;
    let us = t.elapsed().as_micros() as u64;
    let nh: usize = r.legs.iter().map(|l| l.0.len()).sum();
    let gross = r.out + gh * U256::from(nh) + gb;
    let legs: Vec<_> = r.legs.iter().map(|(path, la)| serde_json::json!({"amountIn": la.to_string(),
        "hops": path.iter().map(|h| { let p = &st.pools[h.pool]; serde_json::json!({"pool": p.addr, "fee": p.fee, "zfo": h.zfo,
            "tokenIn": if h.zfo {&p.token0} else {&p.token1}, "tokenOut": if h.zfo {&p.token1} else {&p.token0}}) }).collect::<Vec<_>>()})).collect();
    (if fast { &LAT_FAST } else { &LAT_BEST }).write().map(|mut v| { v.push(us); if v.len() > 20000 { v.drain(0..10000); } }).ok();
    Ok(serde_json::json!({"block": st.block, "tokenIn": tin, "tokenOut": tout, "amountIn": amt.to_string(), "grossOut": gross.to_string(), "netOut": r.out.to_string(),
        "gasUnits": 60_000 + 120_000 * nh, "gasOut": (gross - r.out).to_string(), "legs": legs, "mode": if fast {"fast"} else {"best"}, "compute_us": us, "paths": ps.len()}))
}
fn pct(v: &[u64], p: f64) -> u64 { if v.is_empty() { return 0; } let mut s = v.to_vec(); s.sort(); s[((s.len() - 1) as f64 * p) as usize] }
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let ptr = a.get(1).cloned().unwrap_or("data/state_all_latest.txt".into()); let bind = a.get(2).cloned().unwrap_or("127.0.0.1:8781".into());
    let st: Arc<RwLock<Arc<St>>> = Arc::new(RwLock::new(Arc::new(load_st(&ptr).expect("initial state"))));
    { let st = st.clone(); let ptr = ptr.clone(); std::thread::spawn(move || { let mut last = String::new(); loop {
        if let Ok(p) = std::fs::read_to_string(&ptr) { if p != last { if let Some(n) = load_st(&ptr) { *st.write().unwrap() = Arc::new(n); NRELOAD.fetch_add(1, Relaxed); last = p; } } }
        std::thread::sleep(std::time::Duration::from_millis(100)); } }); }
    let l = TcpListener::bind(&bind).expect("bind"); eprintln!("{{\"msg\":\"qserver listening\",\"bind\":\"{bind}\"}}");
    for c in l.incoming() { let Ok(mut c) = c else { continue }; let st = st.clone(); std::thread::spawn(move || {
        let _ = c.set_nodelay(true); let mut buf = [0u8; 8192];
        loop { let n = match c.read(&mut buf) { Ok(0) | Err(_) => break, Ok(n) => n };
            let req = String::from_utf8_lossy(&buf[..n]); let line = req.lines().next().unwrap_or("");
            let path = line.split_whitespace().nth(1).unwrap_or("/");
            let (route, qs) = path.split_once('?').unwrap_or((path, ""));
            let params: HashMap<String, String> = qs.split('&').filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect();
            let s = st.read().unwrap().clone();
            let (code, body) = match route {
                "/quote" => { NQ.fetch_add(1, Relaxed); match q(&params, &s) { Ok(v) => (200, v.to_string()), Err(e) => { NERR.fetch_add(1, Relaxed); (400, serde_json::json!({"error": e}).to_string()) } } }
                "/health" => (200, serde_json::json!({"ok": true, "block": s.block, "pools": s.pools.len(), "state_age_ms": s.loaded.elapsed().map(|d| d.as_millis() as u64).unwrap_or(0)}).to_string()),
                "/metrics" => { let f = LAT_FAST.read().unwrap(); let b = LAT_BEST.read().unwrap();
                    (200, format!("qcore_quotes_total {}\nqcore_errors_total {}\nqcore_reloads_total {}\nqcore_state_block {}\nqcore_pools {}\nqcore_compute_us{{mode=\"fast\",q=\"0.5\"}} {}\nqcore_compute_us{{mode=\"fast\",q=\"0.99\"}} {}\nqcore_compute_us{{mode=\"best\",q=\"0.5\"}} {}\nqcore_compute_us{{mode=\"best\",q=\"0.99\"}} {}\n",
                        NQ.load(Relaxed), NERR.load(Relaxed), NRELOAD.load(Relaxed), s.block, s.pools.len(), pct(&f, 0.5), pct(&f, 0.99), pct(&b, 0.5), pct(&b, 0.99))) }
                _ => (404, "{\"error\":\"not found\"}".into()) };
            let ct = if route == "/metrics" { "text/plain" } else { "application/json" };
            if c.write_all(format!("HTTP/1.1 {code} OK\r\nContent-Type: {ct}\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{body}", body.len()).as_bytes()).is_err() { break; }
        } }); }
}
