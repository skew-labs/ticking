// usage: quote <state.json> <tokenIn sym|addr> <tokenOut sym|addr> <amountIn human,...> [chunks=64] [gasPriceWei=60000000] [gasPerHop=120000] [gasBase=60000] [reps=200]
use core::*; use core::router::*; use ruint::aliases::U256; use std::time::Instant;
fn main() { std::panic::set_hook(Box::new(|_| {}));
    let a: Vec<String> = std::env::args().collect();
    let (blk, pools, toks) = load(&a[1]);
    let find = |s: &str| -> String { let s = s.to_lowercase(); if s.starts_with("0x") { return s; }
        // pick the token with this symbol that has the most pools (avoid same-symbol impostors)
        let mut c: Vec<(&String, usize)> = toks.iter().filter(|(_, t)| t.symbol.to_lowercase() == s).map(|(k, _)| (k, pools.iter().filter(|p| &p.token0 == k || &p.token1 == k).count())).collect();
        c.sort_by_key(|x| std::cmp::Reverse(x.1)); c.first().expect("token").0.clone() };
    let (tin, tout) = (find(&a[2]), find(&a[3]));
    let chunks: u32 = a.get(5).map(|x| x.parse().unwrap()).unwrap_or(64);
    let gp = U256::from(a.get(6).map(|x| x.parse::<u64>().unwrap()).unwrap_or(60_000_000));
    let gh = U256::from(a.get(7).map(|x| x.parse::<u64>().unwrap()).unwrap_or(120_000));
    let gb = U256::from(a.get(8).map(|x| x.parse::<u64>().unwrap()).unwrap_or(60_000));
    let reps: usize = a.get(9).map(|x| x.parse().unwrap()).unwrap_or(200);
    let fast_mode = a.get(10).map(|x| x=="fast").unwrap_or(false); let nobr = fast_mode || a.get(10).map(|x| x=="nobr").unwrap_or(false);
    let (chunks, max_hops, topk) = if fast_mode { (chunks.min(4), 2usize, 4usize) } else { (chunks, 3usize, 12usize) };
    let weth = find("WETH");
    let t0 = Instant::now();
    let ps = paths(&pools, &tin, &tout, max_hops);
    let t_paths = t0.elapsed();
    // gas -> tokenOut conversion via best small WETH->tokenOut path (0.01 ETH), exact math
    let eth_to_out = |wei: U256| -> U256 { if tout == weth { return wei; }
        let pw = paths(&pools, &weth, &tout, 2); let probe = U256::from(10u64.pow(16));
        let best = pw.iter().filter_map(|p| quote_path(&pools, p, probe)).max().unwrap_or(U256::ZERO);
        best * wei / probe };
    let g_hop = eth_to_out(gh * gp); let g_base = eth_to_out(gb * gp);
    let din = toks[&tin].decimals; let dout = toks[&tout].decimals;
    let mut res = vec![];
    for amt_s in a[4].split(',') {
        let amt_f: f64 = amt_s.parse().unwrap(); // input parsing only; all money math below is U256
        let amt = U256::from((amt_f * 1e6) as u128) * U256::from(10u128.pow(din)) / U256::from(1_000_000u64);
        let mut lat = vec![];
        let mut r = route_k(&pools, &ps, amt, chunks, g_hop, g_base, topk);
        for _ in 0..reps { let t = Instant::now(); r = route_k(&pools, &ps, amt, chunks, g_hop, g_base, topk); lat.push(t.elapsed().as_micros() as u64); }
        lat.sort(); let p = |q: f64| lat[((lat.len() as f64 - 1.0) * q) as usize];
        // ---- backrun: apply our legs on post-state, then greedy cyclic arb tout->..->tout (2-3 hops) ----
        let tb = Instant::now();
        let mut post: Vec<Pool> = pools.clone();
        for (path, la) in r.legs.iter() { let _ = sim_path(&mut post, path, *la); }
        let mut cyc: Vec<Path> = vec![];
        for (i, pl) in post.iter().enumerate() { if pl.liq == 0 || nobr { continue; }
            let (z, mid) = if pl.token0 == tout { (true, &pl.token1) } else if pl.token1 == tout { (false, &pl.token0) } else { continue };
            for q in paths(&post, mid, &tout, 2) { if q.iter().any(|h| h.pool == i) { continue; }
                let mut c = vec![Hop { pool: i, zfo: z }]; c.extend(q); cyc.push(c); } }
        let mut br_total = U256::ZERO; let mut br_legs = vec![];
        for _round in 0..(if nobr {0} else {4}) {
            let mut best: (U256, usize, U256) = (U256::ZERO, 0, U256::ZERO);
            for (ci, c) in cyc.iter().enumerate() {
                let gas = g_hop * U256::from(c.len());
                let prof = |x: U256| -> U256 { quote_path(&post, c, x).map(|o| o.saturating_sub(x)).unwrap_or(U256::ZERO) };
                // doubling then ternary on concave profit
                let mut x = U256::ZERO; { let mut t = (amt / U256::from(1_000_000u32)).max(U256::from(1000u32)); for _ in 0..8 { if !prof(t).is_zero() { x = t; break; } t *= U256::from(10u8); } } if x.is_zero() { continue; }
                while prof(x * U256::from(2u8)) > prof(x) && x < amt * U256::from(100u8) { x *= U256::from(2u8); }
                let (mut lo, mut hi) = (x / U256::from(2u8), x * U256::from(2u8));
                for _ in 0..60 { if hi - lo < U256::from(3u8) { break; } let m1 = lo + (hi - lo) / U256::from(3u8); let m2 = hi - (hi - lo) / U256::from(3u8); if prof(m1) < prof(m2) { lo = m1 } else { hi = m2 } }
                let pr = prof(lo).saturating_sub(gas);
                if pr > best.0 { best = (pr, ci, lo); }
            }
            if best.0.is_zero() { break; }
            let c = cyc[best.1].clone(); let x = best.2;
            if let Some(o) = sim_path(&mut post, &c, x) { br_total += o.saturating_sub(x).saturating_sub(g_hop * U256::from(c.len()));
                br_legs.push(serde_json::json!({"amountIn": x.to_string(), "out": o.to_string(), "hops": c.iter().map(|h| serde_json::json!({"pool": post[h.pool].addr, "zfo": h.zfo})).collect::<Vec<_>>()})); } else { break; }
        }
        let br_us = tb.elapsed().as_micros() as u64;
        let legs: Vec<_> = r.legs.iter().map(|(path, la)| serde_json::json!({"amountIn": la.to_string(),
            "hops": path.iter().map(|h| serde_json::json!({"pool": pools[h.pool].addr, "fee": pools[h.pool].fee, "zfo": h.zfo,
                "tokenIn": if h.zfo {&pools[h.pool].token0} else {&pools[h.pool].token1}, "tokenOut": if h.zfo {&pools[h.pool].token1} else {&pools[h.pool].token0}})).collect::<Vec<_>>()})).collect();
        res.push(serde_json::json!({"amountIn": amt.to_string(), "amountInHuman": amt_s, "netOut": r.out.to_string(), "bestSingleNetOut": r.gross_single_best.to_string(),
            "netOutHuman": format!("{}", (r.out.to_string().parse::<f64>().unwrap()) / 10f64.powi(dout as i32)),
            "splitGainBps": if r.gross_single_best.is_zero() { 0.0 } else { (r.out.to_string().parse::<f64>().unwrap() / r.gross_single_best.to_string().parse::<f64>().unwrap() - 1.0) * 1e4 },
            "legs": legs, "backrunNetOut": br_total.to_string(), "backrunLegs": br_legs, "backrun_us": br_us, "latency_us": {"p50": p(0.5), "p99": p(0.99), "max": lat[lat.len()-1], "reps": reps}}));
    }
    println!("{}", serde_json::to_string_pretty(&serde_json::json!({"block": blk, "tokenIn": tin, "tokenOut": tout, "paths": ps.len(), "path_enum_us": t_paths.as_micros() as u64,
        "gasPerHopOut": g_hop.to_string(), "gasBaseOut": g_base.to_string(), "chunks": chunks, "results": res})).unwrap());
}
