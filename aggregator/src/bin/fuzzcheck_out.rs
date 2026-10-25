use core::*; use ruint::aliases::U256;
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&a[2]).unwrap()).unwrap();
    let (blk, pools, _) = load(&a[1]);
    assert_eq!(blk, v["block"].as_u64().unwrap());
    let m: std::collections::HashMap<_, _> = pools.iter().map(|p| (p.addr.clone(), p)).collect();
    std::panic::set_hook(Box::new(|_| {}));
    let (mut n, mut ok, mut bad, mut rev, mut rev_agree) = (0u64,0,0,0,0);
    for c in v["cases"].as_array().unwrap() {
        let p = match m.get(&c["pool"].as_str().unwrap().to_lowercase()) { Some(x)=>x, None=>{eprintln!("missing pool {}", c["pool"]); continue} };
        let amt = U256::from_str_radix(c["amount"].as_str().unwrap(), 10).unwrap();
        let zfo = c["zfo"].as_bool().unwrap();
        let res = std::panic::catch_unwind(|| { let mut q = (*p).clone(); q.swap_exact_in(zfo, amt, None).1 });
        n += 1;
        if c.get("q_err").is_some() { rev += 1; if res.is_err() || res.as_ref().ok().map(|o| o.is_zero()).unwrap_or(false) { rev_agree += 1 } else if a.len()>3 { eprintln!("chain rev ours {:?}", res); } continue; }
        let qo = U256::from_str_radix(c["q_out"].as_str().unwrap(), 10).unwrap();
        match res { Ok(o) if o == qo => ok += 1,
            r => { bad += 1; if bad < 20 { eprintln!("MISMATCH amt={} zfo={} pool={} ours={:?} chain={}", amt, zfo, c["pool"], r.ok(), qo); } } }
    }
    println!("block {blk} cases {n} exact_match_out {ok} mismatch {bad} onchain_reverts {rev} (engine also rejects/zero {rev_agree})");
}
