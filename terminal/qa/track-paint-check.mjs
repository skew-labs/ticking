import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseTrackPaint,layoutTrack,replayFrames,panesForWallet,seriesFromPaint,columnForBridge,museLine,isSharp,sampleTrackPaint,TRACK_SCHEMA} from '../src/track-paint.js';
import {loadTrackPaint,parseWorkspaceHash,trackerRedirect,shouldOpenWallet,trackRouteFromResolve,acceptTrackHandoff} from '../src/track-workspace.js';
import {classifyTrackIntent,guardedResolveBody,trackResolveBody,TRACK_GUARD_TEXT} from '../src/track-intent.js';

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
assert.equal(laid.tags.find(t=>t.kind==='buy').clock,'+0m');
assert.equal(laid.tags.find(t=>t.kind==='buy').sharp,false);
const near=layoutTrack(parseTrackPaint({chartMarkers:[
 {kind:'buy',time:1_700_000_000_000,label:'buy'},
 {kind:'sell',time:1_700_000_030_000,label:'sell'},
]}),{plotWidth:800,indexAt:t=>(t-1_700_000_000_000)/1000,x:i=>i});
assert.equal(near.tags.length,2);
assert.notEqual(near.tags[0].lane,near.tags[1].lane);
const far=layoutTrack(parseTrackPaint({chartMarkers:[
 {kind:'buy',time:1_700_000_000_000,label:'buy'},
 {kind:'sell',time:1_700_000_200_000,label:'sell'},
]}),{plotWidth:800,indexAt:t=>(t-1_700_000_000_000)/1000,x:i=>i});
assert.equal(far.tags[0].lane,far.tags[1].lane);
assert.equal(far.tags[1].clock,'+3m');

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
const dump=parseTrackPaint({mode:'dump',chartMarkers:[
 {kind:'sell',time:1_700_000_000_000,label:'sell'},
 {kind:'suspect',time:1_700_000_060_000,label:'suspect'},
 {kind:'dump',time:1_700_000_120_000,label:'dump'},
]});
assert.equal(dump.chartMarkers.length,2);
assert.ok(dump.chartMarkers.every(m=>m.kind==='dump'&&m.chip===false&&isSharp(m)));
const whale=parseTrackPaint({chartMarkers:[{kind:'tag',time:1_700_000_000_000,label:'whale',wallet:hop}]});
assert.equal(whale.chartMarkers[0].chip,false);
assert.equal(isSharp(whale.chartMarkers[0]),true);
const sample=sampleTrackPaint();
assert.equal(sample.chartMarkers.find(m=>m.kind==='dump').chip,false);
assert.equal(isSharp(sample.chartMarkers.find(m=>m.label==='whale')),true);
assert.equal(sample.chartMarkers.find(m=>m.label==='whale').chip,false);
assert.ok(sample.chartMarkers.some(m=>m.kind==='hop'&&m.chip));
assert.ok(sample.chartMarkers.some(m=>m.kind==='bridge'&&m.chip));
assert.equal(sample.listAddresses,false);
assert.equal(parseTrackPaint({chartMarkers:[{kind:'suspect',time:1_700_000_000_000,label:'suspect'}]}).chartMarkers[0].kind,'dump');
const bridgeCol=columnForBridge(paint,{dst:'eth'});
assert.equal(bridgeCol.chain,'ethereum');
assert.equal(bridgeCol.markers.length,1);
assert.equal(museLine('Paid 0x278d858f05b94576c1e6f73285886876ff6ef8d2 then left'),'Paid a wallet then left');
const quick=replayFrames(parseTrackPaint({chartMarkers:[{kind:'buy',time:1_700_000_000_000,label:'buy'},{kind:'sell',time:1_700_000_060_000,label:'sell'}],muse:{replay:true}}));
assert.equal(quick.at(-1).at,900);
assert.ok(quick.length<=8);
assert.equal(shouldOpenWallet('0x278d858f05b94576c1e6f73285886876ff6ef8d2',{address:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',kind:'cex'}),false);
assert.equal(shouldOpenWallet('0x278d858f05b94576c1e6f73285886876ff6ef8d2',{address:hop,kind:'hop'}),true);
assert.equal(shouldOpenWallet('0x278d858f05b94576c1e6f73285886876ff6ef8d2',{address:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',kind:'bridge',dst:'eth'}),false);
const trackIntent=classifyTrackIntent('where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2');
assert.equal(trackIntent.mode,'track');
assert.equal(trackIntent.listAddresses,false);
assert.equal(trackIntent.perceive,'track');
assert.equal(trackIntent.act,'chartMarkers');
assert.equal(trackIntent.route.startsWith('#/console/workspace/wallet%3A'),true);
assert.equal(classifyTrackIntent('suspect dump 0xd8da6bf26964af9d7eed9e03e53415d37aa96045').mode,'dump');
assert.equal(classifyTrackIntent('suspect dump 0xd8da6bf26964af9d7eed9e03e53415d37aa96045').dumpTagsOnly,true);
const pump=classifyTrackIntent('PUMP');
assert.equal(pump.mode,'market');
assert.equal(pump.route,'#/console/workspace/PUMP');
assert.equal(pump.act,'chart');
assert.equal(classifyTrackIntent('PUMP chart').route,'#/console/workspace/PUMP');
assert.equal(classifyTrackIntent('SOL').route,'#/console/workspace/SOL');
assert.equal(classifyTrackIntent('arbitrum chart').symbol,'ARB');
assert.equal(classifyTrackIntent('market of SOL').route,'#/console/workspace/SOL');
assert.equal(classifyTrackIntent('RH').mode,'other');
assert.equal(classifyTrackIntent('fast feed').mode,'other');
assert.equal(classifyTrackIntent('fast feed').act,'none');
const fed=classifyTrackIntent('fast feed 0xd8da6bf26964af9d7eed9e03e53415d37aa96045');
assert.equal(fed.mode,'track');
assert.equal(fed.route.startsWith('#/console/workspace/wallet%3A'),true);
const mapped=classifyTrackIntent('map this wallet 0xd8da6bf26964af9d7eed9e03e53415d37aa96045');
assert.equal(mapped.mode,'track');
assert.equal(mapped.route.includes('investigate'),false);
assert.equal(mapped.route.startsWith('#/console/workspace/wallet%3A'),true);
assert.equal(classifyTrackIntent('flow of funds from 0xd8da6bf26964af9d7eed9e03e53415d37aa96045').mode,'track');
assert.equal(classifyTrackIntent('whale trail 0xd8da6bf26964af9d7eed9e03e53415d37aa96045').mode,'track');
const hinted=classifyTrackIntent('track wallet 0x278d858f05b94576c1e6f73285886876ff6ef8d2 on robinhood and solana');
assert.equal(hinted.chain,'robinhood');
assert.equal(hinted.route.endsWith('?chain=robinhood'),true);
assert.equal(classifyTrackIntent('map').route,'#/console/workspace/auto?q=map');
assert.equal(classifyTrackIntent('flow').route,'#/console/workspace/auto?q=flow');
assert.equal(trackIntent.forbid.includes('investigate'),true);
assert.equal(trackResolveBody({query:'SOL'}).track,false);
assert.equal(trackResolveBody({query:'SOL'}).mode,'market');
assert.equal(guardedResolveBody({query:'SOL'}),null);
assert.equal(guardedResolveBody({query:'where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2'}).track,true);
assert.equal(trackResolveBody({query:trackIntent.route&&'where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2'}).guard.text,TRACK_GUARD_TEXT);
const routed=trackRouteFromResolve({state:'ready',route:'#/console/workspace/wallet:0x278d858f05b94576c1e6f73285886876ff6ef8d2',searchGuard:'search-guard-7+track',message:'Opening workspace chart to paint the trail.',handoff:{next:{request:{track:true,listAddresses:false,chartMarkers:[]}}}});
assert.equal(routed,'#/console/workspace/wallet:0x278d858f05b94576c1e6f73285886876ff6ef8d2');
assert.equal(acceptTrackHandoff({state:'ready',title:'Markets'}),null);
const live=await loadTrackPaint({wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',chain:'arbitrum',fetchImpl:async(url,opts)=>{
 assert.equal(url,'/api/public/intelligence/resolve');
 assert.match(opts.body,/where did the money go from 0x278d858f05b94576c1e6f73285886876ff6ef8d2 on arbitrum/);
 const sent=JSON.parse(opts.body);
 assert.equal(sent.listAddresses,false);
 assert.equal(sent.mode,'track');
 assert.equal(sent.guard.perceive,'track');
 assert.equal(sent.guard.act,'chartMarkers');
 return {ok:true,json:async()=>handoff};
}});
assert.equal(live.source,'resolve');
assert.equal(live.paint.chartMarkers.length,5);
assert.equal(live.paint.listAddresses,false);
const kept=await loadTrackPaint({wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',handoff,fetchImpl:async()=>({ok:false,status:503})});
assert.equal(kept.source,'handoff');
assert.equal(kept.paint.bands.length,2);
const emptyLive=await loadTrackPaint({wallet:'0x278d858f05b94576c1e6f73285886876ff6ef8d2',handoff,fetchImpl:async()=>({ok:true,json:async()=>({state:'ready',message:'Opening workspace chart to paint the trail.',searchGuard:'search-guard-7+track',handoff:{next:{request:{track:true,listAddresses:false,chartMarkers:[],bands:[],sideColumns:[]}}}})})});
assert.equal(emptyLive.source,'handoff');
assert.equal(emptyLive.paint.chartMarkers.length,5);
const told=await loadTrackPaint({wallet:'0x1111111111111111111111111111111111111111',fetchImpl:async()=>({ok:true,json:async()=>({message:'Opening workspace chart to paint the trail.',handoff:{next:{request:{track:true,listAddresses:false,chartMarkers:[]}}}})})});
assert.match(told.paint.muse.summary,/Opening workspace chart/);
assert.ok(seriesFromPaint(paint).every(b=>b.synthetic&&b.volume===0));
assert.equal(seriesFromPaint(parseTrackPaint({chartMarkers:[],bands:[],sideColumns:[]})).length,0);
const buyT=1_700_000_060_000,dumpT=1_700_000_180_000;
const sil=seriesFromPaint(parseTrackPaint({chartMarkers:[{kind:'buy',time:buyT,label:'buy'},{kind:'dump',time:dumpT,label:'dump'}]}));
const bucket=t=>Math.floor(t/60_000)*60_000;
const buyBar=sil.find(b=>b.time===bucket(buyT));
const dumpBar=sil.find(b=>b.time===bucket(dumpT));
assert.ok(buyBar&&dumpBar&&dumpBar.close<buyBar.close);
assert.ok(sil.every(b=>b.synthetic===true&&b.volume===0&&b.close>=0.2));

const root=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const paintSrc=readFileSync(resolve(root,'terminal/src/track-paint.js'),'utf8');
assert.match(paintSrc,/data-track-focus=/);
assert.match(paintSrc,/data-track-focus-wallet/);
const workspaceSrc=readFileSync(resolve(root,'terminal/src/track-workspace.js'),'utf8');
assert.match(workspaceSrc,/Replay trail/);
assert.match(workspaceSrc,/Same case · wallet chart/);
assert.match(workspaceSrc,/Same case · chain chart/);
assert.match(workspaceSrc,/data-track-caption/);
assert.match(workspaceSrc,/Beside this chart/);
assert.doesNotMatch(workspaceSrc,/address-list/);

const built=spawnSync(process.execPath,[resolve(root,'scripts/hash-track-build.mjs')],{cwd:root,encoding:'utf8'});
assert.equal(built.status,0,built.stderr);
const manifest=JSON.parse(readFileSync(resolve(root,'terminal/track-build.json'),'utf8'));
assert.equal(manifest.schema,'ticking.track-build.v1');
assert.match(manifest.paint,/\/terminal\/dist\/_app\/track-paint-[A-Z2-7]{8}\.js/);
assert.match(manifest.workspace,/\/terminal\/dist\/_app\/track-workspace-[A-Z2-7]{8}\.js/);
const hashed=readFileSync(resolve(root,manifest.workspace.slice(1)),'utf8');
assert.match(hashed,/track-paint-[A-Z2-7]{8}\.js/);
assert.match(hashed,/track-intent-[A-Z2-7]{8}\.js/);
assert.doesNotMatch(hashed,/from '\.\/track-paint\.js'/);
assert.doesNotMatch(hashed,/from '\.\/track-intent\.js'/);
assert.match(hashed,/setTrackPaint/);
assert.match(hashed,/data-track-form/);
assert.doesNotMatch(hashed,/whale-tracker/);
console.log(JSON.stringify({passed:true,markers:paint.chartMarkers.length,bands:paint.bands.length,replaySteps:frames.length,paint:manifest.paint,workspace:manifest.workspace}));
