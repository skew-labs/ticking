import {subscribeCustomerStream} from './customer-stream.js';
export const tradeIdentity=row=>String(row.identity||row.id||['slot','tx_order','event_order','pool','signature'].map(k=>row[k]??'').join(':'));
export function applyTradeMessage(previous,message){
 if(!Number.isSafeInteger(message.seq)||message.seq<1)throw Error('Invalid execution sequence.');
 if(message.type==='snapshot'){
  if(!Array.isArray(message.data?.trades)||message.data.trades.length>100)throw Error('Invalid tape window.');
  if(new Set(message.data.trades.map(tradeIdentity)).size!==message.data.trades.length)throw Error('Duplicate execution identities.');
  return {seq:message.seq,data:message.data};
 }
 if(message.type!=='trade_delta'||!previous||message.base!==previous.seq||message.seq!==previous.seq+1||!Array.isArray(message.ids)||message.ids.length>100||!Array.isArray(message.trades)||message.trades.length>100)throw Error('Execution base changed.');
 const rows=new Map(previous.data.trades.map(r=>[tradeIdentity(r),r]));for(const row of message.trades)rows.set(tradeIdentity(row),row);
 const trades=message.ids.map(id=>rows.get(id));if(trades.some(r=>!r)||new Set(message.ids).size!==message.ids.length)throw Error('Incomplete execution window.');
 return {seq:message.seq,data:{...message.meta,trades}};
}
export function createTradeFeed({tokenProvider,apiBase='https://api.ticking.dev'}={}){
 if(typeof tokenProvider!=='function')throw Error('Supply short-lived display tokens from your project.');
 let grant=null,current=null,stopped=false,transport=null,delivered=null,lastState={state:'connecting'};const subscriptions=new Set();
 async function headers(){if(!grant||grant.expires*1000<Date.now()+15000)grant=await tokenProvider();if(!grant?.token?.startsWith('td_'))throw Error('Keep server secrets off the browser.');return {Authorization:'Bearer '+grant.token};}
 return {
  subscribe(onData,onState=()=>{}){if(stopped)throw Error('Feed disposed.');const listener={onData,onState};subscriptions.add(listener);if(delivered)onData(delivered);onState(lastState);if(!transport)transport=subscribeCustomerStream('/api/v1/display/trades/stream',{apiBase,headers,onMessage:event=>{current=applyTradeMessage(current,event);delivered={...current.data,receipt:event.receipt,cursor:event.cursor};for(const sub of subscriptions){try{sub.onData(delivered);}catch{sub.onState({state:'consumer_error'});}}},onState:state=>{lastState=state;for(const sub of subscriptions)sub.onState(state);}});return ()=>{subscriptions.delete(listener);if(!subscriptions.size){transport?.();transport=null;}};},
  async history({before,limit=100,signal}={}){if(stopped)throw Error('Feed disposed.');const q=new URLSearchParams({limit:String(limit)});if(before)q.set('before',before);const r=await fetch(new URL('/api/v1/display/trades/history?'+q,apiBase),{headers:await headers(),signal,credentials:'omit',cache:'no-store'});if(!r.ok)throw Error('Execution history unavailable ('+r.status+').');const data=await r.json();if(!Array.isArray(data.trades)||data.trades.length>100)throw Error('Invalid execution page.');return data;},
  destroy(){stopped=true;transport?.();transport=null;subscriptions.clear();grant=null;current=null;delivered=null;},
 };
}
export function mountExecutionTape(root,feed){
 const header=document.createElement('p');header.textContent='Retained pool executions · native quote units';header.style.cssText='padding:12px 16px;margin:0;color:#7b8498';const table=document.createElement('table');table.style.cssText='width:100%;border-collapse:collapse;font:12px inherit;font-variant-numeric:tabular-nums';const head=document.createElement('thead'),body=document.createElement('tbody'),line=document.createElement('tr');for(const label of ['Time (UTC)','Price · native quote','Size · base','Quote']){const cell=document.createElement('th');cell.textContent=label;cell.style.cssText='text-align:right;padding:10px 16px;border-bottom:1px solid #e4e7ef;font-weight:500';line.append(cell);}head.append(line);table.append(head,body);root.replaceChildren(header,table);const rows=new Map(),quotes={'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v':'USDC','So11111111111111111111111111111111111111112':'SOL','Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB':'USDT'};
 const stop=feed.subscribe(data=>{header.textContent=(data.identity?.network==='exchange'?'Hyperliquid executions':'Retained pool executions')+' · '+(data.quote||'native quote units');const ordered=[...data.trades].reverse(),keep=new Set(ordered.map(tradeIdentity));for(const [id,row] of rows)if(!keep.has(id)){row.remove();rows.delete(id);}ordered.forEach((r,index)=>{const id=tradeIdentity(r);let tr=rows.get(id);if(!tr){tr=document.createElement('tr');tr.title=data.identity?.network==='exchange'?'Trade '+r.tradeId+' · '+data.identity.market:'Slot '+r.slot+' · '+(r.signature||'');for(let n=0;n<4;n++){const cell=document.createElement('td');cell.style.cssText='text-align:right;padding:8px 16px;border-bottom:1px solid #e4e7ef';tr.append(cell);}rows.set(id,tr);}const quote=r.quote_mint||data.identity?.quoteMint||'',values=[r.time?new Date(r.time*1000).toISOString().slice(11,19):String(r.slot),Number(r.price).toLocaleString('en-US',{maximumSignificantDigits:8}),Number(r.base_volume).toLocaleString('en-US',{maximumSignificantDigits:6}),quotes[quote]||String(quote).slice(0,8)];values.forEach((value,n)=>{if(tr.cells[n].textContent!==value)tr.cells[n].textContent=value;});tr.cells[3].title=quote;if(body.children[index]!==tr)body.insertBefore(tr,body.children[index]||null);});},state=>{header.textContent=state.state==='blocked'?'Data plan required for the execution tape.':'Retained pool executions · '+state.state;});
 return ()=>{stop();rows.clear();};
}
