/** Pure numerical study module, runnable in a Worker or another host's SDK. */
export const STUDIES = [
  {id:'sma',name:'SMA',description:'Simple moving average · 20',overlay:true,color:'#c4a063'},
  {id:'ema',name:'EMA',description:'Exponential moving average · 20',overlay:true,color:'#8772d4'},
  {id:'wma',name:'WMA',description:'Weighted moving average · 20',overlay:true,color:'#6d9ed6'},
  {id:'bb',name:'Bollinger Bands',description:'20 periods · 2 standard deviations',overlay:true,color:'#93aacb'},
  {id:'vwap',name:'VWAP',description:'UTC session · base-asset volume',overlay:true,color:'#d9aa74'},
  {id:'rsi',name:'RSI',description:'Wilder smoothing · 14',overlay:false,color:'#8772d4'},
  {id:'macd',name:'MACD',description:'12 / 26 / 9 · EMA signal',overlay:false,color:'#6d9ed6'},
  {id:'atr',name:'ATR',description:'Average true range · Wilder 14',overlay:false,color:'#d9aa74'},
  {id:'stoch',name:'Stochastic',description:'%K 14 · %D 3',overlay:false,color:'#7bbcad'},
  {id:'obv',name:'OBV',description:'On-balance base-asset volume',overlay:false,color:'#6d9ed6'},
  {id:'cci',name:'CCI',description:'Typical price · 20 · constant 0.015',overlay:false,color:'#d9aa74'},
  {id:'roc',name:'ROC',description:'Price rate of change · 12',overlay:false,color:'#7bbcad'},
];
export const sma=(values,p)=>{const out=Array(values.length).fill(null);let sum=0,valid=0;values.forEach((v,i)=>{if(v!=null){sum+=v;valid++}if(i>=p&&values[i-p]!=null){sum-=values[i-p];valid--}if(i>=p-1&&valid===p)out[i]=sum/p});return out};
export function ema(values,p){const out=Array(values.length).fill(null);let seed=[],previous=null;for(let i=0;i<values.length;i++){const v=values[i];if(v==null)continue;if(previous==null){seed.push(v);if(seed.length===p){previous=seed.reduce((a,b)=>a+b,0)/p;out[i]=previous}}else{previous+=(v-previous)*2/(p+1);out[i]=previous}}return out}
export function wilder(values,p){const out=Array(values.length).fill(null);let seed=[],previous=null;for(let i=0;i<values.length;i++){const v=values[i];if(v==null)continue;if(previous==null){seed.push(v);if(seed.length===p){previous=seed.reduce((a,b)=>a+b,0)/p;out[i]=previous}}else{previous=(previous*(p-1)+v)/p;out[i]=previous}}return out}
export function calculate(candles,id){
  const close=candles.map(c=>c.close),typical=candles.map(c=>(c.high+c.low+c.close)/3),volume=candles.map(c=>c.baseVolume??c.volume),n=close.length;
  const line=(name,values,color)=>({name,values,color});let lines=[],levels=[];
  if(id==='sma')lines=[line('SMA 20',sma(close,20))];
  if(id==='ema')lines=[line('EMA 20',ema(close,20))];
  if(id==='wma')lines=[line('WMA 20',close.map((_,i)=>i<19?null:close.slice(i-19,i+1).reduce((s,v,j)=>s+v*(j+1),0)/210))];
  if(id==='bb'){const middle=sma(close,20),dev=close.map((_,i)=>i<19?null:Math.sqrt(close.slice(i-19,i+1).reduce((s,v)=>s+(v-middle[i])**2,0)/20));lines=[line('BB mid',middle),line('BB upper',middle.map((v,i)=>v==null?null:v+2*dev[i])),line('BB lower',middle.map((v,i)=>v==null?null:v-2*dev[i]))]}
  if(id==='vwap'){let day=null,pv=0,v=0;lines=[line('Session VWAP',typical.map((p,i)=>{const d=Math.floor(candles[i].time/86400);if(day!==d){day=d;pv=0;v=0}pv+=p*volume[i];v+=volume[i];return v>0?pv/v:null}))]}
  if(id==='rsi'){const gains=close.map((v,i)=>i===0?null:Math.max(0,v-close[i-1])),losses=close.map((v,i)=>i===0?null:Math.max(0,close[i-1]-v)),g=wilder(gains,14),l=wilder(losses,14);lines=[line('RSI 14',g.map((v,i)=>v==null?null:l[i]===0?(v===0?50:100):100-100/(1+v/l[i])))];levels=[30,70]}
  if(id==='macd'){const fast=ema(close,12),slow=ema(close,26),macd=close.map((_,i)=>slow[i]==null?null:fast[i]-slow[i]),signal=ema(macd,9);lines=[line('MACD',macd),line('Signal',signal,'#d9aa74'),line('Histogram',macd.map((v,i)=>signal[i]==null?null:v-signal[i]),'#b4c5d9')];levels=[0]}
  if(id==='atr'){const tr=candles.map((c,i)=>i===0?c.high-c.low:Math.max(c.high-c.low,Math.abs(c.high-close[i-1]),Math.abs(c.low-close[i-1])));lines=[line('ATR 14',wilder(tr,14))]}
  if(id==='stoch'){const k=close.map((v,i)=>{if(i<13)return null;const slice=candles.slice(i-13,i+1),hi=Math.max(...slice.map(c=>c.high)),lo=Math.min(...slice.map(c=>c.low));return hi===lo?50:100*(v-lo)/(hi-lo)});lines=[line('%K 14',k),line('%D 3',sma(k,3),'#d9aa74')];levels=[20,80]}
  if(id==='obv'){let total=0;lines=[line('OBV',close.map((v,i)=>{if(i)total+=Math.sign(v-close[i-1])*volume[i];return total}))]}
  if(id==='cci'){const mean=sma(typical,20);lines=[line('CCI 20',typical.map((v,i)=>{if(i<19)return null;const dev=typical.slice(i-19,i+1).reduce((s,p)=>s+Math.abs(p-mean[i]),0)/20;return dev===0?0:(v-mean[i])/(.015*dev)}))];levels=[-100,100]}
  if(id==='roc')lines=[line('ROC 12',close.map((v,i)=>i<12?null:(v/close[i-12]-1)*100))];
  const descriptor=STUDIES.find(s=>s.id===id);if(!descriptor)throw Error('Unsupported indicator');
  if(lines.some(l=>l.values.length!==n||l.values.some(v=>v!=null&&!Number.isFinite(v))))throw Error('Invalid indicator input');
  return {...descriptor,lines,levels};
}
