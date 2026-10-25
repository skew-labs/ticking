use core::*; use ruint::aliases::U256;
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&a[2]).unwrap()).unwrap();
    let (blk, pools, _) = load(&a[1]);
    assert_eq!(blk, v["block"].as_u64().unwrap(), "BLOCK MISMATCH");
    let m: std::collections::HashMap<_, _> = pools.iter().map(|p| (p.addr.clone(), p)).collect();
    std::panic::set_hook(Box::new(|_| {}));
    let (mut n, mut ok, mut bad, mut rev, mut rev_agree) = (0, 0, 0, 0, 0);
    for c in v["cases"].as_array().unwrap() {
        let p = m[&c["pool"].as_str().unwrap().to_lowercase()];
        let amt = U256::from_str_radix(c["amount"].as_str().unwrap(), 10).unwrap();
        let zfo = c["zfo"].as_bool().unwrap();
        let res = std::panic::catch_unwind(|| { let mut q = p.clone(); let (_, o) = q.swap_exact_in(zfo, amt, None); (o, q.sqrt_p) });
        n += 1;
        if c.get("q_err").is_some() { rev += 1; if res.is_err() || res.as_ref().unwrap().0.is_zero() { rev_agree += 1 } else if a.len()>3 { eprintln!("onchain revert, ours ok {:?} amt={} liq={} err={}", res.as_ref().unwrap(), amt, p.liq, c["q_err"]) } continue; }
        let qo = U256::from_str_radix(c["q_out"].as_str().unwrap(), 10).unwrap();
        let qs = U256::from_str_radix(c["q_sqrt"].as_str().unwrap(), 10).unwrap();
        match res { Ok((o, s)) if o == qo && s == qs => ok += 1,
            r => { bad += 1; if bad < 15 { eprintln!("MISMATCH {} ours={:?} chain_out={} chain_sqrt={}", c, r.ok(), qo, qs); } } }
    }
    println!("block {blk} cases {n} exact_match(out+sqrtAfter) {ok} mismatch {bad} onchain_reverts {rev} (our engine also rejects {rev_agree})");
}
