import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {classifyTrackIntent} from '../src/track-intent.js';
import {
 FAST_FEED_AT,HOT_MS,SPEED_OPTS,WICK_BAND,
 applyTip,decodeTipSample,fastFeedSamples,fastFeedUrl,isBinance1s,isTipChain,mergeBars1s,paintOpenSecond,prewarmBuckets,pullFastFeed,runSpeedOpts,sanitizeTipBar,shouldPaintTip,
} from '../src/speed-pass.js';

const ids=SPEED_OPTS.map(row=>row[0]);
assert.equal(ids.length,100);
assert.equal(new Set(ids).size,100);

const ctx=runSpeedOpts();
assert.equal(ctx.tipArb,true);
assert.equal(ctx.tipRh,true);
assert.equal(ctx.tipSol,false);
assert.equal(ctx.aliasArb,'arbitrum');
assert.equal(ctx.aliasRh,'robinhood');
assert.equal(ctx.band,0.05);
assert.equal(WICK_BAND,0.05);
assert.equal(ctx.hot,120_000);
assert.equal(ctx.feedOpen,true);
assert.equal(ctx.feedEarly,true);
assert.equal(ctx.wickHigh,105);
assert.equal(ctx.wickLow,95);
assert.equal(ctx.wickInside,101);
assert.equal(ctx.wickBad,null);
assert.equal(ctx.kept,2);
assert.equal(ctx.keptSource,'binance_1s');
assert.equal(ctx.binanceWins,true);
assert.equal(ctx.noFake,true);
assert.equal(ctx.cap,480);
assert.equal(ctx.staleDrop,0);
assert.equal(ctx.age,3000);
assert.equal(ctx.fresh,true);
assert.equal(ctx.stale,true);
assert.equal(ctx.order,'1700000001000,1700000002000');
assert.equal(ctx.round,1_700_000_001_000);
assert.equal(ctx.roundClose,4);
assert.equal(ctx.roundSame,1);
assert.equal(ctx.roundBinance,'binance_1s');
assert.equal(ctx.prewarm,5);
assert.equal(ctx.tipInterval,'1s');
assert.match(ctx.url,/chain=arbitrum/);
assert.match(ctx.url,/coverage=tip_hot/);
assert.match(ctx.url,/interval=1s/);
assert.match(ctx.urlRh,/chain=robinhood/);
assert.equal(ctx.subArb,'Arbitrum tip · latency paint');
assert.equal(ctx.subRh,'Robinhood tip · latency paint');
assert.equal(isTipChain('arb'),true);
assert.equal(isTipChain('rh'),true);
assert.equal(isTipChain('sol'),false);
assert.equal(isBinance1s({venue:'binance_1s'}),true);

const inside=sanitizeTipBar({time:1000,open:100,high:104,low:96,close:100});
assert.equal(inside.high,104);
assert.equal(inside.low,96);
assert.equal(inside.latencyPaintOnly,true);
assert.equal(inside.coverage,'tip_hot');
const kept=mergeBars1s(
 [{time:1000,open:2,high:4,low:2,close:2,volume:1,source:'binance_1s'}],
 [{time:1000,open:9,high:20,low:1,close:9,volume:0}],
 {now:1000},
);
assert.equal(kept.series[0].close,2);
assert.equal(kept.series[0].high,4);
assert.equal(kept.series[0].source,'binance_1s');
const venue=mergeBars1s(
 [{time:1000,open:3,high:3,low:3,close:3,source:'tip_hot',latencyPaintOnly:true}],
 [{time:1000,open:8,high:20,low:8,close:8,source:'binance_1s'}],
 {now:1000},
);
assert.equal(venue.series[0].source,'binance_1s');
assert.equal(venue.series[0].high,20);
assert.equal(venue.series[0].latencyPaintOnly,false);
const opened=paintOpenSecond([{time:1_700_000_000_000,open:4,high:4,low:4,close:4,volume:0,source:'tip_hot'}],1_700_000_001_500);
assert.equal(opened.at(-1).time,1_700_000_001_000);
assert.equal(opened.at(-1).close,4);
const same=paintOpenSecond([{time:1_700_000_001_000,open:7,high:7,low:7,close:7,source:'binance_1s'}],1_700_000_001_000);
assert.equal(same.length,1);
assert.equal(same[0].source,'binance_1s');
const stale=mergeBars1s([],[{time:1_700_000_000_000,open:1,high:1,low:1,close:1}],{now:1_700_000_000_000+HOT_MS+5000});
assert.equal(stale.series.length,0);
const bad=applyTip([{time:1_700_000_000_000,open:1,high:1,low:1,close:1,source:'tip_hot'}],[{open:'x'}],{now:1_700_000_000_000});
assert.equal(bad.failSoft,true);
assert.ok(bad.series.length>=1);

let fetches=0;
const sol=await pullFastFeed({chain:'solana',fetchImpl(){fetches+=1;throw new Error('pulled');}});
assert.equal(fetches,0);
assert.equal(sol.coverage,'idle');
assert.equal(sol.reason,'solana-not-tip');
const early=await pullFastFeed({chain:'arbitrum',now:FAST_FEED_AT-1,fetchImpl(){fetches+=1;throw new Error('early');}});
assert.equal(fetches,0);
assert.equal(early.coverage,'scheduled');
const soft=await pullFastFeed({
 chain:'robinhood',now:FAST_FEED_AT,
 fetchImpl:async()=>({ok:false,status:404,json:async()=>({})}),
});
assert.equal(soft.ok,false);
assert.equal(soft.coverage,'fail_soft');
assert.equal(soft.bars.length,0);
let seen='';
const live=await pullFastFeed({
 chain:'arb',symbol:'ETH',mint:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',now:FAST_FEED_AT,
 fetchImpl:async url=>({ok:true,json:async()=>{seen=url;return {coverage:'tip_hot',bars_1s:[{time:FAST_FEED_AT,open:1,high:1,low:1,close:1,volume:0}]};}}),
});
assert.equal(live.ok,true);
assert.equal(live.bars.length,1);
assert.match(seen,/chain=arbitrum/);
assert.match(seen,/coverage=tip_hot/);
assert.match(seen,/interval=1s/);
assert.match(fastFeedUrl({chain:'rh',symbol:'AAPL'}),/chain=robinhood/);
await assert.rejects(pullFastFeed({
 chain:'arbitrum',now:FAST_FEED_AT,
 fetchImpl(){const error=new Error('aborted');error.name='AbortError';throw error;},
}));
const thrown=await pullFastFeed({
 chain:'arbitrum',now:FAST_FEED_AT,
 fetchImpl(){throw new Error('down');},
});
assert.equal(thrown.coverage,'fail_soft');
assert.equal(thrown.ok,false);

const repaired=sanitizeTipBar({time:1_700_000_000_000,open:10,high:10.1,low:9.9,close:10.2});
assert.equal(repaired.close,10.2);
assert.equal(repaired.high,10.2);
assert.ok(repaired.low<=10&&repaired.low>=9.5);
assert.equal(repaired.latencyPaintOnly,true);
const printed=sanitizeTipBar({ts:1_700_000_000_000_000_000,px:100});
assert.equal(printed.time,1_700_000_000_000);
assert.equal(printed.close,100);
assert.equal(printed.high,100);
const quoted=sanitizeTipBar({t:1_700_000_000,bid:99,ask:101});
assert.equal(quoted.close,100);
assert.equal(quoted.high,101);
assert.equal(quoted.low,99);
const scaled=sanitizeTipBar({time:1_700_000_000_000,o:100*1e8,h:400*1e8,l:1e8,c:100*1e8,scale:1e8});
assert.equal(scaled.close,100);
assert.equal(scaled.high,105);
assert.equal(scaled.low,95);
const tuple=sanitizeTipBar([1_700_000_000,100,200,1,100,2]);
assert.equal(tuple.high,105);
assert.equal(tuple.low,95);
assert.equal(tuple.volume,2);
assert.equal(decodeTipSample({time:1_700_000_000_000,open:0,high:1,low:1,close:1}),null);
const mixed=mergeBars1s([], [
 {time:1_700_000_000_000,open:100,high:100,low:100,close:100,symbol:'WETH'},
 {time:1_700_000_000_000,open:100,high:10.1,low:9.9,close:100.2,symbol:'WETH'},
 {time:1_700_000_000_000,px:1000,symbol:'WETH'},
 {time:1_700_000_001_000,px:100,symbol:'WETH'},
], {now:1_700_000_001_000});
assert.equal(mixed.series.length,2);
assert.equal(mixed.series[0].close,100.2);
assert.equal(mixed.series[0].high,105.21);
assert.ok(mixed.series[0].low>=95);
assert.equal(mixed.series[0].source,'tip_hot');
assert.equal(mixed.series[1].close,100);
assert.equal(mixed.failSoft,true);
const other=fastFeedSamples({
 bars_1s:[{time:1,symbol:'WETH',px:1},{time:2,symbol:'ARB',px:2}],
 wickRejects:[{time:3,px:3},{time:4,symbol:'WETHUSD',px:4}],
}, 'WETH');
assert.deepEqual(other.map(row=>row.px??row.symbol),[1,3,4]);
let rejectUrl='';
const rejects=await pullFastFeed({
 chain:'arbitrum',symbol:'WETH',now:FAST_FEED_AT,
 fetchImpl:async url=>({ok:true,json:async()=>{
  rejectUrl=url;
  return {
   coverage:'tip_hot',
   applied:1,
   bars_1s:[{time:FAST_FEED_AT,open:100,high:100,low:100,close:100,symbol:'WETH'}],
   wickRejects:[
    {time:FAST_FEED_AT,open:100,high:10.1,low:9.9,close:100.2,symbol:'WETH'},
    {t:Math.floor(FAST_FEED_AT/1000)+1,px:100,symbol:'WETH'},
    {time:FAST_FEED_AT+2000,o:100,h:400,l:1,c:100,symbol:'WETH'},
    {time:FAST_FEED_AT+3000,o:100,h:400,l:1,c:100,symbol:'PEPE'},
   ],
  };
 }}),
});
assert.equal(rejects.ok,true);
assert.equal(rejects.failSoft,true);
assert.equal(rejects.bars.length,4);
const painted=mergeBars1s([], rejects.bars, {now:FAST_FEED_AT+3000});
assert.equal(painted.series.length,3);
assert.equal(painted.series[0].close,100.2);
assert.equal(painted.series[0].high,100.2);
assert.equal(painted.series[0].low,95);
assert.equal(painted.series[1].close,100);
assert.equal(painted.series[2].high,105);
assert.equal(painted.series[2].low,95);
assert.equal(painted.series[2].close,100);
assert.match(rejectUrl,/symbol=WETH/);
assert.equal(painted.series.every(bar=>bar.latencyPaintOnly&&bar.coverage==='tip_hot'),true);

const buckets=prewarmBuckets([
 {time:1_700_000_000_000,open:1,high:2,low:1,close:2,volume:1},
 {time:1_700_000_004_000,open:2,high:3,low:1,close:1,volume:1},
]);
assert.deepEqual([...buckets.keys()],[1000,3000,5000,15000,30000]);
assert.equal(buckets.get(1000).length,2);
assert.equal(buckets.get(5000).length,1);
assert.equal(buckets.get(5000)[0].high,3);
assert.equal(shouldPaintTip([]),true);
assert.equal(shouldPaintTip([{time:1_700_000_000_000,open:1,high:1,low:1,close:1},{time:1_700_000_001_000,open:1,high:1,low:1,close:1}]),true);
assert.equal(shouldPaintTip([{time:1_700_000_000_000,open:1,high:1,low:1,close:1},{time:1_700_000_060_000,open:1,high:1,low:1,close:1}]),false);
assert.equal(classifyTrackIntent('arbitrum chart').route,'#/console/workspace/ARB?chain=arbitrum');
assert.equal(classifyTrackIntent('price HOOD on robinhood').route,'#/console/workspace/HOOD?chain=robinhood');
assert.equal(classifyTrackIntent('SOL').route,'#/console/workspace/SOL');
assert.equal(classifyTrackIntent('PUMP').chain,null);

const chart=readFileSync(new URL('../src/chart.js',import.meta.url),'utf8');
const skipAt=chart.indexOf('if(frameSig===this._frameSig){this.trackLayer?.sync();return;}');
const fillAt=chart.indexOf('ctx.fillRect(0,0,width,height)',skipAt);
assert.ok(skipAt>0);
assert.ok(fillAt>skipAt);
assert.match(chart,/this\._frameSig=''/);
assert.match(chart,/stamp=/);
assert.match(chart,/alpha:false,desynchronized:true/);
assert.match(chart,/timeStride=\[1000,3000,5000,15000,30000/);
assert.match(chart,/priceLabels/);
assert.match(chart,/timeAxisTicks/);

const paint=readFileSync(new URL('../src/track-paint.js',import.meta.url),'utf8');
assert.match(paint,/new Lru\(24\)/);
assert.match(paint,/if\(geo===this\.geoSig\)/);
assert.match(paint,/this\.renderUi\(laid,top\)/);
assert.match(paint,/import \{Lru\} from '\.\/speed-pass\.js'/);

const workspace=readFileSync(new URL('../src/track-workspace.js',import.meta.url),'utf8');
assert.match(workspace,/import \{applyTip,chainKey,isTipChain,prewarmBuckets,pullFastFeed\} from '\.\/speed-pass\.js'/);
assert.match(workspace,/function chainKeyLabel/);
assert.match(workspace,/if\(!sample&&isTipChain\(chain\)\)scheduleTip\(\)/);
assert.match(workspace,/chart\.set\(\{interval:'1s'\}\)/);
assert.match(workspace,/preserve:true/);
assert.match(workspace,/solana-not-tip|isTipChain\(chain\)/);
assert.match(workspace,/Arbitrum tip · latency paint/);
assert.match(workspace,/Robinhood tip · latency paint/);
assert.match(workspace,/document\.hidden/);
assert.doesNotMatch(workspace,/Fast Feed is a research/);

const promote=readFileSync(new URL('../../scripts/promote-track-static.mjs',import.meta.url),'utf8');
for(const needle of ['latencyPaintOnly','binance_1s','/api/public/engine/fast-feed','solana-not-tip','Arbitrum tip','wickRejects','track-shell-QSAI55JM.js']){
 assert.equal(promote.includes(needle),true,needle);
}

const hot=Array.from({length:120},(_,i)=>({time:(i+1)*1000,open:1,high:1.2,low:.9,close:1.1,volume:1}));
const t0=performance.now();
for(let i=0;i<200;i++)mergeBars1s(hot,hot,{now:120_000});
const hotMs=performance.now()-t0;
const wide=Array.from({length:5000},(_,i)=>({time:(i+1)*1000,open:1,high:2,low:1,close:1,volume:1,source:'binance_1s'}));
const t1=performance.now();
for(let i=0;i<20;i++)mergeBars1s(wide,[],{now:5_000_000});
const wideMs=performance.now()-t1;
assert.ok(hotMs<250,`hot merge ${hotMs}`);
assert.ok(wideMs<250,`wide merge ${wideMs}`);
console.log(`speed-pass ok · 100 opts · hot 120x200 ${hotMs.toFixed(1)}ms · base 5kx20 ${wideMs.toFixed(1)}ms`);
