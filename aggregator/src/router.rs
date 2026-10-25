//! Router: path graph (<=3 hops), greedy marginal-output allocation over paths on a SHARED mutable state
//! (shared pools are automatically respected), local-search refinement, then exact sequential re-simulation
//! in execution order = what a bundled multicall would do at that block.
use crate::Pool; use ruint::aliases::U256; use std::collections::HashMap;

#[derive(Clone, Debug)] pub struct Hop { pub pool: usize, pub zfo: bool }
pub type Path = Vec<Hop>;
pub const MAX_STEPS_PER_SWAP: u32 = 600; // each loop step = SLOAD-heavy gas on-chain; >600 steps is a pool-draining junk route
pub const MAX_CROSS_PER_TX: u32 = 400; // gas-cap guard (observed: 1219 crossings ~ 49M gas -> eth_call/RPC cap revert)

pub fn paths(pools: &[Pool], tin: &str, tout: &str, max_hops: usize) -> Vec<Path> {
    let mut adj: HashMap<&str, Vec<(usize, bool, &str)>> = HashMap::new();
    for (i, p) in pools.iter().enumerate() {
        if p.liq == 0 { continue; }
        adj.entry(&p.token0).or_default().push((i, true, &p.token1));
        adj.entry(&p.token1).or_default().push((i, false, &p.token0));
    }
    let mut out = vec![];
    fn dfs<'a>(adj: &HashMap<&'a str, Vec<(usize, bool, &'a str)>>, cur: &'a str, tout: &str, depth: usize, max: usize, seen: &mut Vec<&'a str>, path: &mut Path, out: &mut Vec<Path>) {
        if cur == tout && !path.is_empty() { out.push(path.clone()); return; }
        if depth == max { return; }
        if let Some(v) = adj.get(cur) { for &(i, z, nx) in v {
            if seen.contains(&nx) { continue; }
            seen.push(nx); path.push(Hop { pool: i, zfo: z }); dfs(adj, nx, tout, depth + 1, max, seen, path, out); path.pop(); seen.pop();
        } }
    }
    let mut seen = vec![tin]; let mut p = vec![];
    // note: seen holds &str from adj keys; tin may not be a key
    let key = adj.keys().find(|k| **k == tin).copied();
    if let Some(k) = key { seen[0] = k; dfs(&adj, k, tout, 0, max_hops, &mut seen, &mut p, &mut out); }
    out
}

/// simulate one path on mutable state; None if any hop panics (revert) or zero out
thread_local! { /// per-thread step cap (fast-path threads lower it; each step is real on-chain gas so lower = cheaper routes only)
    pub static STEP_CAP: std::cell::Cell<u32> = const { std::cell::Cell::new(MAX_STEPS_PER_SWAP) }; }
pub fn sim_path(st: &mut [Pool], path: &Path, amt: U256) -> Option<U256> {
    let mut a = amt;
    for h in path {
        let p = &mut st[h.pool];
        let snapshot = p.clone(); let c0 = p.crossed;
        let r = p.swap_try(h.zfo, a, None, STEP_CAP.with(|c| c.get()));
        match r { Ok((used, o)) if used == a && !o.is_zero() && p.crossed - c0 <= MAX_CROSS_PER_TX => a = o, // partial fill => reject (would leave input unspent)
                  _ => { *p = snapshot; return None } }
    }
    Some(a)
}
pub fn quote_path(st: &[Pool], path: &Path, amt: U256) -> Option<U256> {
    let mut c: Vec<Pool> = path.iter().map(|h| st[h.pool].clone()).collect();
    let local: Path = path.iter().enumerate().map(|(i, h)| Hop { pool: i, zfo: h.zfo }).collect();
    // same pool twice in one path is impossible (tokens unique), so local copy is exact
    sim_path(&mut c, &local, amt)
}

pub struct Route { pub legs: Vec<(Path, U256)>, pub out: U256, pub gross_single_best: U256, pub single_best_path: Option<usize> }

/// gas_cost_out: estimated gas cost per hop expressed in tokenOut units (subtracted per used hop)
pub fn route(pools_all: &[Pool], ps_all: &[Path], amt: U256, chunks: u32, gas_per_hop_out: U256, gas_base_out: U256) -> Route {
    route_k(pools_all, ps_all, amt, chunks, gas_per_hop_out, gas_base_out, 12)
}
/// keep top-k paths by marginal quality at (amt/chunks), compact the state to only touched pools
pub fn route_k(pools_all: &[Pool], ps_all: &[Path], amt: U256, chunks: u32, gas_per_hop_out: U256, gas_base_out: U256, k: usize) -> Route {
    route_kb(pools_all, ps_all, amt, chunks, gas_per_hop_out, gas_base_out, k, 12, None) }
/// Bounded variant: `ls_iters` local-search rounds and an optional wall-clock deadline (search stops, best-so-far is kept;
/// the returned route is always exactly re-simulated, so a cut search only costs optimality, never correctness).
pub fn route_kb(pools_all: &[Pool], ps_all: &[Path], amt: U256, chunks: u32, gas_per_hop_out: U256, gas_base_out: U256, k: usize, ls_iters: usize, deadline: Option<std::time::Instant>) -> Route {
    let probe = (amt / U256::from(chunks.max(1))).max(U256::from(1u8));
    let mut sc: Vec<(U256, usize)> = ps_all.iter().enumerate().filter_map(|(i, p)| quote_path(pools_all, p, probe).map(|o| (o.saturating_sub(gas_per_hop_out * U256::from(p.len())), i))).collect();
    sc.sort_by(|a, b| b.0.cmp(&a.0));
    // also always keep best full-amount single paths (top-k by full amount) so single-path optimum is never lost
    // full-amount sims only on the probe-ranked head (2k) -- bounds the expensive deep-tick-crossing sims
    let mut full: Vec<(U256, usize)> = sc.iter().take(2 * k).filter_map(|&(_, i)| quote_path(pools_all, &ps_all[i], amt).map(|o| (o, i))).collect();
    full.sort_by(|a, b| b.0.cmp(&a.0));
    let mut keep: Vec<usize> = sc.iter().take(k).map(|x| x.1).collect();
    for x in full.iter().take(3) { if !keep.contains(&x.1) { keep.push(x.1); } }
    let mut idx: Vec<usize> = vec![]; let mut remap = std::collections::HashMap::new();
    for &i in &keep { for h in &ps_all[i] { if !remap.contains_key(&h.pool) { remap.insert(h.pool, idx.len()); idx.push(h.pool); } } }
    let pools: Vec<Pool> = idx.iter().map(|&i| pools_all[i].clone()).collect();
    let ps: Vec<Path> = keep.iter().map(|&i| ps_all[i].iter().map(|h| Hop { pool: remap[&h.pool], zfo: h.zfo }).collect()).collect();
    let mut r = route_inner(&pools, &ps, amt, chunks, gas_per_hop_out, gas_base_out, ls_iters, deadline);
    // map legs back to global pool indices
    let inv: Vec<usize> = idx.clone();
    for (p, _) in r.legs.iter_mut() { for h in p.iter_mut() { h.pool = inv[h.pool]; } }
    r
}
fn route_inner(pools: &[Pool], ps: &[Path], amt: U256, chunks: u32, gas_per_hop_out: U256, gas_base_out: U256, ls_iters: usize, deadline: Option<std::time::Instant>) -> Route {
    let late = || deadline.map(|d| std::time::Instant::now() > d).unwrap_or(false);
    // best single path
    let mut best1 = (U256::ZERO, None);
    for (i, p) in ps.iter().enumerate() { if let Some(o) = quote_path(pools, p, amt) { let net = o.saturating_sub(gas_per_hop_out * U256::from(p.len())); if net > best1.0 { best1 = (net, Some(i)); } } }
    // greedy chunks on shared state
    let mut st: Vec<Pool> = pools.to_vec();
    let mut alloc = vec![U256::ZERO; ps.len()];
    let k = U256::from(chunks.max(1));
    let chunk = amt / k; let mut rem = amt;
    for c in 0..chunks {
        let a = if c + 1 == chunks { rem } else { chunk };
        if a.is_zero() { continue; }
        let mut best: (U256, Option<usize>) = (U256::ZERO, None);
        for (i, p) in ps.iter().enumerate() {
            // marginal output of this chunk on this path given current shared state; new path pays its hop gas
            let mut tmp: Vec<Pool> = p.iter().map(|h| st[h.pool].clone()).collect();
            let local: Path = p.iter().enumerate().map(|(j, h)| Hop { pool: j, zfo: h.zfo }).collect();
            if let Some(o) = sim_path(&mut tmp, &local, a) {
                let pen = if alloc[i].is_zero() { gas_per_hop_out * U256::from(p.len()) } else { U256::ZERO };
                let net = o.saturating_sub(pen);
                if net > best.0 { best = (net, Some(i)); }
            }
        }
        let Some(i) = best.1 else { break };
        sim_path(&mut st, &ps[i], a).unwrap();
        alloc[i] += a; rem -= a;
    }
    // exact re-simulation in execution order (allocated paths, in index order) on fresh state
    let exec = |alloc: &Vec<U256>| -> Option<(U256, usize)> {
        let mut s: Vec<Pool> = pools.to_vec(); let mut tot = U256::ZERO; let mut hops = 0;
        for (i, a) in alloc.iter().enumerate() { if a.is_zero() { continue; } tot += sim_path(&mut s, &ps[i], *a)?; hops += ps[i].len(); }
        Some((tot, hops))
    };
    let net = |alloc: &Vec<U256>| exec(alloc).map(|(t, h)| t.saturating_sub(gas_per_hop_out * U256::from(h)));
    let used: U256 = alloc.iter().copied().fold(U256::ZERO, |a, b| a + b);
    let mut best_split = if used == amt { net(&alloc) } else { None };
    // local search: move fractions between allocated paths
    if best_split.is_some() {
        let mut step = amt / U256::from(chunks.max(2) * 2);
        'ls: for _ in 0..ls_iters {
            let mut improved = false;
            let idx: Vec<usize> = (0..ps.len()).filter(|&i| !alloc[i].is_zero()).collect();
            for &from in &idx { if late() { break 'ls; } for to in 0..ps.len() { if from == to || alloc[from] < step || step.is_zero() { continue; }
                let mut t = alloc.clone(); t[from] -= step; t[to] += step;
                if let Some(v) = net(&t) { if Some(v) > best_split { best_split = Some(v); alloc = t; improved = true; } } } }
            if !improved { step /= U256::from(2u8); }
        }
    }
    let single = best1.0.saturating_sub(gas_base_out);
    match best_split { Some(v) if v.saturating_sub(gas_base_out) > single => {
            let legs = alloc.iter().enumerate().filter(|(_, a)| !a.is_zero()).map(|(i, a)| (ps[i].clone(), *a)).collect();
            Route { legs, out: v.saturating_sub(gas_base_out), gross_single_best: single, single_best_path: best1.1 } }
        _ => Route { legs: best1.1.map(|i| vec![(ps[i].clone(), amt)]).unwrap_or_default(), out: single, gross_single_best: single, single_best_path: best1.1 } }
}
