use core::*; use ruint::aliases::U256; use std::time::Instant;
fn main(){ let a: Vec<String> = std::env::args().collect(); let (_, pools, _) = load(&a[1]);
 let p = pools.iter().max_by_key(|p| p.liq.min(u128::MAX)).unwrap(); // just some deep pool
 for amt in [U256::from(10u64.pow(15)), U256::from(10u64.pow(18)), U256::from(10u128.pow(20))] {
  let n=20000; let t=Instant::now(); let mut o=U256::ZERO; for _ in 0..n { o = p.quote(true, amt).1; }
  let t2=Instant::now(); for _ in 0..n { std::hint::black_box(tick_at_sqrt(p.sqrt_p)); } let ta=t2.elapsed();
  let t3=Instant::now(); for _ in 0..n { std::hint::black_box(p.clone()); } 
  println!("pool {} amt {} out {} : quote {:.2}us  tick_at_sqrt {:.2}us clone {:.3}us", p.addr, amt, o, t.elapsed().as_nanos() as f64/n as f64/1e3, ta.as_nanos() as f64/n as f64/1e3, t3.elapsed().as_nanos() as f64/n as f64/1e3); } }
