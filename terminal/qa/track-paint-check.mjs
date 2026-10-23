import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseTrackPaint,layoutTrack,replayFrames,panesForWallet,seriesFromPaint,TRACK_SCHEMA} from '../src/track-paint.js';
import {loadTrackPaint,parseWorkspaceHash,trackerRedirect} from '../src/track-workspace.js';

const handoff={
 state:'ready',route:'#/console/workspace/wallet:0x278d858f05b94576c1e6f73285886876ff6ef8d2',
 handoff:{schema:'ticking.handoff.v1',next:{request:{
  query:'where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2',
  track:true,listAddresses:false,
  chartMarkers:[
   {kind:'buy',time:1_700_000_000,wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',label:'buy',symbol:'WETH',chain:'arbitrum'},
   {kind:'hop',t:1_700_000_120,wallet:'0x51c72848c68a965f66fa7a88855f9f7784502a7f',label:'hop'},
   {kind:'sell',time:1_700_000_240_000,wallet:'0x51c72848c68a965f66fa7a88855f9f7784502a7f',symbol:'USDC',label:'sell'},
   {kind:'cex',time:1_700_000_300_000,wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',label:'Binance',chain:'ethereum'},
   {kind:'frozen',time:1_700_000_360_000,unknown:true},
  ],
  bands:[{t0:1_700_000_000_000,t1:1_700_000_240_000,wallets:['0x278d858f05b94576c1e6f73285886876ff6ef8d2','0x51c72848c68a965f66fa7a88855f9f7784502a7f'],label:'A→B'},{style:'dotted',src:'arb',dst:'eth',t0:1_700_000_300_000,t1:1_700_000_360_000}],
  sideColumns:[{chain:'ethereum',label:'ETH',dotted:true,markers:[{kind:'transfer',time:1_700_000_320_000,label:'transfer'}]}],
  museSummary:'Trail on the workspace chart.',
  muse:{beside:{cta:'Follow next hop',wallet:'0x51c72848c68a965f66fa7a88855f9f7784502a7f'},replay:true,replayMs:1500},
  addresses:['0x278d858f05b94576c1e6f73285886876ff6ef8d2','0x51c72848c68a965f66fa7a88855f9f7784502a7f'],
 }}},
};
const paint=parseTrackPaint(handoff);
assert.equal(paint.schema,TRACK_SCHEMA);
assert.equal(paint.listAddresses,false);
assert.equal(paint.suppressedAddressList,true);
assert.equal(JSON.stringify(paint).includes('addresses'),false);
assert.equal(paint.chartMarkers.length,5);
assert.equal(paint.chartMarkers[0].time,1_700_000_000_000);
assert.equal(paint.chartMarkers[3].solid,true);
assert.equal(paint.chartMarkers[3].dashed,false);
assert.equal(paint.chartMarkers[4].dashed,true);
assert.equal(paint.chartMarkers[4].solid,false);
assert.equal(paint.bands[1].dotted,true);
assert.equal(paint.bands[1].label,'arb→eth');
assert.equal(paint.sideColumns[0].chain,'ethereum');
assert.equal(paint.muse.replay,true);
assert.equal(paint.muse.replayMs,1500);
assert.equal(paint.muse.beside.cta,'Follow next hop');

const view={plotWidth:400,indexAt:t=>(t-1_700_000_000_000)/60_000,x:i=>(i+.5)*8};
const laid=layoutTrack(paint,view);
assert.ok(laid.bands.length>=1);
assert.ok(laid.tags.some(t=>t.kind==='buy'));
assert.ok(laid.chips.some(c=>c.kind==='hop'&&c.wallet.startsWith('0x51c7')));
assert.equal(laid.chips.find(c=>c.kind==='cex').solid,true);

const frames=replayFrames(paint);
assert.ok(frames.length>=2&&frames.length<=12);
assert.equal(frames[0].at,0);
assert.equal(frames.at(-1).at,1500);
assert.ok(frames.at(-1).time>frames[0].time);

const hop='0x51c72848c68a965f66fa7a88855f9f7784502a7f';
const panes=panesForWallet(paint,hop);
assert.equal(panes.length,1);
assert.equal(panes[0].symbol,'USDC');
assert.ok(panes[0].markers.some(m=>m.kind==='hop'));
assert.equal(panes[0].title.includes(hop),false);
const multi=parseTrackPaint({chartMarkers:[
 {kind:'buy',time:1_700_000_000_000,wallet:hop,symbol:'WETH',label:'buy'},
 {kind:'sell',time:1_700_000_060_000,wallet:hop,symbol:'ARB',label:'sell'},
],bands:[],sideColumns:[]});
assert.equal(panesForWallet(multi,hop).length,2);

const route=parseWorkspaceHash('#/console/workspace/wallet:0x278d858f05b94576c1e6f73285886876ff6ef8d2?chain=arbitrum');
assert.equal(route.incident,'wallet:0x278d858f05b94576c1e6f73285886876ff6ef8d2');
assert.equal(route.chain,'arbitrum');
assert.equal(trackerRedirect('#/tracker'),'');
assert.equal(trackerRedirect('#/tracker?address=0x278d858f05b94576c1e6f73285886876ff6ef8d2&chain=arbitrum'),'#/console/workspace/wallet%3A0x278d858f05b94576c1e6f73285886876ff6ef8d2?chain=arbitrum');
assert.equal(trackerRedirect('#/terminal/SOL'),null);
const live=await loadTrackPaint({wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',chain:'arbitrum',fetchImpl:async(url,opts)=>{
 assert.equal(url,'/api/public/intelligence/resolve');
 assert.match(opts.body,/where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2 on arbitrum/);
 return {ok:true,json:async()=>handoff};
}});
assert.equal(live.source,'resolve');
assert.equal(live.paint.chartMarkers.length,5);
assert.equal(live.paint.listAddresses,false);
const kept=await loadTrackPaint({wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',handoff,fetchImpl:async()=>({ok:false,status:503})});
assert.equal(kept.source,'handoff');
assert.equal(kept.paint.bands.length,2);
assert.ok(seriesFromPaint(paint).every(b=>b.synthetic&&b.volume===0));
assert.equal(seriesFromPaint(parseTrackPaint({chartMarkers:[],bands:[],sideColumns:[]})).length,0);

const root=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const built=spawnSync(process.execPath,[resolve(root,'scripts/hash-track-build.mjs')],{cwd:root,encoding:'utf8'});
assert.equal(built.status,0,built.stderr);
const manifest=JSON.parse(readFileSync(resolve(root,'terminal/track-build.json'),'utf8'));
assert.equal(manifest.schema,'ticking.track-build.v1');
assert.match(manifest.paint,/\/terminal\/dist\/_app\/track-paint-[A-Z2-7]{8}\.js/);
assert.match(manifest.workspace,/\/terminal\/dist\/_app\/track-workspace-[A-Z2-7]{8}\.js/);
const hashed=readFileSync(resolve(root,manifest.workspace.slice(1)),'utf8');
assert.match(hashed,/track-paint-[A-Z2-7]{8}\.js/);
assert.doesNotMatch(hashed,/from '\.\/track-paint\.js'/);
console.log(JSON.stringify({passed:true,markers:paint.chartMarkers.length,bands:paint.bands.length,replaySteps:frames.length,paint:manifest.paint,workspace:manifest.workspace}));
