//! Exact Uniswap V3 (v3-core 1.0.0) swap math, U256 bit-exact. Paranoid: no floats in the money path.
use ruint::aliases::{U256, U512};
pub mod router;

pub const MIN_TICK: i32 = -887272;
pub const MAX_TICK: i32 = 887272;
pub fn min_sqrt() -> U256 { U256::from(4295128739u64) }
pub fn max_sqrt() -> U256 { U256::from_str_radix("1461446703485210103287273052203988822378723970342", 10).unwrap() }
fn q96() -> U256 { U256::from(1u8) << 96usize }

#[inline] pub fn mul_div(a: U256, b: U256, d: U256) -> U256 {
    assert!(!d.is_zero());
    let p = U512::from(a) * U512::from(b);
    let q = p / U512::from(d);
    assert!(q <= U512::from(U256::MAX), "mulDiv overflow");
    U256::from(q)
}
#[inline] pub fn mul_div_up(a: U256, b: U256, d: U256) -> U256 {
    let p = U512::from(a) * U512::from(b);
    let dd = U512::from(d);
    let mut q = p / dd;
    if !(p % dd).is_zero() { q += U512::from(1u8); }
    assert!(q <= U512::from(U256::MAX), "mulDivUp overflow");
    U256::from(q)
}
#[inline] fn div_up(a: U256, b: U256) -> U256 { let q = a / b; if (a % b).is_zero() { q } else { q + U256::from(1u8) } }

const C: [&str; 19] = ["fff97272373d413259a46990580e213a","fff2e50f5f656932ef12357cf3c7fdcc","ffe5caca7e10e4e61c3624eaa0941cd0","ffcb9843d60f6159c9db58835c926644","ff973b41fa98c081472e6896dfb254c0","ff2ea16466c96a3843ec78b326b52861","fe5dee046a99a2a811c461f1969c3053","fcbe86c7900a88aedcffc83b479aa3a4","f987a7253ac413176f2b074cf7815e54","f3392b0822b70005940c7a398e4b70f3","e7159475a2c29b7443b29c7fa6e889d9","d097f3bdfd2022b8845ad8f792aa5825","a9f746462d870fdf8a65dc1f90e061e5","70d869a156d2a1b890bb3df62baf32f7","31be135f97d08fd981231505542fcfa6","9aa508b5b7a84e1c677de54f3e99bc9","5d6af8dedb81196699c329225ee604","2216e584f5fa1ea926041bedfe98","48a170391f7dc42444e8fa2"];
pub fn sqrt_at_tick(tick: i32) -> U256 {
    let abs = tick.unsigned_abs();
    assert!(abs <= MAX_TICK as u32, "T");
    static K: std::sync::OnceLock<(U256, Vec<U256>)> = std::sync::OnceLock::new();
    let k = K.get_or_init(|| (U256::from_str_radix("fffcb933bd6fad37aa2d162d1a594001", 16).unwrap(), C.iter().map(|c| U256::from_str_radix(c, 16).unwrap()).collect()));
    let mut r = if abs & 1 != 0 { k.0 } else { U256::from(1u8) << 128usize };
    for (i, c) in k.1.iter().enumerate() {
        if abs & (2u32 << i) != 0 { r = (r * *c) >> 128usize; }
    }
    if tick > 0 { r = U256::MAX / r; }
    let lo = r % (U256::from(1u8) << 32usize);
    (r >> 32usize) + if lo.is_zero() { U256::ZERO } else { U256::from(1u8) }
}
/// greatest tick with sqrt_at_tick(t) <= p (exact definition of getTickAtSqrtRatio)
pub fn tick_at_sqrt(p: U256) -> i32 {
    assert!(p >= min_sqrt() && p < max_sqrt(), "R");
    // float guess, then exact correction with sqrt_at_tick (result is exact by definition, guess only seeds the search)
    let f: f64 = { let (w, sh) = { let bits = p.bit_len(); let sh = bits.saturating_sub(64); ((p >> sh).to::<u64>() as f64, sh as i32) }; (w.ln() + (sh as f64 - 96.0) * std::f64::consts::LN_2) * 2.0 / 1.0001f64.ln() };
    let g = (f.floor() as i64).clamp(MIN_TICK as i64, MAX_TICK as i64) as i32;
    let (mut lo, mut hi) = ((g - 2).max(MIN_TICK), (g + 2).min(MAX_TICK));
    if sqrt_at_tick(lo) > p { lo = MIN_TICK; }
    if hi < MAX_TICK && sqrt_at_tick(hi + 1) <= p { hi = MAX_TICK; }
    while lo < hi { let m = lo + (hi - lo + 1) / 2; if sqrt_at_tick(m) <= p { lo = m } else { hi = m - 1 } }
    lo
}
pub fn amount0_delta(mut a: U256, mut b: U256, l: u128, up: bool) -> U256 {
    if a > b { std::mem::swap(&mut a, &mut b); }
    let n1 = U256::from(l) << 96usize; let n2 = b - a;
    assert!(!a.is_zero());
    if up { div_up(mul_div_up(n1, n2, b), a) } else { mul_div(n1, n2, b) / a }
}
pub fn amount1_delta(mut a: U256, mut b: U256, l: u128, up: bool) -> U256 {
    if a > b { std::mem::swap(&mut a, &mut b); }
    if up { mul_div_up(U256::from(l), b - a, q96()) } else { mul_div(U256::from(l), b - a, q96()) }
}
fn next_from_amount0_up(p: U256, l: u128, amt: U256) -> U256 {
    if amt.is_zero() { return p; }
    let n1 = U256::from(l) << 96usize;
    let prod = amt.wrapping_mul(p); // solidity: unchecked mul, then check product/amount==price
    if prod / amt == p { let den = n1.wrapping_add(prod); if den >= n1 { return mul_div_up(n1, p, den); } }
    div_up(n1, (n1 / p) + amt)
}
fn next_from_amount1_down(p: U256, l: u128, amt: U256) -> U256 {
    let u160max = (U256::from(1u8) << 160usize) - U256::from(1u8);
    let q = if amt <= u160max { (amt << 96usize) / U256::from(l) } else { mul_div(amt, q96(), U256::from(l)) };
    let r = p + q; assert!(r <= u160max); r
}
/// SwapMath.computeSwapStep, exact-input only. returns (next, in, out, fee)
pub fn swap_step(cur: U256, target: U256, l: u128, rem: U256, fee: u32) -> (U256, U256, U256, U256) {
    let zfo = cur >= target;
    let e6 = U256::from(1_000_000u32); let f = U256::from(fee);
    let less = mul_div(rem, e6 - f, e6);
    let mut ain = if zfo { amount0_delta(target, cur, l, true) } else { amount1_delta(cur, target, l, true) };
    let next = if less >= ain { target } else if l == 0 { target } else if zfo { next_from_amount0_up(cur, l, less) } else { next_from_amount1_down(cur, l, less) };
    // NB: contract calls getNextSqrtPriceFromInput which requires liquidity>0; with l==0 ain==0 so branch above is taken.
    let max = next == target;
    let aout;
    if zfo { if !max { ain = amount0_delta(next, cur, l, true); } aout = amount1_delta(next, cur, l, false); }
    else { if !max { ain = amount1_delta(cur, next, l, true); } aout = amount0_delta(cur, next, l, false); }
    let fee_amt = if next != target { rem - ain } else { mul_div_up(ain, f, e6 - f) };
    (next, ain, aout, fee_amt)
}

#[derive(Clone, Debug)]
pub struct Pool { pub addr: String, pub token0: String, pub token1: String, pub fee: u32, pub protocol_fee: u32, pub spacing: i32,
    pub sqrt_p: U256, pub tick: i32, pub liq: u128,
    pub bal0: U256, pub bal1: U256,
    pub crossed: u32,
    pub ticks: std::sync::Arc<Vec<(i32, i128)>>, // sorted by tick, initialized only
    pub venue: String,
}
impl Pool {
    /// Uniswap v4 ProtocolFeeLibrary.calculateSwapFee (assembly truncates the product term).
    /// protocol_fee packed: low12 = zeroForOne, high12 = oneForZero. V3 pools leave this 0.
    #[inline] pub fn effective_fee(&self, zfo: bool) -> u32 {
        let p = if zfo { self.protocol_fee & 0xfff } else { (self.protocol_fee >> 12) & 0xfff };
        if p == 0 { return self.fee; }
        let lp = self.fee; let num = (p as u64) * (lp as u64);
        (p + lp - (num / 1_000_000) as u32)
    }
    #[inline] fn compress(&self, t: i32) -> i32 { t.div_euclid(self.spacing) }
    /// TickBitmap.nextInitializedTickWithinOneWord, exact semantics (word boundaries included)
    pub fn next_tick(&self, tick: i32, lte: bool) -> (i32, bool) {
        let comp = self.compress(tick);
        if lte {
            let word = comp >> 8; let lo = word << 8;
            // largest initialized compressed in [lo, comp]
            let idx = self.ticks.partition_point(|&(t, _)| self.compress(t) <= comp);
            if idx > 0 { let c = self.compress(self.ticks[idx - 1].0); if c >= lo { return (c * self.spacing, true); } }
            (lo * self.spacing, false)
        } else {
            let c1 = comp + 1; let word = c1 >> 8; let hi = (word << 8) + 255;
            let idx = self.ticks.partition_point(|&(t, _)| self.compress(t) < c1);
            if idx < self.ticks.len() { let c = self.compress(self.ticks[idx].0); if c <= hi { return (c * self.spacing, true); } }
            (hi * self.spacing, false)
        }
    }
    fn net(&self, t: i32) -> i128 { let i = self.ticks.binary_search_by_key(&t, |x| x.0).expect("tick"); self.ticks[i].1 }
    /// UniswapV3Pool.swap exact-input with given price limit; mutates state. returns (amountIn consumed incl fee, amountOut)
    pub fn swap_exact_in(&mut self, zfo: bool, amount: U256, limit: Option<U256>) -> (U256, U256) { self.swap_exact_in_lim(zfo, amount, limit, u32::MAX) }
    /// max_steps: abort (panic) after this many loop steps; each step costs real gas on-chain, so a cap is gas-honest
    pub fn swap_exact_in_lim(&mut self, zfo: bool, amount: U256, limit: Option<U256>, max_steps: u32) -> (U256, U256) {
        match self.swap_try(zfo, amount, limit, max_steps) { Ok(r) => r, Err(e) => panic!("{}", e) } }
    /// Non-panicking exact swap. Zero-liquidity stretches are fast-forwarded to the next initialized tick (exact: with L=0 every
    /// intermediate step moves price with amountIn=amountOut=fee=0), while the skipped bitmap words still count as steps (gas-honest cap).
    pub fn swap_try(&mut self, zfo: bool, amount: U256, limit: Option<U256>, max_steps: u32) -> Result<(U256, U256), &'static str> {
        let mut steps = 0u32;
        let lim = limit.unwrap_or(if zfo { min_sqrt() + U256::from(1u8) } else { max_sqrt() - U256::from(1u8) });
        let mut rem = amount; let mut out = U256::ZERO;
        while !rem.is_zero() && self.sqrt_p != lim {
            steps += 1; if steps > max_steps { return Err("STEPS"); }
            if self.liq == 0 {
                let comp = self.compress(self.tick);
                let nxt = if zfo { let i = self.ticks.partition_point(|&(t, _)| self.compress(t) <= comp); if i > 0 { Some(self.ticks[i - 1].0) } else { None } }
                          else { let i = self.ticks.partition_point(|&(t, _)| self.compress(t) <= comp); self.ticks.get(i).map(|x| x.0) };
                let tc = nxt.map(|t| self.compress(t)).unwrap_or(if zfo { MIN_TICK.div_euclid(self.spacing) } else { MAX_TICK.div_euclid(self.spacing) });
                let words = ((comp >> 8) - (tc >> 8)).unsigned_abs(); steps = steps.saturating_add(words); if steps > max_steps { return Err("STEPS"); }
                match nxt.map(|t| (t, sqrt_at_tick(t))) {
                    Some((t, sn)) if (zfo && sn > lim) || (!zfo && sn < lim) => { if sn != self.sqrt_p || words > 0 { self.sqrt_p = sn; self.tick = if zfo { t } else { t - 1 }; } }
                    _ => { self.sqrt_p = lim; self.tick = tick_at_sqrt(lim); break; }
                }
            }
            let start = self.sqrt_p;
            let (mut nt, init) = self.next_tick(self.tick, zfo);
            nt = nt.clamp(MIN_TICK, MAX_TICK);
            let sn = sqrt_at_tick(nt);
            let tgt = if zfo { if sn < lim { lim } else { sn } } else if sn > lim { lim } else { sn };
            let (np, ain, aout, fee) = swap_step(self.sqrt_p, tgt, self.liq, rem, self.effective_fee(zfo));
            self.sqrt_p = np; rem -= ain + fee; out += aout;
            if np == sn {
                if init { self.crossed += 1; let mut n = self.net(nt); if zfo { n = -n; }
                    self.liq = if n < 0 { self.liq.checked_sub(n.unsigned_abs()).ok_or("LS")? } else { self.liq.checked_add(n as u128).ok_or("LA")? }; }
                self.tick = if zfo { nt - 1 } else { nt };
            } else if np != start { self.tick = tick_at_sqrt(np); }
        }
        // pool transfers output before callback: real token balance is a hard cap (on-chain reverts otherwise)
        let bal = if zfo { &mut self.bal1 } else { &mut self.bal0 };
        if out > *bal { return Err("INSUFFICIENT_POOL_BALANCE"); }
        *bal -= out; let used = amount - rem;
        if zfo { self.bal0 += used } else { self.bal1 += used }
        Ok((used, out))
    }
    pub fn quote(&self, zfo: bool, amount: U256) -> (U256, U256) { let mut c = self.clone(); c.swap_exact_in(zfo, amount, None) }
}

#[derive(serde::Deserialize)] pub struct JPool { pub pool: String, pub token0: String, pub token1: String, pub fee: u32, #[serde(default, rename="protocolFee")] pub protocol_fee: u32, pub spacing: i32, #[serde(rename="sqrtPriceX96")] pub sqrt: String, pub tick: i32, pub liquidity: String, #[serde(default)] pub bal0: Option<String>, #[serde(default)] pub bal1: Option<String>, pub ticks: Vec<(i32, String)>, #[serde(default)] pub venue: String }
#[derive(serde::Deserialize)] pub struct JTok { pub decimals: u32, pub symbol: String }
#[derive(serde::Deserialize)] pub struct JState { pub block: u64, pub pools: Vec<JPool>, pub tokens: std::collections::HashMap<String, JTok> }
pub fn load(path: &str) -> (u64, Vec<Pool>, std::collections::HashMap<String, JTok>) {
    let s: JState = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let pools = s.pools.into_iter().map(|p| {
        let mut t: Vec<(i32, i128)> = p.ticks.iter().map(|(a, b)| (*a, b.parse().unwrap())).collect(); t.sort();
        Pool { addr: p.pool.to_lowercase(), token0: p.token0.to_lowercase(), token1: p.token1.to_lowercase(), fee: p.fee, protocol_fee: p.protocol_fee, spacing: p.spacing,
            sqrt_p: U256::from_str_radix(&p.sqrt, 10).unwrap(), tick: p.tick, liq: p.liquidity.parse().unwrap(), bal0: U256::from_str_radix(p.bal0.as_deref().unwrap_or("1606938044258990275541962092341162602522202993782792835301376"),10).unwrap(), bal1: U256::from_str_radix(p.bal1.as_deref().unwrap_or("1606938044258990275541962092341162602522202993782792835301376"),10).unwrap(), crossed: 0, ticks: std::sync::Arc::new(t), venue: p.venue }
    }).collect();
    (s.block, pools, s.tokens.into_iter().map(|(k, v)| (k.to_lowercase(), v)).collect())
}
