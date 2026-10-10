// Native candle studies. Warm-up and unknown execution gaps remain unavailable.
export const INDICATORS=[
 {id:'sma',name:'Simple moving average',short:'SMA',color:'#d0a052',defaults:{period:20},fields:[['period','Length',2,200]]},
 {id:'ema',name:'Exponential moving average',short:'EMA',color:'#4384c4',defaults:{period:20},fields:[['period','Length',2,200]]},
 {id:'bb',name:'Bollinger Bands',short:'BB',color:'#7564c3',defaults:{period:20,multiplier:2},fields:[['period','Length',2,200],['multiplier','Std. deviations',.5,5,.5]]},
 {id:'rsi',name:'Relative strength index',short:'RSI',color:'#7564c3',pane:true,defaults:{period:14},fields:[['period','Length',2,200]]},
 {id:'macd',name:'MACD',short:'MACD',color:'#4384c4',pane:true,defaults:{fast:12,slow:26,signal:9},fields:[['fast','Fast EMA',2,100],['slow','Slow EMA',3,200],['signal','Signal EMA',2,100]]}
];
export function normalizeIndicators(input){
 if(!Array.isArray(input))return [];
 return INDICATORS.flatMap(def=>{const value=input.find(x=>x?.id===def.id);if(!value)return [];const config={id:def.id};
  for(const [key,,min,max,step=1] of def.fields){const n=Number(value[key]??def.defaults[key]);if(!Number.isFinite(n)||n<min||n>max||Math.abs(n/step-Math.round(n/step))>1e-8)throw Error(`Invalid ${def.short} ${key}.`);config[key]=n;}
  if(def.id==='macd'&&config.fast>=config.slow)throw Error('MACD fast length must be smaller than slow length.');return [config];
 });
}
function rolling(values,period,withDeviation=true){
 const mean=Array(values.length).fill(null),deviation=withDeviation?Array(values.length).fill(null):null,ring=new Float64Array(period);let size=0,index=0,sum=0,center=0,m2=0,rebase=0;
 values.forEach((value,i)=>{
  if(value===null){size=index=sum=center=m2=rebase=0;return;}
  if(size===period){const old=ring[index];sum-=old;if(withDeviation){const next=(period*center-old)/(period-1);m2-=(old-center)*(old-next);center=next;}size--;}
  ring[index]=value;index=(index+1)%period;sum+=value;size++;
  if(withDeviation){const delta=value-center;center+=delta/size;m2+=delta*(value-center);}
  if(size===period){const m=sum/period;mean[i]=m;if(withDeviation){
   // Rebase once per window to bound removable-variance roundoff, including
   // high nominal prices with tiny changes. No O(period) scan on every bar.
   if(++rebase>=period||m2<0){m2=0;for(let j=0;j<period;j++)m2+=(ring[j]-m)**2;center=m;rebase=0;}
   deviation[i]=Math.sqrt(Math.max(0,m2/period+(center-m)**2));
  }}
 });return {mean,deviation};
}
function ema(values,period){let seed=[],previous=null;const alpha=2/(period+1);return values.map(value=>{if(value===null){seed=[];previous=null;return null;}if(previous===null){seed.push(value);if(seed.length<period)return null;previous=seed.reduce((s,x)=>s+x,0)/period;}else previous+=alpha*(value-previous);return previous;});}
function rsi(values,period){let previous=null,gain=0,loss=0,count=0;return values.map(value=>{if(value===null){previous=null;gain=loss=count=0;return null;}if(previous===null){previous=value;return null;}const delta=value-previous;previous=value;const g=Math.max(0,delta),l=Math.max(0,-delta);if(count<period){gain+=g;loss+=l;count++;if(count<period)return null;gain/=period;loss/=period;}else{gain=(gain*(period-1)+g)/period;loss=(loss*(period-1)+l)/period;}return loss===0?gain===0?50:100:100-100/(1+gain/loss);});}
export function computeIndicators(series,configs,period){
 if(!configs.length)return [];
 const closes=series.map((bar,i)=>!Number.isFinite(bar.close)||bar.synthetic||(i&&((period&&bar.time-series[i-1].time!==period)||(bar.quoteCurrency&&series[i-1].quoteCurrency&&bar.quoteCurrency!==series[i-1].quoteCurrency)))?null:bar.close);
 return configs.map(config=>{const def=INDICATORS.find(x=>x.id===config.id);let lines;
  if(config.id==='sma')lines={value:rolling(closes,config.period,false).mean};
  if(config.id==='ema')lines={value:ema(closes,config.period)};
  if(config.id==='bb'){const {mean,deviation}=rolling(closes,config.period);lines={middle:mean,upper:mean.map((x,i)=>x===null?null:x+config.multiplier*deviation[i]),lower:mean.map((x,i)=>x===null?null:x-config.multiplier*deviation[i])};}
  if(config.id==='rsi')lines={value:rsi(closes,config.period)};
  if(config.id==='macd'){const fast=ema(closes,config.fast),slow=ema(closes,config.slow),value=fast.map((x,i)=>x===null||slow[i]===null?null:x-slow[i]),signal=ema(value,config.signal);lines={value,signal,histogram:value.map((x,i)=>x===null||signal[i]===null?null:x-signal[i])};}
  return {...config,definition:def,lines};
 });
}

// These five close-based studies depend on price, time gaps, source units and
// synthetic flags. Volume-only updates still redraw candles without rebuilding
// the identical study arrays. Historical corrections invalidate the cache too.
export function sameIndicatorInput(previous,next){
 if(!previous||previous.length!==next.length)return false;
 for(let i=0;i<next.length;i++){const a=previous[i],b=next[i];if(a===b)continue;if(a.time!==b.time||!Object.is(a.close,b.close)||a.synthetic!==b.synthetic||a.quoteCurrency!==b.quoteCurrency)return false;}
 return true;
}
// Dense Bollinger polygons are expensive to rasterize while panning. Cache the
// exact study pixels, independent of the visible first index. A changed study,
// price transform, zoom, theme or pixel ratio creates a new bounded bitmap.
const bandPixels=new WeakMap();
function paintCachedBand(ctx,s,{begin,end,x,y,top,bottom,dark}){
 if(end-begin<250)return false;
 const step=Number((x(1)-x(0)).toPrecision(12)),dpr=ctx.getTransform().a,h=bottom-top,w=s.lines.upper.length*step;
 if(!(step>0&&h>0)||w*dpr>8192||w*h*dpr*dpr>4000000)return false;
 const key=[step,y(0),y(1),top,h,dpr,dark,s.definition.color].join(':');let cache=bandPixels.get(s);
 if(cache?.key!==key){
  const canvas=document.createElement('canvas');canvas.width=Math.ceil(w*dpr);canvas.height=Math.ceil(h*dpr);const c=canvas.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);c.lineWidth=1.5;
  const px=i=>(i+.5)*step,py=value=>y(value)-top,{upper,lower,middle}=s.lines;let run=[];
  const fill=()=>{if(run.length>1){c.beginPath();run.forEach((i,j)=>j?c.lineTo(px(i),py(upper[i])):c.moveTo(px(i),py(upper[i])));for(let j=run.length-1;j>=0;j--)c.lineTo(px(run[j]),py(lower[run[j]]));c.closePath();c.fillStyle=s.definition.color+(dark?'20':'10');c.fill();}run=[];};
  for(let i=0;i<upper.length;i++){if(upper[i]===null)fill();else run.push(i);}fill();
  for(const [values,dash] of [[upper,[]],[lower,[]],[middle,[4,3]]]){c.beginPath();let started=false;values.forEach((v,i)=>{if(v===null){started=false;return;}started?c.lineTo(px(i),py(v)):c.moveTo(px(i),py(v));started=true;});c.strokeStyle=s.definition.color;c.setLineDash(dash);c.stroke();}
  cache={key,canvas,w:canvas.width/dpr,h:canvas.height/dpr};bandPixels.set(s,cache);
 }
 ctx.drawImage(cache.canvas,x(0)-(begin+.5)*step,top,cache.w,cache.h);return true;
}
export function paintIndicators(ctx,studies,{begin,end,x,y,width,top,bottom,dark,grid,text,format,volumeBottom}){
 const panes=[];const line=(values,map,color,dash=[])=>{ctx.beginPath();let started=false;values.slice(begin,end).forEach((value,i)=>{if(value===null){started=false;return;}const px=x(i),py=map(value);started?ctx.lineTo(px,py):ctx.moveTo(px,py);started=true;});ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.setLineDash(dash);ctx.stroke();ctx.setLineDash([]);};
 ctx.save();ctx.beginPath();ctx.rect(0,top,width,bottom-top);ctx.clip();
 for(const s of studies.filter(s=>!s.definition.pane)){
  if(s.id==='bb'){if(paintCachedBand(ctx,s,{begin,end,x,y,top,bottom,dark}))continue;const {upper,lower,middle}=s.lines;let run=[];const fill=()=>{if(run.length<2){run=[];return;}ctx.beginPath();run.forEach((i,j)=>j?ctx.lineTo(x(i-begin),y(upper[i])):ctx.moveTo(x(i-begin),y(upper[i])));[...run].reverse().forEach(i=>ctx.lineTo(x(i-begin),y(lower[i])));ctx.closePath();ctx.fillStyle=s.definition.color+(dark?'20':'10');ctx.fill();run=[];};for(let i=begin;i<end;i++){if(upper[i]===null)fill();else run.push(i);}fill();line(upper,y,s.definition.color);line(lower,y,s.definition.color);line(middle,y,s.definition.color,[4,3]);}
  else line(s.lines.value,y,s.definition.color);
 }
 ctx.restore();let paneTop=volumeBottom;
 for(const s of studies.filter(s=>s.definition.pane)){
  const paneHeight=s.paneHeight,paneBottom=paneTop+paneHeight,values=Object.values(s.lines).flatMap(a=>a.slice(begin,end).filter(x=>x!==null)),span=Math.max(...values.map(Math.abs),1e-12)*1.2;
  const origin=paneTop,min=s.id==='rsi'?0:-span,max=s.id==='rsi'?100:span,map=value=>origin+22+(max-value)/(max-min)*(paneHeight-30);
  panes.push({id:s.id,top:origin,bottom:paneBottom,min,max,y:map,value:py=>max-(py-origin-22)/(paneHeight-30)*(max-min)});
  ctx.save();ctx.beginPath();ctx.rect(0,paneTop,width,paneHeight);ctx.clip();ctx.strokeStyle=grid;ctx.beginPath();ctx.moveTo(0,paneTop);ctx.lineTo(width,paneTop);ctx.stroke();
  const guides=s.id==='rsi'?[30,70]:[0];ctx.font='500 10px "DM Sans",sans-serif';ctx.textAlign='left';
  for(const v of guides){ctx.setLineDash([3,4]);ctx.strokeStyle=grid;ctx.beginPath();ctx.moveTo(0,map(v));ctx.lineTo(width,map(v));ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=text;ctx.fillText(String(v),width-23,map(v)-6);}
  if(s.id==='rsi'){ctx.fillStyle=s.definition.color+'08';ctx.fillRect(0,map(70),width,map(30)-map(70));line(s.lines.value,map,s.definition.color);}
  else{s.lines.histogram.slice(begin,end).forEach((value,i)=>{if(value===null)return;ctx.fillStyle=value>=0?'#239a8290':'#d36a7990';ctx.fillRect(x(i)-2,Math.min(map(0),map(value)),4,Math.max(1,Math.abs(map(value)-map(0))));});line(s.lines.value,map,s.definition.color);line(s.lines.signal,map,'#d0a052');}
  const last=s.lines.value[end-1];ctx.fillStyle=text;ctx.fillText(`${s.definition.short} ${s.period||`${s.fast}, ${s.slow}, ${s.signal}`}   ${last==null?'Warming up / data gap':s.id==='rsi'?last.toFixed(2):format(last)}`,8,paneTop+12);ctx.restore();paneTop=paneBottom;
 }
 return panes;
}
