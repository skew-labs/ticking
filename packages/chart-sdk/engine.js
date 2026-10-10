import {CandleBuffer,DrawingBook,clone,TOOLS} from './model.js';
import {Canvas2DRenderer,priceText} from './renderer.js';
/** The engine only knows its data adapter, renderer and browser container. */
export class ChartEngine {
  constructor(host,options={}){
    this.host=host;this.options=options;this.interval=options.interval||1;this.quote=options.quote||'QUOTE';this.volumeUnit=options.volumeUnit;this.buffer=new CandleBuffer(options.maxCandles||2400);this.book=new DrawingBook(options.drawings||[]);this.view=options.view||{bars:80,rightTime:null};this.log=!!options.log;this.studies=options.studies||['ema','vwap'];this.results=[];this.orders=[];this.tool='cursor';this.selected=null;this.hover=null;this.drag=null;this.preview=null;this.frame=null;this.crossFrame=null;this.alive=true;this.handlers=[];this.listeners=new Map();this.job=0;
    this.layers=['base','drawings','crosshair'].map(name=>{const c=document.createElement('canvas');c.className='sdk-layer sdk-'+name;host.append(c);return c});const canvas=this.layers[2];canvas.tabIndex=0;canvas.setAttribute('role','img');canvas.setAttribute('aria-label','Candlestick chart. Drag to pan, scroll to zoom. Select a drawing tool to annotate. Arrow keys pan. Plus and minus zoom.');
    this.legend=document.createElement('div');this.legend.className='sdk-legend';host.append(this.legend);this.empty=document.createElement('div');this.empty.className='sdk-empty';this.empty.textContent='Waiting for market data…';host.append(this.empty);
    this.renderer=options.rendererFactory?options.rendererFactory(this.layers):new Canvas2DRenderer(this.layers,options.theme,{locale:options.locale,timeZone:options.timeZone,priceFormatter:options.priceFormatter});
    this.chartStyle=options.chartStyle||'candles';this.rightPadding=Math.max(0,Math.min(40,options.rightPadding??8));this.drawingsVisible=true;
    this.resize=new ResizeObserver(()=>this.invalidate());this.resize.observe(host);this.inViewport=true;
    if(typeof IntersectionObserver!=='undefined'){this.intersection=new IntersectionObserver(entries=>{const visible=entries.at(-1)?.isIntersecting;if(visible==null)return;const changed=visible!==this.inViewport;this.inViewport=visible;if(visible&&changed){this.compute();this.invalidate()}});this.intersection.observe(host)}
    this.onDOM(canvas,'wheel',e=>{e.preventDefault();if(this.scene&&e.offsetX>this.scene.right)this.scalePrice(e.deltaY>0?1.1:.9);else this.zoom(e.deltaY>0?1.12:.88,e.offsetX)},{passive:false});
    this.onDOM(canvas,'dblclick',e=>{if(this.scene&&e.offsetX>this.scene.right){this.priceRange=null;this.invalidate()}else this.reset()});
    this.onDOM(canvas,'pointerdown',e=>this.down(e));this.onDOM(canvas,'pointermove',e=>this.move(e));this.onDOM(canvas,'pointerup',e=>this.up(e));this.onDOM(canvas,'pointercancel',()=>{this.drag=null;this.preview=null;this.paintAnnotations()});this.onDOM(canvas,'pointerleave',()=>{if(!this.drag){this.hover=null;this.cross();this.emit('crosshair',null)}});
    this.onDOM(canvas,'keydown',e=>{if(['ArrowLeft','ArrowRight','+','-','=','Home','End','Delete','Backspace'].includes(e.key)){e.preventDefault();if(e.key==='ArrowLeft')this.pan(5);if(e.key==='ArrowRight')this.pan(-5);if(e.key==='+'||e.key==='=')this.zoom(.8);if(e.key==='-')this.zoom(1.2);if(e.key==='Home'||e.key==='End')this.reset();if(e.key==='Delete'||e.key==='Backspace')this.deleteSelected()}if((e.ctrlKey||e.metaKey)&&e.key==='z'){e.preventDefault();e.shiftKey?this.redo():this.undo()}});
    this.visibility=()=>{if(!document.hidden){this.compute();this.invalidate();this.options.recover?.()}};document.addEventListener('visibilitychange',this.visibility);this.invalidate();
  }
  onDOM(target,name,fn,options){target.addEventListener(name,fn,options);this.handlers.push([target,name,fn,options])}
  on(name,callback){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(callback);return ()=>this.listeners.get(name)?.delete(callback)}
  emit(name,value){for(const cb of this.listeners.get(name)||[])cb(value)}
  setData(candles,quote=this.quote){
    if(!this.alive)return;const previous=this.buffer.data,revision=this.buffer.revision,quoteChanged=this.quote!==quote;this.quote=quote;this.buffer.merge(candles,this.view.rightTime);
    const changed=this.buffer.revision!==revision;if(!changed&&!quoteChanged)return;
    if(changed&&(previous.length!==this.buffer.data.length||previous.some((c,i)=>c.time!==this.buffer.data[i].time)))this.results=[];
    this.empty.textContent=this.buffer.data.length?'':'No candles yet. Waiting for your market feed.';
    if(changed&&!document.hidden)this.compute();this.invalidate();
  }
  removeCandles(times){
    if(!this.alive)return;const revision=this.buffer.revision;this.buffer.remove(times);if(this.buffer.revision===revision)return;this.results=[];this.empty.textContent=this.buffer.data.length?'':'No candles yet. Waiting for your market feed.';if(!document.hidden)this.compute();this.invalidate();
  }
  update(candles){const previous=this.buffer.data.at(-1)?.close;this.setData(candles);const price=this.buffer.data.at(-1)?.close;if(price!==previous)this.emit('price',price)}
  setIndicators(ids){if(ids.length===this.studies.length&&ids.every((id,i)=>id===this.studies[i]))return;this.studies=[...ids];this.compute();this.invalidate()}
  compute(){
    const id=++this.job,worker=this.options.indicatorWorker;
    if(!this.alive||document.hidden||this.inViewport===false||!worker||this.computing)return;
    this.computing=true;let task;
    try{task=worker.request(this.buffer.data,this.studies)}catch(error){task=Promise.reject(error)}
    Promise.resolve(task).then(results=>{if(this.alive&&id===this.job){this.results=results;this.invalidate()}})
      .catch(error=>{if(this.alive)this.emit('error',error.message)})
      .finally(()=>{this.computing=false;if(this.alive&&!document.hidden&&this.inViewport!==false&&id!==this.job)this.compute()});
  }
  setTool(tool){if(!TOOLS.includes(tool))return;this.tool=tool;this.layers[2].style.cursor=tool==='cursor'?'crosshair':'cell';this.preview=null;this.paintAnnotations()}
  setStyle(style){if(!['candles','bars','line','area'].includes(style))throw Error('Invalid chart style');if(this.chartStyle===style)return;this.chartStyle=style;this.invalidate()}
  setMagnet(enabled){this.magnet=!!enabled}
  setDrawingsVisible(visible){this.drawingsVisible=!!visible;this.paintAnnotations()}
  lockAllDrawings(locked){this.book.commit(this.book.items.map(d=>({...d,locked:!!locked})));this.paintAnnotations();this.emit('drawing',this.book.items)}
  clearDrawings(){this.book.commit(this.book.items.filter(d=>d.locked));this.selected=null;this.paintAnnotations();this.emit('drawing',this.book.items)}
  scalePrice(factor){if(!this.scene)return;const {min,max}=this.priceRange||this.scene,mid=(min+max)/2,half=(max-min)*factor/2;this.priceRange={min:mid-half,max:mid+half};this.invalidate()}
  takeScreenshot(){const canvas=document.createElement('canvas');canvas.width=this.layers[0].width;canvas.height=this.layers[0].height;const ctx=canvas.getContext('2d');for(const layer of this.layers.slice(0,2))ctx.drawImage(layer,0,0);ctx.fillStyle=this.renderer.theme.axisText;ctx.font=`${12*this.renderer.dpr}px sans-serif`;ctx.fillText(this.legend.textContent,14*this.renderer.dpr,20*this.renderer.dpr);return canvas.toDataURL('image/png')}
  setTheme(theme){if(typeof this.renderer.setTheme!=='function')throw Error('Renderer does not support themes');this.renderer.setTheme(theme);this.themeRevision=(this.themeRevision||0)+1;this.legend.style.color=this.renderer.theme?.axisText||'#9ba6b8';this.invalidate()}
  setLog(value){value=!!value;if(this.log===value)return;this.log=value;this.priceRange=null;this.invalidate()}
  setViewport(view,emit=false){const next={bars:Math.max(15,Math.min(600,view.bars)),rightTime:view.rightTime||null};if(next.bars===this.view.bars&&next.rightTime===this.view.rightTime)return;this.view=next;this.invalidate();if(emit)this.viewportChanged()}
  viewportChanged(){const {candles,start}=this.buffer.view(this.view.rightTime,this.view.bars);this.emit('viewport',{...this.view,startTime:candles[0]?.time,endTime:this.view.rightTime||candles.at(-1)?.time,span:(this.view.bars-1)*this.interval*60});if(start<40&&this.buffer.data.length)this.options.loadOlder?.(this.buffer.data[0].time)}
  pan(delta){const end=this.view.rightTime?this.buffer.upperBound(this.view.rightTime):this.buffer.data.length;const next=Math.max(Math.min(15,this.buffer.data.length),Math.min(this.buffer.data.length,end-delta));this.view.rightTime=next>=this.buffer.data.length?null:this.buffer.data[next-1]?.time||null;this.invalidate();this.viewportChanged()}
  zoom(factor,px){const old=this.view.bars,bars=Math.max(15,Math.min(600,old*factor));if(px!=null&&this.scene&&this.buffer.data.length){const ratio=Math.max(0,Math.min(1,(px-this.scene.left)/(this.scene.right-this.scene.left)));const end=this.view.rightTime?this.buffer.upperBound(this.view.rightTime):this.buffer.data.length;const next=Math.min(this.buffer.data.length,Math.max(1,Math.round(end+(bars-old)*(1-ratio))));this.view.rightTime=next===this.buffer.data.length?null:this.buffer.data[next-1].time}this.view.bars=bars;this.invalidate();this.viewportChanged()}
  reset(){this.view={bars:this.options.view?.bars||160,rightTime:null};this.priceRange=null;this.invalidate();this.viewportChanged()}
  invalidate(){if(!this.alive||document.hidden||this.inViewport===false||this.frame)return;this.frame=requestAnimationFrame(()=>{
    this.frame=null;if(!this.alive||document.hidden||this.inViewport===false)return;const r=this.host.getBoundingClientRect();if(!r.width||!r.height)return;
    const dpr=Math.min(devicePixelRatio||1,2);this.renderer.resize(r.width,r.height,dpr);const {candles,start}=this.buffer.view(this.view.rightTime,this.view.bars);
    const key=JSON.stringify([r.width,r.height,dpr,this.themeRevision,this.log,this.quote,this.volumeUnit,this.view.bars,this.chartStyle,this.priceRange,this.rightPadding,start]);
    // CandleBuffer keeps unchanged candles immutable and reference-stable.
    // Avoid serializing every OHLC/study point and recomputing the entire scene
    // when an update is outside the viewport or a duplicate invalidate arrives.
    const changed=key!==this.sceneKey||this.results!==this.sceneResults||!this.scene||candles.length!==this.scene.candles.length||candles.some((c,i)=>c!==this.scene.candles[i]);
    if(changed){this.sceneKey=key;this.sceneResults=this.results;this.baseKey=(this.baseKey||0)+1;this.scene=this.renderer.scene(candles,start,this.results,this.log,this.quote,this.view.bars,{rightPadding:this.rightPadding,priceRange:this.priceRange,chartStyle:this.chartStyle,volumeUnit:this.volumeUnit});this.renderer.base(this.scene);this.cross();this.updateLegend()}
    this.paintAnnotations();this.host.dataset.baseFrames=this.renderer.metrics.baseFrames;this.host.dataset.visibleCandles=candles.length;this.host.dataset.candlesInMemory=this.buffer.data.length;
  })}
  paintAnnotations(){
    if(!this.scene||!this.alive||document.hidden||this.inViewport===false||this.annotationFrame)return;
    this.annotationFrame=requestAnimationFrame(()=>{this.annotationFrame=null;this.drawAnnotations()});
  }
  drawAnnotations(){
    if(!this.scene||!this.alive||document.hidden||this.inViewport===false)return;
    const drawings=this.drawingsVisible===false?[]:this.preview&&this.drag?.mode==='edit'?this.book.items.filter(d=>d.id!==this.preview.id):this.book.items;
    const overlays=[...this.orders.map(order=>order.kind==='practice-position'?{...order,label:`${order.label} · ${(((this.buffer.data.at(-1)?.close||order.entry)-order.entry)*order.quantity).toFixed(6)} ${this.quote} P/L`}:order),...(this.marketOverlays||[])];
    const key=JSON.stringify([this.baseKey,drawings,this.selected,this.preview,overlays]);if(key===this.annotationKey)return;this.annotationKey=key;
    this.renderer.annotations(this.scene,drawings,this.selected,this.drawingsVisible===false?null:this.preview,overlays);
  }
  setMarketOverlays(overlays){
    if(!Array.isArray(overlays)||overlays.length>100||overlays.some(o=>!Number.isFinite(o.price)||o.price<=0||typeof o.label!=='string'||o.label.length>160||(o.color&&!/^#[a-f0-9]{6}$/i.test(o.color))))throw Error('Invalid public market overlays');
    this.marketOverlays=overlays.map(({price,label,color})=>({price,label,color}));this.paintAnnotations();
  }
  cross(){if(!this.alive||document.hidden||this.inViewport===false||this.crossFrame)return;this.crossFrame=requestAnimationFrame(()=>{this.crossFrame=null;if(this.scene&&this.alive&&!document.hidden&&this.inViewport!==false){this.renderer.crosshair(this.scene,this.hover);this.host.dataset.crosshairFrames=this.renderer.metrics.crosshairFrames;this.updateLegend()}})}
  updateLegend(){if(!this.scene)return;const format=this.renderer.priceText?.bind(this.renderer)||priceText;const candles=this.scene.candles,c=this.hover?candles.find(c=>c.time===this.hover.time)||candles.at(-1):candles.at(-1);this.legend.textContent=c?`O ${format(c.open)}  H ${format(c.high)}  L ${format(c.low)}  C ${format(c.close)}`:''}
  point(e){if(!this.scene?.candles.length)return null;const time=this.scene.timeAt(e.offsetX);let price=this.scene.priceAt(Math.max(this.scene.top,Math.min(this.scene.bottom,e.offsetY)));if(this.magnet){const c=this.scene.candles.find(c=>c.time===time);if(c)price=[c.open,c.high,c.low,c.close].sort((a,b)=>Math.abs(this.scene.y(a)-e.offsetY)-Math.abs(this.scene.y(b)-e.offsetY))[0]}return time&&Number.isFinite(price)&&price>0?{time,price}:null}
  down(e){if(!this.scene||!this.point(e))return;e.preventDefault();this.layers[2].focus();this.layers[2].setPointerCapture(e.pointerId);this.emit('active',true);const point=this.point(e);
    if(e.offsetX>this.scene.right){this.drag={mode:'scale',y:e.offsetY,range:{min:this.scene.min,max:this.scene.max}};return}
    if(e.offsetY>this.scene.h-26){this.drag={mode:'time-scale',x:e.offsetX,bars:this.view.bars};return}
    if(this.tool!=='cursor'){if(e.offsetY>this.scene.bottom)return;const d={id:crypto.randomUUID(),type:this.tool,points:[point],color:this.options.drawingColor||'#9a88e3',text:this.options.noteText||'Note',locked:false};if(!['horizontal','vertical','text'].includes(d.type))d.points.push({...point});this.preview=d;this.drag={mode:'draw',start:point};this.paintAnnotations();return}
    const hit=this.drawingsVisible===false?null:[...this.book.items].reverse().map(d=>this.renderer.hit(this.scene,d,e.offsetX,e.offsetY)).find(Boolean);this.selected=hit?.id||null;this.emit('selection',this.selected);const drawing=this.book.items.find(d=>d.id===this.selected);
    this.drag=hit&&drawing&&!drawing.locked?{mode:'edit',original:clone(drawing),handle:hit.handle,start:point}:{mode:'pan',x:e.offsetX,end:this.view.rightTime?this.buffer.upperBound(this.view.rightTime):this.buffer.data.length};this.paintAnnotations()
  }
  move(e){const point=this.point(e);if(!point)return;this.hover={...point,y:e.offsetY};this.cross();this.emit('crosshair',point);
    if(!this.drag)return;
    if(this.drag.mode==='scale'){const {min,max}=this.drag.range,mid=(min+max)/2,half=(max-min)*Math.exp((e.offsetY-this.drag.y)/150)/2;this.priceRange={min:mid-half,max:mid+half};this.invalidate();return}
    if(this.drag.mode==='time-scale'){this.view.bars=Math.max(15,Math.min(600,this.drag.bars*Math.exp((e.offsetX-this.drag.x)/200)));this.invalidate();this.viewportChanged();return}
    if(this.drag.mode==='pan'){const delta=Math.round((e.offsetX-this.drag.x)/this.scene.step),end=Math.min(this.buffer.data.length,Math.max(Math.min(15,this.buffer.data.length),this.drag.end-delta));this.view.rightTime=end===this.buffer.data.length?null:this.buffer.data[end-1]?.time;this.invalidate();this.viewportChanged();return}
    if(this.drag.mode==='draw'){if(this.preview.points.length===2)this.preview.points[1]=point;else this.preview.points[0]=point;this.paintAnnotations();return}
    const d=clone(this.drag.original);if(this.drag.handle!=null)d.points[this.drag.handle]=point;else{const dt=point.time-this.drag.start.time,ratio=point.price/this.drag.start.price;d.points=d.points.map(p=>({time:p.time+dt,price:p.price*ratio}))}this.preview=d;this.paintAnnotations()
  }
  up(){if(!this.drag)return;try{if(this.drag.mode==='draw'&&this.preview){this.book.add(this.preview);this.selected=this.preview.id;this.emit('drawing',this.book.items)}if(this.drag.mode==='edit'&&this.preview){this.book.edit(this.preview.id,{points:this.preview.points});this.emit('drawing',this.book.items)}}catch(error){this.emit('error',error.message)}this.drag=null;this.preview=null;this.paintAnnotations();this.emit('selection',this.selected)}
  linkedCrosshair(point){this.hover=point;this.cross()}
  undo(){this.book.undo();this.selected=null;this.paintAnnotations();this.emit('drawing',this.book.items)}
  redo(){this.book.redo();this.paintAnnotations();this.emit('drawing',this.book.items)}
  deleteSelected(){if(!this.selected)return;this.book.remove(this.selected);if(!this.book.items.some(d=>d.id===this.selected))this.selected=null;this.paintAnnotations();this.emit('drawing',this.book.items)}
  lockSelected(){const d=this.book.items.find(d=>d.id===this.selected);if(d){this.book.edit(d.id,{locked:!d.locked});this.paintAnnotations();this.emit('drawing',this.book.items)}}
  editSelected(patch){if(this.selected){this.book.edit(this.selected,patch);this.paintAnnotations();this.emit('drawing',this.book.items)}}
  setTradingOverlays(items){if(!Array.isArray(items)||items.length>100||items.some(o=>!Number.isFinite(o.price)||o.price<=0||typeof o.label!=='string'||o.label.length>120))throw Error('Invalid trading overlays');this.orders=clone(items);this.paintAnnotations()}
  setPositions(positions){if(!Array.isArray(positions)||positions.some(p=>!Number.isFinite(p.entry)||p.entry<=0||!Number.isFinite(p.quantity)||p.quantity<=0||!Number.isFinite(p.pnl)||!['long','short'].includes(p.side)))throw Error('Invalid verified position state');this.setTradingOverlays([...this.orders.filter(o=>o.kind!=='position'),...positions.map(p=>({kind:'position',price:p.entry,label:`${p.side} · ${p.quantity} · ${p.pnl.toFixed(6)} ${this.quote} P/L`,color:p.side==='long'?'#6d9ed6':'#d8929e'}))])}
  serialize(){return {interval:this.interval,chartStyle:this.chartStyle||'candles',log:this.log,studies:[...this.studies],drawings:clone(this.book.items),view:{...this.view}}}
  diagnostics(){const m=this.renderer.metrics,sorted=[...m.baseMs].sort((a,b)=>a-b);return {...m,baseMs:undefined,annotationMs:undefined,candlesInMemory:this.buffer.data.length,visibleCandles:this.scene?.candles.length||0,baseP95Ms:sorted[Math.floor(sorted.length*.95)]||0}}
  destroy(){if(!this.alive)return;this.alive=false;this.emit('destroy',undefined);cancelAnimationFrame(this.frame);cancelAnimationFrame(this.crossFrame);cancelAnimationFrame(this.annotationFrame);this.resize.disconnect();this.intersection?.disconnect();document.removeEventListener('visibilitychange',this.visibility);for(const [t,n,f,o] of this.handlers)t.removeEventListener(n,f,o);this.renderer.destroy();this.legend.remove();this.empty.remove();this.listeners.clear()}
}
