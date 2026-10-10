import {ChartEngine,StudyWorker,validateSDKLayout} from './index.js';
import {verifyLicense} from './license.js';
/** Browser license session. Server API keys never enter this module. */
export async function createChartSDK({licenseProvider,publicKey,usageEndpoint=null,onLicenseError=()=>{}}){
  if(typeof licenseProvider!=='function'||!publicKey)throw Error('Provide a license callback and pinned public signing key');
  const website=location.origin,charts=new Set(),session=crypto.randomUUID(),reported=new WeakSet();let license,claims,worker,closed=false,renewing=false,lastRenew=0,refreshing=null;
  const notify=error=>{if(error&&typeof error==='object'){if(reported.has(error))return;reported.add(error)}try{onLicenseError(error)}catch{}};
  const refresh=()=>{
    if(refreshing)return refreshing;
    refreshing=(async()=>{
      let result;
      try{result=await licenseProvider()}catch(error){
        const status=error?.status,retryable=status===429||(Number.isInteger(status)&&status>=500&&status<=599)||error instanceof TypeError||error?.name==='TimeoutError';
        throw Object.assign(Error(retryable?'Chart license renewal is temporarily unavailable':'Chart license request was rejected'),{retryable,status});
      }
      const verified=await verifyLicense(result.license,publicKey,website);license=result.license;claims=verified;lastRenew=Date.now();
      for(const engine of [...charts].slice(claims.maxCharts))engine.destroy();return claims;
    })().finally(()=>{refreshing=null});return refreshing;
  };
  await refresh();worker=new StudyWorker();
  const assertFeature=feature=>{if(closed||claims.exp<=Date.now()/1000)throw Error('The chart license has expired');if(feature&&!claims.features.includes(feature))throw Error('This feature is not included in the project plan')};
  const heartbeat=()=>{
    if(!usageEndpoint||closed||document.hidden)return;
    try{const url=new URL(usageEndpoint,location.href);if(url.protocol!=='https:'&&url.origin!==location.origin)throw Error();
      fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license,session,charts:charts.size,version:'0.2.0'}),credentials:'omit',keepalive:true}).catch(()=>{});
    }catch{/* Telemetry never blocks price data or chart rendering. */}
  };
  const sdk={
    get entitlement(){return claims},
    createChart(container,options={}){
      assertFeature();if(charts.size>=claims.maxCharts)throw Error(`Your project allows ${claims.maxCharts} concurrent chart${claims.maxCharts===1?'':'s'} per SDK session`);
      if(options.studies?.length)assertFeature('studies');if(options.drawings?.length)assertFeature('drawings');
      const engine=new ChartEngine(container,{...options,indicatorWorker:worker});charts.add(engine);
      for(const [method,feature] of [['setIndicators','studies'],['setTool','drawings'],['setTradingOverlays','tradingOverlays'],['setPositions','tradingOverlays'],['serialize','layouts']]){
        const original=engine[method].bind(engine);engine[method]=(...args)=>{assertFeature(feature);return original(...args)};
      }
      const destroy=engine.destroy.bind(engine);let destroyed=false;engine.destroy=()=>{if(destroyed)return;destroyed=true;charts.delete(engine);destroy();heartbeat()};heartbeat();return engine;
    },
    linkCharts(engines){assertFeature('linkedCharts');if(!engines.every(e=>charts.has(e)))throw Error('Link charts from the same SDK session');const unsubs=[];
      for(const source of engines){unsubs.push(source.on('crosshair',point=>{for(const target of engines)if(target!==source)target.linkedCrosshair(point)}));unsubs.push(source.on('viewport',view=>{for(const target of engines)if(target!==source){target.setViewport({rightTime:view.rightTime===null?null:view.endTime,bars:view.span/(target.interval*60)+1},false);if(target.buffer.view(target.view.rightTime,target.view.bars).start<40&&target.buffer.data.length)target.options.loadOlder?.(target.buffer.data[0].time)}}))}
      let done=false;const unlink=()=>{if(done)return;done=true;unsubs.forEach(f=>f())};for(const engine of engines)unsubs.push(engine.on('destroy',unlink));return unlink;
    },
    saveLayout(engines,market,series='trade'){assertFeature('layouts');if(!engines.every(e=>charts.has(e)))throw Error('Save charts from the same SDK session');return validateSDKLayout({version:1,market,series,linked:engines.length>1,panes:engines.map(e=>e.serialize())})},
    openLayout(containers,layout){assertFeature('layouts');const value=validateSDKLayout(layout);if(containers.length!==value.panes.length||charts.size+containers.length>claims.maxCharts)throw Error('Layout exceeds the project chart limit');const created=[];try{value.panes.forEach((pane,i)=>created.push(sdk.createChart(containers[i],pane)));if(value.linked&&created.length>1)sdk.linkCharts(created);return created}catch(error){created.forEach(e=>e.destroy());throw error}},
    refresh,
    async dataToken(){
      assertFeature();
      if(claims.exp-Date.now()/1000<120){try{await refresh()}catch(error){
        // Reuse only a still-valid signed entitlement for explicit transient
        // provider errors. Rejection/invalid signature/origin stops this SDK.
        if(!error.retryable||closed||claims.exp<=Date.now()/1000){sdk.destroy();notify(error);throw error}
        notify(error);
      }}
      assertFeature();return license;
    },
    destroy(){if(closed)return;closed=true;clearInterval(timer);document.removeEventListener('visibilitychange',visible);for(const engine of [...charts])engine.destroy();worker.destroy()}
  };
  const expire=error=>{sdk.destroy();notify(error)};
  const tick=async()=>{
    if(closed)return;if(claims.exp<=Date.now()/1000){expire(Error('Chart access expired. Renew your project subscription'));return}
    if(!renewing&&claims.exp-Date.now()/1000<300&&Date.now()-lastRenew>30000){renewing=true;try{await refresh()}catch(error){lastRenew=Date.now();if(!error.retryable)expire(error);else notify(error)}finally{renewing=false}}
    heartbeat();
  };
  const visible=()=>{if(!document.hidden)tick()};document.addEventListener('visibilitychange',visible);const timer=setInterval(tick,30000);return sdk;
}
/** Generic host data adapter. Market identity and all three price series remain
 * explicit. The engine consumes completed snapshots and sequenced deltas. */
const dataConnections=new WeakMap();
export async function connectData(engine,adapter,{quote=adapter.identity.quoteSymbol,limit=300}={}){
  if(!engine.alive)return ()=>{};
  if(!Number.isInteger(limit)||limit<1||limit>600)throw Error('History pages must contain 1–600 candles');
  dataConnections.get(engine)?.();
  let closed=false,sequence=-1,loading=false,recovering=null,hasMore=true,unsubscribe=()=>{},stopDestroy=()=>{},historyGeneration=0;
  const versions=new Map(),previousLoad=engine.options.loadOlder,previousRecover=engine.options.recover;
  const report=error=>{if(!closed)engine.emit('error',error instanceof Error?error.message:String(error))};
  const validate=page=>{if(!page||!Number.isSafeInteger(page.sequence)||page.sequence<0||!Array.isArray(page.candles))throw Error('The data adapter returned an invalid sequenced snapshot');if(page.deleted!==undefined&&(!Array.isArray(page.deleted)||page.deleted.length>6000||page.deleted.some(t=>!Number.isSafeInteger(t)||t<=0)))throw Error('Invalid candle correction');return page};
  const merge=page=>{
    if(page.historyReset===true){historyGeneration++;engine.removeCandles(engine.buffer.data.map(c=>c.time));versions.clear();hasMore=true}
    const removed=(page.deleted||[]).filter(t=>page.sequence>=(versions.get(t)??-1));
    if(removed.length){engine.removeCandles(removed);for(const time of removed)versions.set(time,page.sequence)}
    const accepted=page.candles.filter(c=>Number.isFinite(c.time)&&page.sequence>=(versions.get(c.time)??-1));
    if(accepted.length){engine.update(accepted);for(const c of accepted)versions.set(c.time,page.sequence)}
    if(versions.size>6000){const oldest=[...versions.keys()].sort((a,b)=>a-b);for(const time of oldest.slice(0,versions.size-6000))versions.delete(time);}
  };
  const loadOlder=async before=>{
    if(closed||loading||!hasMore)return;loading=true;const generation=historyGeneration;
    try{const older=validate(await adapter.history(before,limit,{minSequence:sequence}));if(!closed&&generation===historyGeneration){merge(older);hasMore=Boolean(older.hasMore)}}catch(error){report(error)}finally{loading=false}
  };
  const recover=()=>{
    if(closed)return Promise.resolve(false);if(recovering)return recovering;
    recovering=(async()=>{
      try{const fresh=validate(await adapter.history(undefined,limit,{minSequence:sequence}));if(closed)return false;
        if(fresh.sequence<sequence)throw Error('Recovery snapshot is older than the stream cursor');
        merge(fresh);sequence=fresh.sequence;return true;
      }catch(error){report(error);return false}finally{recovering=null}
    })();return recovering;
  };
  const disconnect=()=>{
    if(closed)return;closed=true;stopDestroy();try{unsubscribe()}finally{
      if(engine.options.loadOlder===loadOlder)engine.options.loadOlder=previousLoad;
      if(engine.options.recover===recover)engine.options.recover=previousRecover;
      if(dataConnections.get(engine)===disconnect)dataConnections.delete(engine);versions.clear();
    }
  };
  dataConnections.set(engine,disconnect);stopDestroy=engine.on('destroy',disconnect);
  try{
    const page=validate(await adapter.history(undefined,limit));if(closed)return disconnect;
    engine.setData(page.candles,quote);sequence=page.sequence;hasMore=Boolean(page.hasMore);
    for(const c of engine.buffer.data)versions.set(c.time,page.sequence);
    engine.options.loadOlder=loadOlder;engine.options.recover=recover;
    const stop=adapter.subscribe(update=>{
      if(closed)return;try{validate(update);if(update.sequence<sequence||(update.sequence===sequence&&!update.historyReset))return;merge(update);sequence=update.sequence}catch(error){report(error)}
    },{afterSequence:sequence,onGap:recover});
    if(typeof stop!=='function')throw Error('The data adapter must return an unsubscribe function');
    unsubscribe=stop;if(closed)unsubscribe();return disconnect;
  }catch(error){disconnect();throw error}
}
