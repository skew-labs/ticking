import assert from 'node:assert/strict';
import {createDataFeed,bindDataFeed} from '../sdk/data-feed.js';
globalThis.location={origin:'https://fixture.test'};
globalThis.document=new EventTarget();document.hidden=false;
const mint='So11111111111111111111111111111111111111112';
const quality={basis:'estimated',observedMinutes:0,referenceMinutes:0,estimatedMinutes:1,carriedMinutes:0};
const bar={time:1800000000000,sourceTime:1800000000000,open:10,high:11,low:10,close:11,volume:0,baseVolume:0,count:0,estimated:true,referenceOnly:true,quality};
let response={mode:'asset_history',aggregation:'token',pool:null,mint,interval:'1m',candles:[bar],identity:{network:'solana',baseMint:mint,quoteMint:'q',series:'reference',aggregation:'v1',historyPolicy:'h'}};
globalThis.fetch=async()=>new Response(JSON.stringify(response));const feed=createDataFeed({mint,aggregation:'token'});
await feed.history();
for(const invalid of [{...bar,volume:1},{...bar,count:1},{...bar,quality:{...quality,basis:'observed'}},{...bar,quality:{...quality,estimatedMinutes:1.5}},{...bar,quality:{...quality,estimatedMinutes:2}},{...bar,estimated:false}]){
 response={...response,candles:[invalid]};await assert.rejects(feed.history(),/provenance|execution volume/);
}
response={...response,interval:'5m',candles:[{...bar,volume:4,baseVolume:4,count:2,referenceOnly:false,quality:{...quality,basis:'mixed',observedMinutes:1,estimatedMinutes:4}}]};await feed.history('5m');feed.destroy();
const root=new EventTarget(),container=new EventTarget();container.closest=()=>root;let paints=0;
const chart={container,options:{interval:'5m',series:[]},view:{period:300000},offset:0,token:{},setSeries(b){this.options.series=b;paints++;},set(){},requestDraw(){}};
const fake={async history(){return structuredClone(response)},cancelPending(){},destroy(){}};
const dispose=bindDataFeed(chart,{feed:fake,refreshMs:60000});await new Promise(r=>setTimeout(r,5));
const previous=paints;document.dispatchEvent(new Event('visibilitychange'));await new Promise(r=>setTimeout(r,5));assert.equal(paints,previous);
response.candles[0].receipt='corrected-receipt';document.dispatchEvent(new Event('visibilitychange'));await new Promise(r=>setTimeout(r,5));assert.equal(paints,previous+1);
response.candles[0].quality={...response.candles[0].quality,observedMinutes:2,estimatedMinutes:3};document.dispatchEvent(new Event('visibilitychange'));await new Promise(r=>setTimeout(r,5));assert.equal(paints,previous+2);
dispose();console.log(JSON.stringify({passed:true,malformedProvenanceRejected:6,mixedRecordedVolumeAccepted:true,unchangedAvoidsPaint:true,receiptAndQualityCorrectionsRedraw:true}));
