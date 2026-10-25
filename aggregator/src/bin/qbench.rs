use core::router::*;
use core::*;
use ruint::aliases::U256;
use std::time::Instant;
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (_, pools, _) = load(&a[1]);
    let (tin, tout, amt, hops, chunks, k) = (
        a[2].to_lowercase(),
        a[3].to_lowercase(),
        U256::from_str_radix(&a[4], 10).unwrap(),
        a[5].parse().unwrap(),
        a[6].parse().unwrap(),
        a[7].parse().unwrap(),
    );
    let t = Instant::now();
    let ps = paths(&pools, &tin, &tout, hops);
    let tp = t.elapsed();
    let t = Instant::now();
    for p in ps.iter().take(400) {
        let t1 = Instant::now();
        let _ = quote_path(&pools, p, amt);
        let d = t1.elapsed();
        if d.as_millis() > 5 {
            println!(
                "slow path {:?} {:?}",
                d,
                p.iter()
                    .map(|h| (
                        &pools[h.pool].addr,
                        &pools[h.pool].venue,
                        pools[h.pool].ticks.len()
                    ))
                    .collect::<Vec<_>>()
            );
        }
    }
    let tq = t.elapsed();
    let t = Instant::now();
    let r = route_kb(
        &pools,
        &ps,
        amt,
        chunks,
        U256::ZERO,
        U256::ZERO,
        k,
        if chunks <= 4 { 0 } else { 12 },
        None,
    );
    println!(
        "paths {} in {:?}; all-path full sims {:?}; route_k {:?} out {}",
        ps.len(),
        tp,
        tq,
        t.elapsed(),
        r.out
    );
}
