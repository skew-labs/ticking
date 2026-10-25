use core::*;
use ruint::aliases::U256;
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (blk, pools, _) = load(&a[1]);
    let p = pools
        .iter()
        .find(|p| p.addr == a[2].to_lowercase())
        .unwrap();
    let zfo: bool = a[3].parse().unwrap();
    let amt = U256::from_str_radix(&a[4], 10).unwrap();
    println!(
        "pool fee={} spacing={} tick={} liq={} sqrt={} ticks={}",
        p.fee,
        p.spacing,
        p.tick,
        p.liq,
        p.sqrt_p,
        p.ticks.len()
    );
    let mut q = p.clone();
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        q.swap_exact_in(zfo, amt, None)
    }));
    match r {
        Ok((u, o)) => println!(
            "used={} out={} end_tick={} end_sqrt={} crossed={}",
            u, o, q.tick, q.sqrt_p, q.crossed
        ),
        Err(_) => println!("PANIC"),
    }
}
