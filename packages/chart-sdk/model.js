import {STUDIES} from './indicators.js';
export const TOOLS=['cursor','trend','ray','extended','horizontal','vertical','box','ellipse','arrow','text','fib','measure'];
export const INTERVALS=[1,5,15,60,240,1440];
export const clone=value=>JSON.parse(JSON.stringify(value));
export function validCandle(c){return c&&Number.isSafeInteger(c.time)&&c.time>0&&['open','high','low','close','volume'].every(k=>Number.isFinite(c[k]))&&Math.min(c.open,c.high,c.low,c.close)>0&&c.high>=Math.max(c.open,c.close,c.low)&&c.low<=Math.min(c.open,c.close)&&c.volume>=0}
const candleFields=['time','open','high','low','close','volume','baseVolume','count'];
const sameCandle=(a,b)=>a&&candleFields.every(key=>a[key]===b[key]);
export class CandleBuffer {
  constructor(limit=2400){this.limit=limit;this.data=[];this.revision=0}
  remove(times){const removed=new Set(times);const next=this.data.filter(c=>!removed.has(c.time));if(next.length!==this.data.length){this.data=next;this.revision++}return this.data}
  merge(page,anchor=null){
    const map=new Map(this.data.map(c=>[c.time,c]));let changed=false;
    for(const c of page)if(validCandle(c)&&!sameCandle(map.get(c.time),c)){map.set(c.time,{...c});changed=true}
    if(!changed)return this.data;
    let data=[...map.values()].sort((a,b)=>a.time-b.time);
    if(data.length>this.limit){let end=data.length;if(anchor!=null){const i=data.findIndex(c=>c.time>=anchor);if(i>=0)end=Math.min(data.length,Math.max(this.limit,i+Math.floor(this.limit/2)))}data=data.slice(Math.max(0,end-this.limit),end)}
    // A new live candle may be outside an anchored, bounded history window.
    if(data.length===this.data.length&&data.every((c,i)=>sameCandle(this.data[i],c)))return this.data;
    this.data=data;this.revision++;return data;
  }
  view(rightTime,bars){let end=this.data.length;if(rightTime!=null){end=this.upperBound(rightTime)}const start=Math.max(0,end-Math.round(bars));return {candles:this.data.slice(start,end),start,end}}
  upperBound(time){let lo=0,hi=this.data.length;while(lo<hi){const mid=(lo+hi)>>1;if(this.data[mid].time<=time)lo=mid+1;else hi=mid}return lo}
}
export function validateDrawing(d){if(!d||!TOOLS.includes(d.type)||d.type==='cursor'||typeof d.id!=='string'||d.id.length>80||!Array.isArray(d.points)||d.points.length!==(['horizontal','vertical','text'].includes(d.type)?1:2))throw Error('Invalid drawing');for(const p of d.points)if(!Number.isSafeInteger(p.time)||p.time<=0||!Number.isFinite(p.price)||p.price<=0)throw Error('Invalid drawing coordinates');if(d.text!=null&&(typeof d.text!=='string'||d.text.length>160))throw Error('Invalid drawing text');if(d.color&&!/^#[a-f0-9]{6}$/i.test(d.color))throw Error('Invalid drawing color');return {id:d.id,type:d.type,points:clone(d.points),locked:!!d.locked,text:d.text||'',color:d.color||'#8772d4'};}
export class DrawingBook {
  constructor(drawings=[]){this.items=drawings.map(validateDrawing);this.past=[];this.future=[]}
  commit(next){if(next.length>100)throw Error('Maximum 100 drawings per pane');const validated=next.map(validateDrawing);if(JSON.stringify(validated)===JSON.stringify(this.items))return;this.past.push(clone(this.items));if(this.past.length>100)this.past.shift();this.future=[];this.items=validated}
  undo(){if(!this.past.length)return;this.future.push(clone(this.items));this.items=this.past.pop()}
  redo(){if(!this.future.length)return;this.past.push(clone(this.items));this.items=this.future.pop()}
  add(d){this.commit([...this.items,d])}
  edit(id,patch){const d=this.items.find(d=>d.id===id);if(!d||d.locked&&patch.locked!==false)return;this.commit(this.items.map(d=>d.id===id?{...d,...patch}:d))}
  remove(id){this.commit(this.items.filter(d=>d.id!==id||d.locked))}
}
export function validateLayout(layout){
  if(!layout||layout.version!==1||typeof layout.mint!=='string'||!(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(layout.mint)||/^binance:[\p{L}\p{N}_]{3,40}$/u.test(layout.mint))||!Array.isArray(layout.panes)||layout.panes.length<1||layout.panes.length>4)throw Error('Unsupported chart layout');
  if(layout.dataSource!==undefined&&(!['native','bitquery','archive'].includes(layout.dataSource)||(layout.dataSource==='archive')!==layout.mint.startsWith('binance:')))throw Error('Invalid layout data source');
  return {version:1,mint:layout.mint,dataSource:layout.dataSource||(layout.mint.startsWith('binance:')?'archive':'native'),linked:layout.linked!==false,panes:layout.panes.map(p=>{if(!INTERVALS.includes(p.interval)||!Array.isArray(p.studies)||p.studies.length>12||p.studies.some(id=>!STUDIES.some(s=>s.id===id))||!Array.isArray(p.drawings)||p.drawings.length>100)throw Error('Invalid pane configuration');if(p.chartStyle!==undefined&&!['candles','bars','line','area'].includes(p.chartStyle))throw Error('Invalid saved chart type');return {interval:p.interval,chartStyle:p.chartStyle||'candles',log:!!p.log,studies:[...new Set(p.studies)],drawings:p.drawings.map(validateDrawing),view:p.view&&Number.isFinite(p.view.bars)&&p.view.bars>=15&&p.view.bars<=600&&(!p.view.rightTime||Number.isSafeInteger(p.view.rightTime))?{bars:p.view.bars,rightTime:p.view.rightTime||null}:{bars:80,rightTime:null}}})};
}
/** Exchange-independent saved layout. Community mint validation remains separate. */
export function validateSDKLayout(layout){
  if(!layout||typeof layout.market!=='string'||!layout.market.trim()||layout.market.length>160||!['trade','mark','index'].includes(layout.series))throw Error('Invalid SDK market identity');
  const checked=validateLayout({...layout,mint:'So11111111111111111111111111111111111111112'});
  return {version:1,market:layout.market,series:layout.series,linked:checked.linked,panes:checked.panes};
}
