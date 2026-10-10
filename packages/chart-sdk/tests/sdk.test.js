import test from 'node:test';import assert from 'node:assert/strict';
import {calculate,STUDIES,sma,ema} from '../indicators.js';
import {CandleBuffer,DrawingBook,validateLayout} from '../model.js';
const mint='EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm';
const candles=values=>values.map((v,i)=>({time:1710028800+i*60,open:v,high:v+1,low:Math.max(.1,v-1),close:v,volume:2*v,baseVolume:2}));
const linear=candles(Array.from({length:80},(_,i)=>i+1));
const near=(v,e)=>assert.ok(Math.abs(v-e)<1e-8,`${v} != ${e}`);
test('SMA/EMA seed, warmup and recurrence',()=>{assert.deepEqual(sma([1,2,3,4],3),[null,null,2,3]);assert.deepEqual(ema([1,2,3,4],3),[null,null,2,3]);near(calculate(linear,'sma').lines[0].values[19],10.5);near(calculate(linear,'ema').lines[0].values[20],11.5)});
test('Weighted average assigns newest greatest weight',()=>near(calculate(linear,'wma').lines[0].values[19],13.666666666666666));
test('Bollinger population variance fixture',()=>{const r=calculate(linear,'bb');near(r.lines[0].values[19],10.5);near(r.lines[1].values[19],10.5+2*Math.sqrt(33.25));near(r.lines[2].values[19],10.5-2*Math.sqrt(33.25))});
test('Wilder RSI published worksheet',()=>{const values=[44.34,44.09,44.15,43.61,44.33,44.83,45.1,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28];near(calculate(candles(values),'rsi').lines[0].values[14],70.46413502109705)});
test('VWAP resets at UTC boundary, uses base volume',()=>{const c=candles([10,20,30]);c[0].time=1710028740;c[1].time=1710028800;c[2].time=1710028860;const v=calculate(c,'vwap').lines[0].values;near(v[0],10);near(v[1],20);near(v[2],25)});
test('ATR and stochastic deterministic ranges',()=>{const c=candles(Array.from({length:30},()=>10));near(calculate(c,'atr').lines[0].values[13],2);near(calculate(c,'stoch').lines[0].values[13],50);near(calculate(c,'stoch').lines[1].values[15],50)});
test('MACD EMA spread and signal on linear price',()=>{const r=calculate(linear,'macd');near(r.lines[0].values[25],7);near(r.lines[1].values[33],7);near(r.lines[2].values[33],0)});
test('OBV, CCI and ROC fixtures',()=>{assert.deepEqual(calculate(candles([10,11,9,9]),'obv').lines[0].values,[0,2,0,0]);near(calculate(candles(Array(25).fill(10)),'cci').lines[0].values[19],0);near(calculate(linear,'roc').lines[0].values[12],1200)});
test('All twelve studies have finite values or explicit warmup',()=>{assert.equal(STUDIES.length,12);for(const s of STUDIES)for(const l of calculate(linear,s.id).lines){assert.equal(l.values.length,80);assert.ok(l.values.every(v=>v==null||Number.isFinite(v)))}});
test('Bounded candle buffer deduplicates, anchors old view and rejects malformed data',()=>{const b=new CandleBuffer(100);b.merge(linear);b.merge(linear);assert.equal(b.data.length,80);b.merge(candles([1]).map(c=>({...c,high:0})));assert.equal(b.data.length,80);const more=candles(Array.from({length:500},(_,i)=>i+1));b.merge(more,more[100].time);assert.equal(b.data.length,100);assert.ok(b.data.some(c=>c.time===more[100].time));assert.ok(b.view(more[100].time,30).candles.length<=30)});
test('Drawing edit/lock/undo/redo round trip',()=>{const b=new DrawingBook();const d={id:'d',type:'trend',points:[{time:1710028800,price:1},{time:1710028860,price:2}]};b.add(d);b.edit('d',{locked:true});b.remove('d');assert.equal(b.items.length,1);b.edit('d',{points:[{time:1710028800,price:7},{time:1710028860,price:8}]});assert.equal(b.items[0].points[0].price,1);b.edit('d',{locked:false});b.remove('d');assert.equal(b.items.length,0);b.undo();assert.equal(b.items.length,1);b.redo();assert.equal(b.items.length,0)});
test('Saved layout validates and strips private overlays',()=>{const l=validateLayout({version:1,mint,positions:[1],panes:[{interval:5,studies:['ema'],drawings:[],orders:[1]}]});assert.equal(l.panes[0].interval,5);assert.equal(l.positions,undefined);assert.equal(l.panes[0].orders,undefined);assert.throws(()=>validateLayout({...l,panes:Array(5).fill(l.panes[0])}))});

test('browser license validates signature, origin, expiry and capped charts',async()=>{
  const {verifyLicense}=await import('../license.js');const {generateKeyPairSync,sign}=await import('node:crypto');const {privateKey,publicKey}=generateKeyPairSync('ed25519');const key=publicKey.export({format:'der',type:'spki'}).subarray(-32),kid='test-key',now=Date.now()/1000;
  const pub={algorithm:'Ed25519',key:key.toString('base64url'),kid};const claims={iss:'mingle-chart-sdk',aud:'chart-embed',project:'a'.repeat(24),origin:'https://exchange.example',plan:'trial',features:['studies'],maxCharts:4,iat:now,exp:now+3600};
  const encode=payload=>{const h=Buffer.from(JSON.stringify({alg:'EdDSA',typ:'JWT',kid})).toString('base64url'),p=Buffer.from(JSON.stringify(payload)).toString('base64url');return h+'.'+p+'.'+sign(null,Buffer.from(h+'.'+p),privateKey).toString('base64url')};
  const token=encode(claims);assert.equal((await verifyLicense(token,pub,claims.origin,now)).maxCharts,4);
  await assert.rejects(verifyLicense(token,pub,'https://other.example',now));await assert.rejects(verifyLicense(token,pub,claims.origin,now+4000));await assert.rejects(verifyLicense(encode({...claims,maxCharts:8}),pub,claims.origin,now));await assert.rejects(verifyLicense(token.slice(0,20)+'A'+token.slice(21),pub,claims.origin,now));
});
test('shared data recovery never forwards an older candle over a live update',async()=>{
  const {MarketDataHub}=await import('../data.js');const hub=Object.create(MarketDataHub.prototype),channel={data:new Map(),versions:new Map(),sequence:0};
  const older={time:60,open:1,high:2,low:1,close:1,volume:2},newer={...older,close:2,volume:4};
  assert.equal(hub.merge(channel,[newer],8).length,1);assert.equal(hub.merge(channel,[older],4).length,0);assert.equal(channel.data.get(60).close,2);assert.equal(channel.sequence,8);
});
test('external exchange layout supports distinct market series and strips account data',async()=>{
  const {validateSDKLayout}=await import('../model.js');const pane={interval:1,studies:['ema'],drawings:[],orders:[{price:140}],positions:[{entry:140}]};const layout=validateSDKLayout({version:1,market:'MINGLE:SOL-USD-PERP',series:'mark',panes:[pane]});assert.equal(layout.market,'MINGLE:SOL-USD-PERP');assert.equal(layout.series,'mark');assert.equal(layout.panes[0].orders,undefined);assert.equal(layout.panes[0].positions,undefined);assert.throws(()=>validateSDKLayout({...layout,series:'liquidation'}));
});
test('SDK themes are isolated and reject malformed canvas colors',async()=>{
  const {validateTheme,DARK_THEME,LIGHT_THEME}=await import('../theme.js');const theme=validateTheme({...DARK_THEME,up:'#00aa88'});assert.equal(theme.up,'#00aa88');assert.equal(LIGHT_THEME.background,'#ffffff');assert.throws(()=>validateTheme({background:'url(javascript:alert(1))'}));assert.throws(()=>validateTheme({fontFamily:'bad\nfont'}));
});
test('server license broker coalesces requests and never returns the server key',async()=>{
  const {createLicenseBroker}=await import('../server.js');let calls=0;const key='mgl_test-fixture-key';
  const broker=createLicenseBroker({endpoint:'https://sdk.example/sdk/v1/licenses',apiKey:key,origin:'https://exchange.example',fetcher:async(url,options)=>{calls++;assert.equal(options.headers.Authorization,'Bearer '+key);assert.equal(JSON.parse(options.body).origin,'https://exchange.example');return {ok:true,json:async()=>({license:'public-browser-token',expiresAt:Date.now()/1000+3600})}}});
  const results=await Promise.all(Array.from({length:100},()=>broker.license()));assert.equal(calls,1);assert.deepEqual(results[0],{license:'public-browser-token'});assert.equal(JSON.stringify(results).includes(key),false);broker.clear();await broker.license();assert.equal(calls,2);assert.throws(()=>createLicenseBroker({endpoint:'http://untrusted.example',apiKey:key,origin:'https://exchange.example'}));
});

function adapterEngine(){
  const listeners=new Map(),buffer=new CandleBuffer(100),errors=[];
  return {alive:true,buffer,options:{},errors,on(type,callback){listeners.set(type,callback);return ()=>listeners.delete(type)},emit(type,value){if(type==='error')errors.push(value);listeners.get(type)?.(value)},setData(c,quote){this.quote=quote;buffer.merge(c)},update(c){buffer.merge(c)},removeCandles(times){buffer.remove(times)},destroy(){this.alive=false;this.emit('destroy')}};
}
test('historical fork invalidation discards loaded pages and fences their in-flight responses',async()=>{
 const {connectData}=await import('../client.js');const engine=adapterEngine(),bars=candles([2,3]);let callback,resolve;
 const adapter={identity:{quoteSymbol:'USD'},history:async before=>before?new Promise(r=>resolve=r):{sequence:2,candles:bars,hasMore:true},subscribe(fn){callback=fn;return ()=>{}}};
 const stop=await connectData(engine,adapter);const pending=engine.options.loadOlder(bars[0].time);
 callback({sequence:3,candles:[bars[1]],historyReset:true});assert.deepEqual(engine.buffer.data.map(c=>c.time),[bars[1].time]);
 resolve({sequence:2,candles:[bars[0]],hasMore:false});await pending;assert.deepEqual(engine.buffer.data.map(c=>c.time),[bars[1].time]);stop();
});
test('SDK adapter replays from history cursor and rejects stale overlapping pages',async()=>{
  const {connectData}=await import('../client.js');const engine=adapterEngine(),first=candles([1])[0];let callback,resolveOlder,stopped=0;
  const adapter={identity:{quoteSymbol:'USD'},history:async before=>before?new Promise(resolve=>resolveOlder=resolve):{candles:[first],sequence:4,hasMore:true},subscribe(fn,options){callback=fn;assert.equal(options.afterSequence,4);return ()=>stopped++}};
  const disconnect=await connectData(engine,adapter),older=engine.options.loadOlder(first.time);
  callback({sequence:8,candles:[{...first,high:2,close:2,volume:4}]});resolveOlder({sequence:5,candles:[{...first,volume:3}],hasMore:false});await older;
  assert.equal(engine.buffer.data[0].close,2);assert.equal(engine.buffer.data[0].volume,4);callback({sequence:7,candles:[first]});assert.equal(engine.buffer.data[0].close,2);
  engine.destroy();disconnect();assert.equal(stopped,1);assert.equal(engine.options.recover,undefined);
});
test('SDK adapter coalesces recovery, surfaces failures and prevents stale recovery',async()=>{
  const {connectData}=await import('../client.js');const engine=adapterEngine(),first=candles([1])[0];let callback,gap,resolveRecovery,calls=0;
  const adapter={identity:{quoteSymbol:'USD'},history:async(before,limit,options)=>{calls++;if(!options)return {candles:[first],sequence:4,hasMore:true};assert.equal(options.minSequence,8);return new Promise(resolve=>resolveRecovery=resolve)},subscribe(fn,options){callback=fn;gap=options.onGap;return ()=>{}}};
  const disconnect=await connectData(engine,adapter);callback({sequence:8,candles:[{...first,high:2,close:2}]});const a=gap(),b=gap();assert.equal(a,b);assert.equal(calls,2);resolveRecovery({sequence:5,candles:[first]});assert.equal(await a,false);assert.equal(engine.buffer.data[0].close,2);assert.match(engine.errors[0],/older than the stream cursor/);disconnect();
});
test('SDK adapter destroyed during initial history never subscribes',async()=>{
  const {connectData}=await import('../client.js');const engine=adapterEngine();let resolvePage,subscribed=false;
  const pending=connectData(engine,{identity:{quoteSymbol:'USD'},history:()=>new Promise(resolve=>resolvePage=resolve),subscribe(){subscribed=true;return ()=>{}}});engine.destroy();resolvePage({sequence:4,candles:candles([1]),hasMore:false});const disconnect=await pending;disconnect();assert.equal(subscribed,false);assert.equal(engine.buffer.data.length,0);
});
test('Engine keeps one indicator job in flight and computes only the latest burst state',async()=>{
  const {ChartEngine}=await import('../engine.js');const previous=globalThis.document;globalThis.document={hidden:false};let calls=0,resolveJob,latestPrice;
  const engine={job:0,alive:true,buffer:{data:candles([1])},studies:['ema'],options:{indicatorWorker:{request(data){calls++;latestPrice=data[0].close;return new Promise(resolve=>resolveJob=resolve)}}},invalidate(){},emit(){},compute:ChartEngine.prototype.compute};
  try{engine.compute();engine.buffer.data=candles([2]);for(let i=0;i<1000;i++)engine.compute();assert.equal(calls,1);resolveJob(['old']);await new Promise(setImmediate);assert.equal(calls,2);assert.equal(latestPrice,2);assert.equal(engine.results,undefined);resolveJob(['latest']);await new Promise(setImmediate);assert.deepEqual(engine.results,['latest']);assert.equal(calls,2)}finally{if(previous===undefined)delete globalThis.document;else globalThis.document=previous}
});

test('Identical candles and discarded out-of-window ticks keep the buffer revision unchanged',()=>{
  const b=new CandleBuffer(30);b.merge(linear,linear[20].time);const revision=b.revision,data=b.data;
  b.merge(data.map(c=>({...c})),linear[20].time);assert.equal(b.revision,revision);assert.equal(b.data,data);
  b.merge([{...linear.at(-1),time:linear.at(-1).time+60}],linear[20].time);assert.equal(b.revision,revision);
  const first={...data[0],baseVolume:4};b.merge([first],linear[20].time);assert.equal(b.revision,revision+1);assert.equal(b.data[0].baseVolume,4);
});
async function renderHarness(run){
  const {ChartEngine}=await import('../engine.js');
  const names=['document','requestAnimationFrame','cancelAnimationFrame','devicePixelRatio'],previous=new Map(names.map(n=>[n,globalThis[n]]));const queue=new Map();let id=0;
  globalThis.document={hidden:false};globalThis.devicePixelRatio=1;globalThis.requestAnimationFrame=callback=>{queue.set(++id,callback);return id};globalThis.cancelAnimationFrame=id=>queue.delete(id);
  const engine=Object.assign(Object.create(ChartEngine.prototype),{alive:true,buffer:new CandleBuffer(100),view:{bars:20,rightTime:linear[39].time},results:[],studies:[],quote:'USD',options:{},job:0,orders:[],book:new DrawingBook(),listeners:new Map(),empty:{textContent:''},legend:{textContent:''},host:{dataset:{},getBoundingClientRect:()=>({width:600,height:360})},renderer:{metrics:{baseFrames:0,annotationFrames:0,crosshairFrames:0,baseMs:[],annotationMs:[]},resize(){},scene(candles,start){return {candles,start}},base(){this.metrics.baseFrames++},annotations(){this.metrics.annotationFrames++},crosshair(){this.metrics.crosshairFrames++}}});
  const flush=()=>{let rounds=0;while(queue.size){if(++rounds>10)throw Error('Unexpected permanent animation loop');const tasks=[...queue.values()];queue.clear();for(const task of tasks)task()}};
  try{await run(engine,flush,queue)}finally{for(const name of names)previous.get(name)===undefined?delete globalThis[name]:globalThis[name]=previous.get(name)}
}
test('Idle charts, identical data and changes outside the viewport do not repaint candles',()=>renderHarness((engine,flush,queue)=>{
  engine.setData(linear);flush();assert.equal(engine.renderer.metrics.baseFrames,1);assert.equal(queue.size,0);const job=engine.job;
  for(let i=0;i<1000;i++)engine.setData(linear.map(c=>({...c})));assert.equal(queue.size,0);assert.equal(engine.job,job);
  engine.update([{...linear[79],volume:900}]);flush();assert.equal(engine.renderer.metrics.baseFrames,1);
  engine.update([{...linear[30],close:linear[30].close+.5}]);flush();assert.equal(engine.renderer.metrics.baseFrames,2);assert.equal(queue.size,0);
}));
test('Public auction bursts repaint only annotations, coalesce frames and never enter saved layouts',()=>renderHarness((engine,flush,queue)=>{
  engine.setData(linear);flush();const before={...engine.renderer.metrics};
  for(let i=0;i<1000;i++)engine.setMarketOverlays([{price:30+i/1000,label:'JIT BUY · confirmed · slot 100'}]);assert.equal(queue.size,1);flush();
  assert.equal(engine.renderer.metrics.baseFrames,before.baseFrames);assert.equal(engine.renderer.metrics.annotationFrames,before.annotationFrames+1);assert.equal(engine.renderer.metrics.crosshairFrames,before.crosshairFrames);
  engine.setMarketOverlays([{price:30.999,label:'JIT BUY · confirmed · slot 100'}]);flush();assert.equal(engine.renderer.metrics.annotationFrames,before.annotationFrames+1);
  const layout=engine.serialize();assert.equal(layout.marketOverlays,undefined);assert.equal(layout.orders,undefined);assert.throws(()=>engine.setMarketOverlays([{price:1,label:'bad',color:'url(invalid)'}]));
}));
const solanaIdentity={network:'mainnet-beta',venue:'test-fixture',market:'SOL-USD-PERP',program:mint,baseMint:'So11111111111111111111111111111111111111112',quoteMint:'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'};
const publicAuction=patch=>({id:'auction-1',visibility:'public',side:'buy',status:'open',commitment:'confirmed',slot:100,startSlot:100,endSlot:110,observedAt:1780000020,price:140,remainingBaseAtoms:'100000000000000000001',...patch});
test('Solana auction state preserves exact size and excludes private intents/account payloads',async()=>{
  const {SolanaAuctionBook}=await import('../solana.js');const book=new SolanaAuctionBook(solanaIdentity);
  book.replace({sequence:1,slot:100,auctions:[publicAuction({wallet:'private',positions:[1]})]});const active=book.active();assert.equal(active[0].remainingBaseAtoms,'100000000000000000001');assert.equal(active[0].wallet,undefined);assert.equal(active[0].positions,undefined);assert.ok(Object.isFrozen(active[0]));assert.match(book.overlays()[0].label,/confirmed.*slot 100/);
  assert.throws(()=>book.replace({sequence:2,slot:100,auctions:[publicAuction({visibility:'private'})]}));assert.equal(book.sequence,1);
  assert.throws(()=>book.replace({sequence:2,slot:100,auctions:[publicAuction({remainingBaseAtoms:100})]}));assert.equal(book.sequence,1);
});
test('Solana gaps suppress uncertain auction lines until authoritative recovery, including fork rollback',async()=>{
  const {SolanaAuctionBook}=await import('../solana.js');const book=new SolanaAuctionBook(solanaIdentity);
  book.replace({sequence:10,slot:100,auctions:[publicAuction({commitment:'processed'})]});assert.equal(book.apply({sequence:9,slot:99,auctions:[]}),false);
  assert.throws(()=>book.apply({sequence:12,slot:102,auctions:[]}));assert.equal(book.sequence,10);assert.equal(book.active().length,0);
  assert.throws(()=>book.replace({sequence:9,slot:99,auctions:[]}));book.replace({sequence:12,slot:99,auctions:[]});assert.equal(book.needsRecovery,false);assert.equal(book.slot,99);assert.equal(book.overlays().length,0);
});
test('Auction cancellation/fills/expiry remove lines without manufacturing candles or volume',async()=>{
  const {SolanaAuctionBook}=await import('../solana.js');const book=new SolanaAuctionBook(solanaIdentity,{maxAuctions:1});
  book.replace({sequence:0,slot:100,auctions:[publicAuction()]});book.apply({sequence:1,slot:101,auctions:[publicAuction({status:'filled',remainingBaseAtoms:'0',slot:101})]});assert.equal(book.active().length,0);
  book.apply({sequence:2,slot:102,auctions:[publicAuction({slot:102})]});book.apply({sequence:3,slot:103,auctions:[],removed:['auction-1']});assert.equal(book.active().length,0);
  book.apply({sequence:4,slot:104,auctions:[publicAuction({slot:104})]});book.apply({sequence:5,slot:111,auctions:[]});assert.equal(book.active().length,0);
  book.apply({sequence:6,slot:112,auctions:[publicAuction({id:'new-auction',slot:112,startSlot:112,endSlot:120})]});assert.equal(book.active().length,1);assert.equal(book.candles,undefined);
});
test('Auction batches validate atomically and reject capacity overflow without dropping active orders',async()=>{
  const {SolanaAuctionBook}=await import('../solana.js');const book=new SolanaAuctionBook(solanaIdentity,{maxAuctions:1});
  book.replace({sequence:0,slot:100,auctions:[publicAuction()]});assert.throws(()=>book.apply({sequence:1,slot:101,auctions:[publicAuction({id:'second',slot:101})]}));assert.equal(book.sequence,0);assert.equal(book.entries.size,1);assert.equal(book.overlays().length,0);
  assert.throws(()=>book.replace({sequence:1,slot:100,auctions:[publicAuction({endSlot:99})]}));assert.equal(book.sequence,0);
});
test('Solana adapter coalesces recovery, preserves candles and tears down subscriptions',async()=>{
  const {connectSolanaAuctions}=await import('../solana.js');const engine=adapterEngine();engine.setMarketOverlays=value=>engine.overlays=value;engine.setData(candles([1]));const original=JSON.stringify(engine.buffer.data);let callback,gap,resolveRecovery,calls=0,stopped=0;
  const adapter={identity:solanaIdentity,snapshot:async({minSequence})=>{calls++;if(calls===1)return {sequence:1,slot:100,auctions:[publicAuction()]};assert.equal(minSequence,3);return new Promise(resolve=>resolveRecovery=resolve)},subscribe(fn,options){callback=fn;gap=options.onGap;assert.equal(options.afterSequence,1);return ()=>stopped++}};
  const connection=await connectSolanaAuctions(engine,adapter);assert.equal(engine.overlays.length,1);
  callback({sequence:3,slot:102,auctions:[]});const a=gap(),b=connection.recover();assert.equal(a,b);await Promise.resolve();assert.equal(calls,2);assert.equal(engine.overlays.length,0);
  resolveRecovery({sequence:3,slot:102,auctions:[publicAuction({slot:102})]});assert.equal(await a,true);assert.equal(engine.overlays.length,1);assert.equal(JSON.stringify(engine.buffer.data),original);
  connection.disconnect();connection.disconnect();assert.equal(stopped,1);assert.equal(engine.overlays.length,0);
});
test('Destroyed chart during auction snapshot load never publishes or subscribes',async()=>{
  const {connectSolanaAuctions}=await import('../solana.js');const engine=adapterEngine();let publish=0,subscribe=0,resolveSnapshot;engine.setMarketOverlays=()=>publish++;
  const pending=connectSolanaAuctions(engine,{identity:solanaIdentity,snapshot:()=>new Promise(resolve=>resolveSnapshot=resolve),subscribe(){subscribe++;return ()=>{}}});engine.destroy();resolveSnapshot({sequence:0,slot:100,auctions:[]});await pending;assert.equal(subscribe,0);assert.equal(publish,0);
});
test('Auction recovery cannot unhide stale state when newer missing sequences arrive in flight',async()=>{
  const {connectSolanaAuctions}=await import('../solana.js');const engine=adapterEngine();engine.setMarketOverlays=value=>engine.overlays=value;let callback,resolveRecovery,calls=0;
  const connection=await connectSolanaAuctions(engine,{identity:solanaIdentity,snapshot:async()=>{if(++calls===1)return {sequence:1,slot:100,auctions:[]};return new Promise(resolve=>resolveRecovery=resolve)},subscribe(fn){callback=fn;return ()=>{}}});
  callback({sequence:3,slot:102,auctions:[]});const pending=connection.recover();await Promise.resolve();callback({sequence:5,slot:104,auctions:[]});resolveRecovery({sequence:3,slot:102,auctions:[publicAuction({slot:102})]});assert.equal(await pending,false);assert.equal(engine.overlays.length,0);assert.equal(connection.book.needsRecovery,true);assert.match(engine.errors.at(-1),/newest observed/);connection.disconnect();
});
test('Synchronous auction provider failures can recover on a later attempt',async()=>{
  const {connectSolanaAuctions}=await import('../solana.js');const engine=adapterEngine();engine.setMarketOverlays=value=>engine.overlays=value;let calls=0;
  const connection=await connectSolanaAuctions(engine,{identity:solanaIdentity,snapshot(){if(++calls===2)throw Error('Temporary provider failure');return Promise.resolve({sequence:1,slot:100,auctions:[]})},subscribe(){return ()=>{}}});
  assert.equal(await connection.recover(),false);assert.equal(await connection.recover(),true);assert.equal(calls,3);connection.disconnect();
});
test('Queued chart paints stop when the tab becomes hidden before the frame',()=>renderHarness((engine,flush)=>{
  engine.setData(linear);document.hidden=true;flush();assert.equal(engine.renderer.metrics.baseFrames,0);
  document.hidden=false;engine.invalidate();flush();const before={...engine.renderer.metrics};engine.cross();engine.setMarketOverlays([{price:30,label:'Public auction'}]);document.hidden=true;flush();assert.equal(engine.renderer.metrics.crosshairFrames,before.crosshairFrames);assert.equal(engine.renderer.metrics.annotationFrames,before.annotationFrames);
}));
test('Unchanged viewport skips scene construction but a visible correction rebuilds it',()=>renderHarness((engine,flush)=>{
 let scenes=0;const scene=engine.renderer.scene;engine.renderer.scene=(...args)=>{scenes++;return scene(...args)};
 engine.setData(linear);flush();const before=scenes;
 for(let i=0;i<50;i++){engine.invalidate();flush()}
 assert.equal(scenes,before);
 engine.update([{...linear.at(-1),high:linear.at(-1).high+1}]);flush();assert.equal(scenes,before);
 const visible=engine.scene.candles.at(-1);engine.update([{...visible,high:visible.high+1}]);flush();assert.equal(scenes,before+1);
}));

test('Extended drawing tools survive layouts with the exact number of handles',()=>{
  for(const type of ['ray','extended','vertical','ellipse','arrow']){
    const drawing={id:type,type,points:type==='vertical'?[{time:60,price:1}]:[{time:60,price:1},{time:120,price:2}],color:'#abcdef'};
    const value=validateLayout({version:1,mint,panes:[{interval:1,studies:[],drawings:[drawing]}]});assert.equal(value.panes[0].drawings[0].type,type);
    assert.throws(()=>validateLayout({...value,panes:[{...value.panes[0],drawings:[{...drawing,points:[]}]}]}));
  }
});
test('Chart geometry leaves right whitespace, dedicates a volume band and round trips log prices',async()=>{
  const {Canvas2DRenderer}=await import('../renderer.js');const r=Object.create(Canvas2DRenderer.prototype);r.width=900;r.height=460;
  const scene=r.scene(linear.slice(0,80),0,[],true,'USD',160,{rightPadding:12});assert.ok(scene.x(79)<scene.right-20);assert.ok(scene.volumeTop>scene.bottom);assert.ok(scene.volumeBottom<scene.h-26);assert.ok(Math.abs(scene.priceAt(scene.y(42))-42)<1e-8);
  const custom=r.scene(linear.slice(0,80),0,[],false,'USD',160,{priceRange:{min:1,max:120},chartStyle:'line'});assert.equal(custom.min,1);assert.equal(custom.max,120);assert.equal(custom.chartStyle,'line');
});
test('Ray and extended-line drawing hit tests follow the rendered extension',async()=>{
  const {Canvas2DRenderer}=await import('../renderer.js');const r=Object.create(Canvas2DRenderer.prototype),scene={left:0,right:100,top:0,bottom:100,xAt:t=>t,y:p=>p};
  const d={id:'r',type:'ray',points:[{time:20,price:20},{time:40,price:40}]};assert.deepEqual(r.hit(scene,d,80,80),{id:'r'});assert.equal(r.hit(scene,d,5,5),null);d.type='extended';assert.deepEqual(r.hit(scene,d,5,5),{id:'r'});
});
test('Chart styles repaint only on a change and reject unknown modes',()=>renderHarness((engine,flush)=>{
  engine.setData(linear);flush();const frames=engine.renderer.metrics.baseFrames;engine.setStyle('line');flush();assert.equal(engine.renderer.metrics.baseFrames,frames+1);engine.setStyle('line');flush();assert.equal(engine.renderer.metrics.baseFrames,frames+1);assert.throws(()=>engine.setStyle('fake'));
}));
test('Hide and lock operations preserve drawings and unlocked-only clear is undoable',()=>renderHarness((engine,flush)=>{
  engine.book.add({id:'first',type:'horizontal',points:[{time:60,price:1}]});engine.book.add({id:'second',type:'horizontal',points:[{time:120,price:2}],locked:true});engine.setData(linear);flush();const base=engine.renderer.metrics.baseFrames;
  engine.setDrawingsVisible(false);flush();assert.equal(engine.book.items.length,2);assert.equal(engine.renderer.metrics.baseFrames,base);engine.clearDrawings();flush();assert.deepEqual(engine.book.items.map(d=>d.id),['second']);engine.undo();flush();assert.equal(engine.book.items.length,2);engine.lockAllDrawings(true);flush();assert.ok(engine.book.items.every(d=>d.locked));
}));
test('Shared layouts preserve quote source and chart type without personal overlays',()=>{
 const value=validateLayout({version:1,mint,dataSource:'bitquery',panes:[{interval:15,chartStyle:'area',studies:[],drawings:[],positions:[1]}]});assert.equal(value.dataSource,'bitquery');assert.equal(value.panes[0].chartStyle,'area');assert.equal(value.panes[0].positions,undefined);assert.throws(()=>validateLayout({...value,dataSource:'unknown'}));
});
test('Source changes isolate channels and a late removed subscription cannot remove its replacement',async()=>{
 const {MarketDataHub}=await import('../data.js');const hub=Object.create(MarketDataHub.prototype);let closed=0;const original={closed:false,stream:{close(){closed++}},listeners:new Set(),started:true,data:new Map(),sequence:0};hub.channels=new Map([[1,original]]);hub.notify=()=>{};
 const stop=hub.subscribe(1,()=>{});hub.setSource('bitquery');assert.equal(original.closed,true);assert.equal(closed,1);const replacement=hub.channel(1);stop();assert.equal(hub.channels.get(1),replacement);assert.equal(hub.source,'bitquery');
});
test('Archive layouts cannot switch venue identity or start a native live stream',async()=>{
 const {MarketDataHub}=await import('../data.js');const value=validateLayout({version:1,mint:'binance:SOLUSDT',dataSource:'archive',panes:[{interval:60,studies:[],drawings:[]}]});assert.equal(value.dataSource,'archive');assert.throws(()=>validateLayout({...value,dataSource:'native'}));assert.throws(()=>validateLayout({...value,mint,dataSource:'archive'}));
 const hub=Object.create(MarketDataHub.prototype);hub.mint='binance:SOLUSDT';hub.channels=new Map();hub.setSource('archive');assert.throws(()=>hub.setSource('native'));hub.openStream({});
 assert.equal(validateLayout({...value,mint:'binance:币安人生USDT'}).mint,'binance:币安人生USDT');assert.throws(()=>validateLayout({...value,mint:'binance:../bad'}));
});
test('Renderer keeps base volume units independent of price quote and chart density',async()=>{
 const {Canvas2DRenderer}=await import('../renderer.js');const renderer=Object.create(Canvas2DRenderer.prototype);renderer.width=900;renderer.height=460;const scene=renderer.scene(linear,0,[],false,'USDT',160,{volumeUnit:'SOL',rightPadding:12});assert.equal(scene.quote,'USDT');assert.equal(scene.volumeUnit,'SOL');assert.ok(Number.isFinite(scene.step));
});

test('fork candle deletion changes the buffer revision without manufacturing zero prices',()=>{const b=new CandleBuffer();b.merge(candles([2,3,4]));const before=b.revision;b.remove([b.data[1].time]);assert.equal(b.data.length,2);assert.equal(b.revision,before+1);assert.equal(b.data.some(c=>c.close===3),false);b.remove([1]);assert.equal(b.revision,before+1)});


test('sequenced fork tombstone cannot be restored by an older history response',async()=>{
  const {connectData}=await import('../client.js');const engine=adapterEngine(),first=candles([2])[0];let callback,resolve;
  const adapter={identity:{quoteSymbol:'USD'},history:before=>before?new Promise(r=>resolve=r):Promise.resolve({candles:[first],sequence:2,hasMore:true}),subscribe(fn){callback=fn;return ()=>{}}};
  const stop=await connectData(engine,adapter);const pending=engine.options.loadOlder(first.time);callback({candles:[],deleted:[first.time],sequence:5});assert.equal(engine.buffer.data.length,0);resolve({candles:[first],sequence:3,hasMore:false});await pending;assert.equal(engine.buffer.data.length,0);callback({candles:[{...first,close:3,high:4}],sequence:6});assert.equal(engine.buffer.data[0].close,3);stop();
});
test('native market adapter rejects an unregistered or changed market identity',async()=>{
  const {createSolanaMarketAdapter}=await import('../feed.js');const identity={market:mint,network:'solana',series:'trade',quoteSymbol:'SOL',baseMint:mint,quoteMint:'SOL',program:'adapter'};let changed=false;
  const adapter=await createSolanaMarketAdapter({market:mint,baseURL:'http://localhost:19377',eventSource:class{},fetcher:async()=>({ok:true,json:async()=>({identity:changed?{...identity,baseMint:'different'}:identity,candles:[],sequence:1,hasMore:false,nextBefore:null})})});assert.equal(adapter.identity.pool,mint);changed=true;await assert.rejects(adapter.history(),/identity changed/);await assert.rejects(createSolanaMarketAdapter({market:'invalid',baseURL:'http://localhost',eventSource:class{},fetcher:async()=>{}}),/market address/);
});
