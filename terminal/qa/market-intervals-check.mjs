// Focused interval and identity contracts for the exported SDK.
import assert from 'node:assert/strict';
import {MARKET_INTERVALS,parseInterval,aggregateCandles} from '../src/intervals.js';
import {createDataFeed} from '../sdk/data-feed.js';

for(const tf of MARKET_INTERVALS)assert.ok(parseInterval(tf).seconds>=60&&parseInterval(tf).seconds<=604800);
assert.equal(parseInterval('7d').label,'1w');assert.equal(parseInterval('1W').milliseconds,604800000);
assert.throws(()=>parseInterval('2w'));
const monday=Date.parse('2026-10-05T00:00:00Z'),sunday=monday-60000;
const rows=[{time:sunday,open:10,high:10,low:10,close:10,volume:10,baseVolume:1,count:1},{time:monday,open:12,high:12,low:12,close:12,volume:12,baseVolume:1,count:1},{time:monday+43200000,open:8,high:8,low:8,close:8,volume:8,baseVolume:1,count:1}];
const bars=aggregateCandles(rows,604800000);
assert.equal(bars.length,2);assert.equal(bars[0].time,monday-604800000);assert.deepEqual(bars[1],{time:monday,open:12,high:12,low:8,close:8,volume:20,baseVolume:2,count:2,synthetic:false});
const data={mode:'executed_history',identity:{network:'solana'},pool:'pool',interval:'1w',fillPolicy:'recorded',candles:[bars[1]]};
globalThis.fetch=async()=>new Response(JSON.stringify(data),{status:200});
const feed=createDataFeed();assert.equal((await feed.history('1w')).candles[0].time,monday);
data.candles=[{...bars[1],time:Math.floor(monday/604800000)*604800000}];
await assert.rejects(()=>feed.history('1w'),/Invalid candle sequence/);feed.destroy();
console.log(JSON.stringify({passed:true,marketIntervals:MARKET_INTERVALS,weekStartsMonday:true,canonicalWeeklySDKValidation:true}));

const tokenData={...data,aggregation:'token',pool:null,mint:'mint',quoteMint:'native-quote',identity:{network:'solana',baseMint:'mint',quoteMint:'native-quote',series:'reference',aggregation:'v3'},candles:[bars[1]]};
let lastURL;globalThis.fetch=async url=>{lastURL=String(url);return new Response(JSON.stringify(tokenData),{status:200});};
const referenceFeed=createDataFeed({mint:'mint',aggregation:'token'});await referenceFeed.history('1w');assert.match(lastURL,/aggregation=token/);
tokenData.identity.quoteMint='other';assert.equal((await referenceFeed.history('1w')).identityReset,true);assert.equal((await referenceFeed.history('1w')).identityReset,false);referenceFeed.destroy();
assert.throws(()=>createDataFeed({mint:'mint',pool:'pool',aggregation:'token'}));
console.log(JSON.stringify({tokenReferenceValidation:true,quoteChangeRequiresRebase:true}));
