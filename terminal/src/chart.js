import {parseInterval} from './intervals.js';
import {DrawingLayer} from './drawings.js';
import {normalizeIndicators,computeIndicators,paintIndicators,sameIndicatorInput} from './indicators.js';
import {TrackPaintLayer,parseTrackPaint} from './track-paint.js';
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const niceStep=x=>{const p=10**Math.floor(Math.log10(x)),n=x/p;return (n<1.5?1:n<2.25?2:n<3.75?2.5:n<7.5?5:10)*p;};
function precision(step){const power=Math.floor(Math.log10(step)),mantissa=step/10**power;return clamp(-power+(Math.abs(mantissa-2.5)<1e-7?1:0),0,14);}
// The locale formatter is expensive to construct. There are at most 15 valid
// precisions, shared across panes; values themselves are never rounded in data.
const priceFormats=new Map();
function formatPrice(p,n){if(Math.abs(p)>0&&Math.abs(p)<1e-12)return p.toExponential(5);let format=priceFormats.get(n);if(!format){format=new Intl.NumberFormat('en-US',{minimumFractionDigits:n,maximumFractionDigits:n});priceFormats.set(n,format);}return format.format(p);}

/** Price/volume pane, independent axes and a separate crosshair layer. */
export class TickChart {
 constructor(container,token,options={}){
  this.container=container;this.token={...token};this.options={interval:'1h',mode:'candles',theme:'light',compact:false,average:false,...options};
  this.studyKey=`ticking-indicators-v1:${this.options.storageScope||'workspace'}`;
  try{this.options.indicators=normalizeIndicators(options.indicators??(this.options.editable?JSON.parse(localStorage.getItem(this.studyKey)||'[]'):[]));}catch{this.options.indicators=[];}
  if(this.options.average&&!this.options.indicators.some(s=>s.id==='sma'))this.options.indicators.push({id:'sma',period:9});this.options.average=this.options.indicators.some(s=>s.id==='sma');
  this.options.volume=options.volume!==false;
  if(this.options.editable&&options.volume===undefined)try{this.options.volume=localStorage.getItem(this.studyKey+':volume')!=='false';}catch{}
  this.count=this.options.compact?55:88;this.offset=0;this.pointer=null;this.drawingLayer=null;this.frame=0;this.crosshairFrame=0;this.destroyed=false;this.renderStats={baseFrames:0,crosshairFrames:0};this.touches=new Map();
  this.canvas=document.createElement('canvas');this.canvas.tabIndex=0;this.canvas.setAttribute('role','img');this.canvas.setAttribute('aria-label',`${token.symbol} price chart. Scroll to zoom; drag to pan. Drag the price axis to scale. Arrow keys inspect candles.`);
  this.overlay=document.createElement('canvas');this.overlay.setAttribute('aria-hidden','true');Object.assign(this.overlay.style,{position:'absolute',inset:'0',pointerEvents:'none'});
  this.tooltip=document.createElement('div');this.tooltip.className='chart-tooltip';this.tooltip.style.pointerEvents='none';
  this.legend=document.createElement('div');this.legend.className='tick-chart-legend';this.legend.setAttribute('aria-label','Candle values');
  Object.assign(this.legend.style,{position:'absolute',top:'8px',left:'10px',pointerEvents:'none',font:'500 11px "DM Sans",sans-serif',lineHeight:'1.6',fontVariantNumeric:'tabular-nums',whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',maxWidth:'calc(100% - 98px)'});
  this.legendName=document.createElement('div');this.legendValues=document.createElement('div');this.legend.append(this.legendName,this.legendValues);
  Object.assign(this.legendValues.style,{whiteSpace:'pre',overflow:'hidden',textOverflow:'ellipsis'});
  this.navigation=document.createElement('div');Object.assign(this.navigation.style,{position:'absolute',bottom:'35px',display:'flex',gap:'5px'});
  const navButton=(label,title)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.title=title;b.setAttribute('aria-label',title);Object.assign(b.style,{font:'500 10px "DM Sans",sans-serif',padding:'5px 8px',borderRadius:'4px',border:'1px solid #7564c340',cursor:'pointer'});this.navigation.append(b);return b;};
  this.latestButton=navButton('Latest →','Return to latest available candle');this.autoButton=navButton('Auto','Reset price scale');
  if(getComputedStyle(container).position==='static')container.style.position='relative';
  container.replaceChildren(this.canvas,this.overlay,this.legend,this.tooltip,this.navigation);this.abort=new AbortController();const opts={signal:this.abort.signal};this.canvas.style.touchAction='none';this.canvas.style.cursor='crosshair';
  this.latestButton.addEventListener('click',()=>this.goToLatest(),opts);this.autoButton.addEventListener('click',()=>{this.manualScale=null;this.autoScale=null;this.requestDraw();},opts);
  // Touch gestures run before the drawing handlers. A second finger takes
  // over only a selection/pan gesture; it never commits an accidental drawing.
  for(const type of ['pointerdown','pointermove','pointerup','pointercancel'])this.canvas.addEventListener(type,e=>this.touchGesture(e),{...opts,capture:true});
  this.resizeObserver=new ResizeObserver(()=>this.requestDraw());this.resizeObserver.observe(container);
  // Keep incoming bars, but do not spend main-thread paint/indicator work on a
  // chart outside the viewport. Re-entry paints the latest retained frame.
  this.inViewport=true;
  if(typeof IntersectionObserver!=='undefined'){this.intersectionObserver=new IntersectionObserver(entries=>{const entry=entries.at(-1);if(!entry)return;const visible=entry.isIntersecting;const changed=visible!==this.inViewport;this.inViewport=visible;if(visible&&changed)this.requestDraw();});this.intersectionObserver.observe(container);}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)this.requestDraw();},{signal:this.abort.signal});
  this.canvas.addEventListener('pointermove',e=>{const r=this.canvas.getBoundingClientRect();this.pointer={x:e.clientX-r.left,y:e.clientY-r.top};
   if(this.axisDrag){const d=this.axisDrag;if(d.kind==='price'){const span=d.span*Math.exp(clamp((this.pointer.y-d.start.y)*.008,-6,6));this.manualScale={min:d.center-span/2,max:d.center+span/2};}else this.resizeView(d.count*Math.exp(clamp((d.start.x-this.pointer.x)*.004,-3,3)),1);this.requestDraw();}
   else if(this.panDrag){this.offset=this.clampOffset(this.panDrag.offset+(this.pointer.x-this.panDrag.x)/this.view.step);this.requestDraw();}
   else this.requestCrosshair();
  },opts);
  this.canvas.addEventListener('pointerleave',()=>{if(!this.axisDrag&&!this.panDrag)this.pointer=null;this.requestCrosshair();},opts);
  this.canvas.addEventListener('pointerdown',e=>{if(e.button!==0||!this.view||this.options.compact)return;const r=this.canvas.getBoundingClientRect(),p={x:e.clientX-r.left,y:e.clientY-r.top};
   if(p.x>=this.plotWidth)this.axisDrag={kind:'price',start:p,center:(this.max+this.min)/2,span:this.max-this.min};
   else if(p.y>=this.view.axisTop)this.axisDrag={kind:'time',start:p,count:this.count};
   else if(!this.drawingLayer)this.panDrag={x:p.x,offset:this.offset};
   if(this.axisDrag||this.panDrag){e.preventDefault();this.canvas.setPointerCapture(e.pointerId);}
  },opts);
  const end=e=>{this.axisDrag=null;this.panDrag=null;if(this.canvas.hasPointerCapture(e.pointerId))this.canvas.releasePointerCapture(e.pointerId);this.requestCrosshair();};
  this.canvas.addEventListener('pointerup',end,opts);this.canvas.addEventListener('pointercancel',end,opts);
  this.canvas.addEventListener('dblclick',e=>{const r=this.canvas.getBoundingClientRect();if(e.clientX-r.left>=this.plotWidth){this.manualScale=null;this.autoScale=null;this.requestDraw();}else if(e.clientY-r.top>=this.view?.axisTop)this.reset();},opts);
  this.canvas.addEventListener('wheel',e=>{
   if(this.options.compact||!this.visible?.length||this.drawingLayer?.drag||this.drawingLayer?.pending)return;
   e.preventDefault();const units=e.deltaMode===1?16:e.deltaMode===2?this.height:1,dx=e.deltaX*units,dy=e.deltaY*units,r=this.canvas.getBoundingClientRect();
   if(e.clientX-r.left>=this.plotWidth){const center=(this.min+this.max)/2,span=(this.max-this.min)*Math.exp(clamp(dy*.002,-1,1));this.manualScale={min:center-span/2,max:center+span/2};}
   else if(!e.ctrlKey&&!e.metaKey&&(e.shiftKey||Math.abs(dx)>Math.abs(dy)))this.offset=this.clampOffset(this.offset+(e.shiftKey&&!dx?dy:dx)/this.view.step);
   else this.resizeView(this.count*Math.exp(clamp(dy*(e.ctrlKey?.003:.0015),-.8,.8)),clamp((e.clientX-r.left)/this.plotWidth,0,1));
   this.requestDraw();
  },{...opts,passive:false});
  this.canvas.addEventListener('keydown',e=>{
   if(e.key==='+'||e.key==='='){e.preventDefault();this.zoom(1);}
   if(e.key==='-'){e.preventDefault();this.zoom(-1);}
   if(this.visible?.length&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){e.preventDefault();this.pointer={x:clamp((this.pointer?.x??this.plotWidth/2)+(e.key==='ArrowLeft'?-this.view.step:this.view.step),0,this.plotWidth-1),y:this.height/2};this.requestCrosshair();}
   if(e.key==='Escape'){this.pointer=null;this.requestCrosshair();}
  },opts);
  // Load saved drawings before the first paint rather than rendering the same
  // chart twice. The synchronous view remains available to existing embeds.
  if(this.options.editable)this.drawingLayer=new DrawingLayer(this);this.draw();
 }
 get drawings(){return this.drawingLayer?.items||[];}
 set(options){if(options.interval)options.interval=parseInterval(options.interval).label;if('average' in options&&!('indicators' in options))options.indicators=options.average?this.options.indicators.some(s=>s.id==='sma')?this.options.indicators:[...this.options.indicators,{id:'sma',period:9}]:this.options.indicators.filter(s=>s.id!=='sma');if(options.indicators){options.indicators=normalizeIndicators(options.indicators);options.average=options.indicators.some(s=>s.id==='sma');}const changed=options.interval&&options.interval!==this.options.interval;this.options={...this.options,...options};if(changed){this.offset=0;this.manualScale=null;}if(options.indicators||'volume' in options){if(this.options.editable)try{localStorage.setItem(this.studyKey,JSON.stringify(this.options.indicators));localStorage.setItem(this.studyKey+':volume',String(this.options.volume));}catch{}this.container.dispatchEvent(new CustomEvent('indicatorchange'));}this.requestDraw();}
 setSeries(series,{preserve=true}={}){const anchor=preserve&&this.offset>0?this.series?.[Math.max(0,this.series.length-1-Math.floor(this.offset))]?.time:null,fraction=this.offset-Math.floor(this.offset);this.options={...this.options,series};if(anchor!=null){const i=series.findIndex(b=>b.time>=anchor);if(i>=0)this.offset=Math.max(0,series.length-1-i+fraction);}else if(!preserve){this.offset=0;this.axisWidth=null;this.autoScale=null;this.count=Math.min(this.options.compact?55:88,Math.max(24,series.length+6));}this.requestDraw();}
 clampOffset(offset){return clamp(offset,0,Math.max(0,(this.series?.length||0)-1));}
 resizeView(count,anchor=1){const old=this.count,next=clamp(count,15,1500);this.offset=this.clampOffset(this.offset+(old-next)*(1-anchor));this.count=next;}
 zoom(direction){this.resizeView(this.count*Math.exp(-direction*.18));this.requestDraw();}
 pan(direction){this.offset=this.clampOffset(this.offset+direction*12);this.requestDraw();}
 reset(){this.count=this.options.compact?55:88;this.offset=0;this.manualScale=null;this.autoScale=null;this.requestDraw();}
 goToLatest(){this.offset=0;this.manualScale=null;this.autoScale=null;this.pointer=null;this.requestDraw();this.container.dispatchEvent(new CustomEvent('latestrequest'));}
 setTrackPaint(paint,hooks){if(!this.trackLayer)this.trackLayer=new TrackPaintLayer(this);if(hooks)this.trackLayer.setHooks(hooks);const n=this.series?.length||this.options.series?.length||0;if(n>8){this.count=Math.min(480,n+2);this.offset=0;}this.trackLayer.setPaint(parseTrackPaint(paint));}
 addLevel(){this.drawingLayer?.setTool('horizontal');}
 yToPrice(y){return this.max-(y-this.priceTop)/this.priceHeight*(this.max-this.min);}
 touchGesture(e){
  if(e.pointerType!=='touch'||this.options.compact||!this.view)return;
  const r=this.canvas.getBoundingClientRect(),p={x:e.clientX-r.left,y:e.clientY-r.top};
  if(e.type==='pointerdown'){
   this.touches.set(e.pointerId,p);
   if(this.touches.size===2&&(!this.drawingLayer||this.drawingLayer.tool==='select'&&!this.drawingLayer.pending&&(!this.drawingLayer.drag||this.drawingLayer.drag.kind==='pan'))){
    const [a,b]=[...this.touches.values()],mid=(a.x+b.x)/2;if([a,b].some(p=>p.x<0||p.x>=this.plotWidth||p.y<0||p.y>=this.view.axisTop))return;
    this.pinch={distance:Math.max(10,Math.hypot(a.x-b.x,a.y-b.y)),mid,count:this.count,offset:this.offset,width:this.plotWidth};this.touchBlocked=true;this.panDrag=null;this.axisDrag=null;this.pointer=null;
    if(this.drawingLayer)this.drawingLayer.drag=null;
    this.canvas.setPointerCapture(e.pointerId);
   }
  }else if(e.type==='pointermove'){
   if(this.touches.has(e.pointerId))this.touches.set(e.pointerId,p);
   if(this.pinch&&this.touches.size===2){
    const [a,b]=[...this.touches.values()],g=this.pinch,mid=(a.x+b.x)/2,next=clamp(g.count*g.distance/Math.max(10,Math.hypot(a.x-b.x,a.y-b.y)),15,1500);
    this.count=next;this.offset=this.clampOffset(g.offset+(g.count-next)*(1-clamp(g.mid/g.width,0,1))+(mid-g.mid)/(g.width/next));this.requestDraw();
   }
  }else this.touches.delete(e.pointerId);
  if(this.touchBlocked){
   e.preventDefault();e.stopImmediatePropagation();
   if(e.type==='pointerup'||e.type==='pointercancel'){this.pinch=null;if(this.canvas.hasPointerCapture(e.pointerId))this.canvas.releasePointerCapture(e.pointerId);}
   if(!this.touches.size){this.touchBlocked=false;this.requestDraw();this.requestCrosshair();}
  }
 }
 requestDraw(){if(this.destroyed||document.hidden||!this.inViewport||this.frame)return;this.frame=requestAnimationFrame(()=>{this.frame=0;if(!this.destroyed&&!document.hidden&&this.inViewport)this.draw();});}
 requestCrosshair(){if(this.destroyed||document.hidden||!this.inViewport||this.crosshairFrame)return;this.crosshairFrame=requestAnimationFrame(()=>{this.crosshairFrame=0;if(!this.destroyed&&!document.hidden&&this.inViewport)this.drawCrosshair();});}
 draw(){
  if(this.destroyed||document.hidden||!this.inViewport)return;
  const width=this.container.clientWidth,height=this.container.clientHeight;if(!width||!height)return;this.renderStats.baseFrames++;
  this.height=height;const dpr=Math.min(devicePixelRatio||1,3,Math.sqrt(8000000/(width*height)));this.dpr=dpr;
  for(const canvas of [this.canvas,this.overlay]){const w=Math.round(width*dpr),h=Math.round(height*dpr);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;canvas.style.width=width+'px';canvas.style.height=height+'px';}}
  const ctx=this.canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
  const dark=this.options.theme==='dark',bg=dark?'#1a1e2d':'#fff',grid=dark?'#2b3040':'#eef0f5',text=dark?'#a0a9bd':'#798395',up='#239a82',down='#d36a79',accent='#7564c3';this.colors={dark,bg,grid,text,up,down};
  ctx.fillStyle=bg;ctx.fillRect(0,0,width,height);this.series=this.options.series??[];this.offset=this.clampOffset(this.offset);
  const compact=this.options.compact,gap=compact?3:6,logicalStart=this.series.length-this.offset-this.count+gap,end=Math.min(this.series.length,Math.max(1,Math.ceil(this.series.length-this.offset)+1)),begin=Math.max(0,Math.floor(logicalStart)-1);
  this.visible=this.series.slice(begin,end);
  if(!this.visible.length){this.view=null;this.legend.hidden=true;this.navigation.style.display='none';this.tooltip.style.display='none';this.overlay.getContext('2d').clearRect(0,0,this.overlay.width,this.overlay.height);ctx.fillStyle=text;ctx.font='14px "DM Sans",sans-serif';ctx.textAlign='center';ctx.fillText(this.options.emptyMessage||'No chart data available.',width/2,height/2);this.trackLayer?.sync();return;}
  const period=parseInterval(this.engineData?.interval||this.options.interval).milliseconds,configs=this.options.indicators,key=JSON.stringify(configs)+':'+period;
  if(this.studyCacheKey!==key||configs.length&&this.studySeries!==this.series&&!sameIndicatorInput(this.studySeries,this.series)){this.studies=computeIndicators(this.series,configs,period);this.studyCacheKey=key;}
  this.studySeries=this.series;
  const paneHeight=Math.max(68,Math.min(110,height*.19)),oscillators=compact?[]:this.studies.filter(s=>s.definition.pane);oscillators.forEach(s=>s.paneHeight=paneHeight);
  const data=this.visible;let min=Infinity,max=-Infinity,maxVol=Number.MIN_VALUE;
  for(const bar of data){min=Math.min(min,bar.low);max=Math.max(max,bar.high);maxVol=Math.max(maxVol,bar.volume);}
  for(const study of this.studies){if(study.definition.pane)continue;for(const line of Object.values(study.lines))for(let i=begin;i<Math.min(end,line.length);i++){const value=line[i];if(value!==null){min=Math.min(min,value);max=Math.max(max,value);}}}
  const reference=Math.abs(data.at(-1).close),range=Math.max(max-min,reference*1e-6,Number.MIN_VALUE);
  const scaleKey=[data[0].time,data.at(-1).time,this.offset,this.count,height,key,this.options.volume].join(':');
  // An update to the open candle should not make the entire pane breathe.
  // Expand to include new extremes; fit again when the viewport changes.
  const target={min:min-range*.12,max:max+range*.12};
  if(this.autoScale?.key===scaleKey){target.min=Math.min(target.min,this.autoScale.min);target.max=Math.max(target.max,this.autoScale.max);}
  this.autoScale={...target,key:scaleKey};this.min=this.manualScale?.min??target.min;this.max=this.manualScale?.max??target.max;
  const tickCount=Math.max(3,Math.min(7,Math.floor((height-30-oscillators.length*paneHeight)*(this.options.volume?.76:1)/36))),tick=niceStep((this.max-this.min)/tickCount),digits=precision(tick);
  const priceDigits=Math.max(digits,reference>=1?2:Math.min(12,Math.max(2,5-Math.floor(Math.log10(reference||1)))));
  this.formatPrice=p=>formatPrice(p,priceDigits);const axisPrice=p=>formatPrice(p,digits);
  const fontSize=compact?10:width<450?11:12;ctx.font=`500 ${fontSize}px "DM Sans",sans-serif`;ctx.textBaseline='middle';
  const measured=Math.max(ctx.measureText(this.formatPrice(this.min)).width,ctx.measureText(this.formatPrice(this.max)).width)+18;
  this.axisWidth=Math.max(this.axisWidth||0,Math.ceil(measured/12)*12,84);const right=compact?58:Math.min(this.axisWidth,Math.max(76,Math.min(144,width*.34))),bottom=30,plotWidth=Math.max(40,width-right),plotHeight=height-bottom;
  const mainHeight=plotHeight-oscillators.length*paneHeight;
  this.plotWidth=plotWidth;this.priceTop=compact?18:44;this.priceHeight=Math.max(30,mainHeight*(this.options.volume?(compact?.83:.76):1)-this.priceTop-(!this.options.volume?8:0));const priceBottom=this.priceTop+this.priceHeight,volumeTop=priceBottom+18;
  this.navigation.style.display=compact?'none':'flex';this.navigation.style.right=(right+8)+'px';this.latestButton.hidden=this.offset<.5;this.autoButton.hidden=!this.manualScale;
  for(const b of [this.latestButton,this.autoButton]){b.style.display=b.hidden?'none':'inline-flex';b.style.background=bg;b.style.color=accent;}
  this.legend.hidden=compact;this.legend.style.color=text;this.legend.style.background=bg+'e8';this.legend.style.maxWidth=Math.max(0,plotWidth-20)+'px';
  const y=p=>this.priceTop+(this.max-p)/(this.max-this.min)*this.priceHeight,step=plotWidth/this.count,left=begin-logicalStart,x=i=>(left+i+.5)*step;
  const indexAt=time=>{if(time<data[0].time)return (time-data[0].time)/period;let lo=0,hi=data.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(data[mid].time<=time)lo=mid;else hi=mid-1;}const next=data[lo+1];return lo+(time-data[lo].time)/(next?next.time-data[lo].time:period);};
  const timeAt=index=>{if(index<0)return data[0].time+index*period;const i=Math.min(data.length-1,Math.floor(index)),next=data[i+1];return data[i].time+(index-i)*(next?next.time-data[i].time:period);};
  const candleAtX=p=>{const i=Math.floor(p/step-left);return i>=0&&i<data.length?data[i]:null;};
  this.view={width:plotWidth,height:mainHeight,axisTop:plotHeight,step,period,series:this.series,price:this.formatPrice,candleAtX,x,y,indexAt,left,value:p=>this.yToPrice(p),screen:p=>({x:x(indexAt(p.time)),y:y(p.price)}),anchor:p=>({time:timeAt(p.x/step-left-.5),price:this.yToPrice(p.y)})};
  ctx.textAlign='left';
  const crisp=p=>(Math.round(p*dpr)+.5)/dpr;
  for(let val=Math.ceil(this.min/tick)*tick,n=0;val<=this.max+tick*1e-8&&n<12;val+=tick,n++){const gy=crisp(y(val));ctx.strokeStyle=grid;ctx.lineWidth=1/dpr;ctx.beginPath();ctx.moveTo(0,gy);ctx.lineTo(plotWidth,gy);ctx.stroke();ctx.fillStyle=text;ctx.fillText(axisPrice(val),plotWidth+8,gy);}
  ctx.strokeStyle=grid;ctx.beginPath();ctx.moveTo(plotWidth,0);ctx.lineTo(plotWidth,plotHeight);ctx.moveTo(0,plotHeight);ctx.lineTo(width,plotHeight);ctx.stroke();
  const ticks=Math.max(2,Math.min(10,Math.floor(plotWidth/100))),stride=Math.max(1,Math.ceil(this.count/ticks)),rawStride=stride*period,timeStride=[1000,3000,5000,15000,30000,45000,60000,180000,300000,900000,1800000,3600000,10800000,21600000,43200000,86400000,259200000,604800000,2592000000,7776000000,31536000000].find(s=>s>=rawStride)||Math.ceil(rawStride/31536000000)*31536000000;let lastLabelRight=-100,lastTickDay=null,lastBucket=null;this.timeAxisTicks=[];
  // Axis labels belong to observed bar coordinates. Empty space to the left of
  // sparse history must not imply months/years of retained candles.
  for(let index=0;index<data.length;index++){const time=data[index].time,bucket=Math.floor(time/timeStride);if(bucket===lastBucket)continue;lastBucket=bucket;const gx=x(index),date=new Date(time),span=data.at(-1).time-data[0].time;
   if(index===0&&time%timeStride>=period)continue;
   const iso=date.toISOString(),day=iso.slice(0,10),label=period>=604800000||span>31536000000?iso.slice(0,7):period>=86400000?iso.slice(5,10):span>86400000&&day!==lastTickDay?iso.slice(5,10):period<60000&&span<86400000?iso.slice(11,19):iso.slice(11,16),tw=ctx.measureText(label).width;
   if(gx-tw/2<lastLabelRight+16||gx-tw/2<0||gx+tw/2>plotWidth)continue;lastLabelRight=gx+tw/2;lastTickDay=day;this.timeAxisTicks.push({time,label,x:gx});
   ctx.strokeStyle=grid;ctx.lineWidth=1/dpr;ctx.beginPath();ctx.moveTo(crisp(gx),this.priceTop);ctx.lineTo(crisp(gx),plotHeight);ctx.stroke();ctx.fillStyle=text;ctx.textAlign='center';ctx.fillText(label,gx,height-13);
  }
  ctx.textAlign='left';
  const wickPixels=Math.max(1,Math.min(Math.floor(dpr),Math.floor(step*dpr)));let bodyPixels=Math.max(wickPixels,Math.min(Math.floor(28*dpr),Math.floor(step*.68*dpr)));if(bodyPixels>=2&&bodyPixels%2!==wickPixels%2)bodyPixels--;const body=bodyPixels/dpr;this.renderGeometry={wickPixels,bodyPixels,dpr};
  ctx.save();ctx.beginPath();ctx.rect(0,volumeTop,plotWidth,Math.max(0,mainHeight-volumeTop));ctx.clip();
  if(this.options.volume){const groups=new Map(),batch=step*dpr>=bodyPixels;data.forEach((c,i)=>{const color=c.close>=c.open?(dark?'#255449':'#d8eee8'):(dark?'#593946':'#f0dee2'),vh=Math.round(c.volume/maxVol*Math.max(0,mainHeight-volumeTop-5)*dpr)/dpr,px=(Math.round(x(i)*dpr)+(wickPixels%2?.5:0))/dpr;if(batch){if(!groups.has(color))groups.set(color,new Path2D());groups.get(color).rect(px-body/2,mainHeight-vh,body,vh);}else{ctx.fillStyle=color;ctx.fillRect(px-body/2,mainHeight-vh,body,vh);}});for(const [color,path] of groups){ctx.fillStyle=color;ctx.fill(path);}}ctx.restore();
  ctx.save();ctx.beginPath();ctx.rect(0,0,plotWidth,priceBottom+2);ctx.clip();
  if(this.options.mode==='line'){ctx.beginPath();data.forEach((c,i)=>i?ctx.lineTo(x(i),y(c.close)):ctx.moveTo(x(i),y(c.close)));ctx.strokeStyle=accent;ctx.lineWidth=2;ctx.stroke();ctx.lineTo(x(data.length-1),priceBottom);ctx.lineTo(x(0),priceBottom);ctx.closePath();const gradient=ctx.createLinearGradient(0,18,0,priceBottom);gradient.addColorStop(0,dark?'#6566ea30':'#6566ea15');gradient.addColorStop(1,'#6566ea00');ctx.fillStyle=gradient;ctx.fill();}
  // Color expresses price direction; reference quality remains in the hover
  // disclosure. Only a carried, unobserved close uses the neutral color.
  else{
   const glyphs=data.map((c,i)=>{const px=(Math.round(x(i)*dpr)+(wickPixels%2?.5:0))/dpr,top=Math.round(Math.min(y(c.open),y(c.close))*dpr)/dpr,bottomY=Math.round(Math.max(y(c.open),y(c.close))*dpr)/dpr;return {px,top,height:Math.max(1/dpr,bottomY-top),high:Math.round(y(c.high)*dpr)/dpr,low:Math.round(y(c.low)*dpr)/dpr,color:c.synthetic?text:c.close>=c.open?up:down};});
   if(step*dpr>=wickPixels){
    // Disjoint pixel columns can be submitted in three color batches without
    // changing candle values or paint order at any shared pixel.
    const groups=new Map();for(const g of glyphs){if(!groups.has(g.color))groups.set(g.color,{wicks:new Path2D(),bodies:new Path2D()});const p=groups.get(g.color);p.wicks.moveTo(g.px,g.high);p.wicks.lineTo(g.px,g.low);p.bodies.rect(g.px-body/2,g.top,body,g.height);}
    ctx.lineWidth=wickPixels/dpr;for(const [color,p] of groups){ctx.strokeStyle=color;ctx.fillStyle=color;ctx.stroke(p.wicks);ctx.fill(p.bodies);}
   }else{
    // Below one physical pixel, retain chronological overdraw rather than
    // allowing a grouped color to imply the last candle's direction.
    for(const g of glyphs){ctx.strokeStyle=g.color;ctx.fillStyle=g.color;ctx.lineWidth=wickPixels/dpr;ctx.beginPath();ctx.moveTo(g.px,g.high);ctx.lineTo(g.px,g.low);ctx.stroke();ctx.fillRect(g.px-body/2,g.top,body,g.height);}
   }
  }
  const last=this.series.at(-1),lastY=y(last.close),lastColor=last.synthetic?text:last.close>=last.open?up:down;ctx.setLineDash([3,3]);ctx.strokeStyle=lastColor+'90';ctx.beginPath();ctx.moveTo(0,lastY);ctx.lineTo(plotWidth,lastY);ctx.stroke();ctx.setLineDash([]);ctx.restore();
  // Today's price must not sit on an unrelated historical scale boundary.
  if(lastY>=this.priceTop&&lastY<=priceBottom){const pillY=clamp(lastY,12,priceBottom-12);ctx.fillStyle=lastColor;ctx.fillRect(plotWidth,pillY-12,right,24);ctx.fillStyle='#fff';ctx.font=`500 ${fontSize}px "DM Sans",sans-serif`;ctx.fillText(this.formatPrice(last.close),plotWidth+7,pillY);}
  this.indicatorPanes=paintIndicators(ctx,compact?this.studies.filter(s=>!s.definition.pane):this.studies,{begin,end,x,y,width:plotWidth,top:this.priceTop,bottom:priceBottom,dark,grid,text,format:this.formatPrice,volumeBottom:mainHeight});
  const transition=this.engineData?.history?.sourceTransition*1000;
  if(Number.isFinite(transition)&&transition>=data[0].time&&transition<=data.at(-1).time+period){const seam=x(indexAt(transition));ctx.save();ctx.strokeStyle=accent+'80';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(seam,this.priceTop);ctx.lineTo(seam,plotHeight);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=accent;ctx.font='500 10px "DM Sans",sans-serif';ctx.fillText('Solana observations →',clamp(seam+6,5,Math.max(5,plotWidth-130)),this.priceTop+10);ctx.restore();}
  this.drawingLayer?.paint(ctx);ctx.fillStyle=dark?'#31374a':'#eceef4';ctx.font=`700 ${compact?22:32}px "DM Sans",sans-serif`;ctx.fillText('ticking',15,mainHeight-16);this.trackLayer?.sync();this.drawCrosshair();
  const rangeKey=[begin,end,this.series[0]?.time,this.series.at(-1)?.time].join(':');if(rangeKey!==this.rangeKey){this.rangeKey=rangeKey;this.container.dispatchEvent(new CustomEvent('viewchange',{detail:{begin,end,total:this.series.length}}));}
 }
 updateLegend(c){
  if(!c||!this.formatPrice)return;
  const change=c.open?(c.close/c.open-1)*100:0,quality=c.synthetic?'Carried close':c.estimated?'Reconstructed reference':c.referenceOnly?'Reference price':this.engineData?.aggregation==='token'?'Token reference':'Recorded candle';
  const name=`${this.token.symbol} · ${this.options.interval}${this.engineData?.quote?' · '+this.engineData.quote:''} · Vol ${formatPrice(c.volume,c.volume>0&&c.volume<1?6:2)}`;if(this.legendName.textContent!==name)this.legendName.textContent=name;
  const value=`O ${this.formatPrice(c.open)}  H ${this.formatPrice(c.high)}  L ${this.formatPrice(c.low)}  C ${this.formatPrice(c.close)}  ${change>=0?'+':''}${change.toFixed(2)}%`;
  if(this.legendValues.textContent!==value)this.legendValues.textContent=value;
  this.legendValues.style.color=c.synthetic?this.colors.text:c.close>=c.open?this.colors.up:this.colors.down;
  this.legend.title=`${new Date(c.time).toISOString().replace('T',' ').slice(0,19)} UTC · ${quality} · Volume ${c.volume} · ${c.quoteCurrency||this.engineData?.quote||'native quote'}`;
 }
 drawCrosshair(){
  if(!this.overlay.width)return;this.renderStats.crosshairFrames++;const ctx=this.overlay.getContext('2d'),dpr=this.dpr||1;ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,this.overlay.width,this.overlay.height);this.tooltip.style.display='none';
  const pointed=this.pointer&&this.view&&this.pointer.x>=0&&this.pointer.x<this.view.width&&this.pointer.y>=0&&this.pointer.y<this.view.axisTop?this.view.candleAtX(this.pointer.x):null;this.updateLegend(pointed||this.visible?.at(-1));
  if(!this.pointer||!this.view||this.axisDrag||this.panDrag||this.drawingLayer?.drag||this.drawingLayer?.pending||this.options.compact)return;
  const p=this.pointer,v=this.view;if(p.x<0||p.x>=v.width||p.y<0||p.y>=v.axisTop)return;
  const c=v.candleAtX(p.x),cx=c?v.screen({time:c.time,price:c.close}).x:p.x,crisp=n=>(Math.round(n*dpr)+.5)/dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.strokeStyle=this.colors.dark?'#8791a8':'#929bad';ctx.lineWidth=1/dpr;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(crisp(cx),this.priceTop);ctx.lineTo(crisp(cx),v.axisTop);ctx.moveTo(0,crisp(p.y));ctx.lineTo(v.width,crisp(p.y));ctx.stroke();ctx.setLineDash([]);ctx.font=`500 ${this.view.width<450?11:12}px "DM Sans",sans-serif`;ctx.textBaseline='middle';
  const pane=this.indicatorPanes?.find(s=>p.y>=s.top&&p.y<s.bottom),py=clamp(p.y,11,this.height-39),priceLabel=pane?pane.id==='rsi'?clamp(pane.value(p.y),0,100).toFixed(2):this.formatPrice(pane.value(p.y)):this.formatPrice(this.yToPrice(p.y));ctx.fillStyle=this.colors.dark?'#596078':'#7b8496';ctx.fillRect(v.width,py-10,this.container.clientWidth-v.width,20);ctx.fillStyle='#fff';ctx.fillText(priceLabel,v.width+7,py);
  const time=c?.time??v.anchor(p).time,label=new Date(time).toISOString().slice(v.period>=86400000?0:5,19).replace('T',' '),tw=ctx.measureText(label).width+16,tx=clamp(cx-tw/2,0,Math.max(0,v.width-tw));ctx.fillStyle=this.colors.dark?'#596078':'#7b8496';ctx.fillRect(tx,v.axisTop,tw,27);ctx.fillStyle='#fff';ctx.fillText(label,tx+8,v.axisTop+13);
  if(c){this.tooltip.style.display='block';this.tooltip.style.left='8px';this.tooltip.style.top='46px';this.tooltip.style.maxWidth=(this.plotWidth-16)+'px';this.tooltip.style.whiteSpace='normal';const names={binance_spot:'Binance',binance_spot_gap:'Binance gap archive',binance_alpha_gap:'Binance Alpha gap archive',binance_alpha:'Binance Alpha',solana_pool_mark:'Pool state · indicative',geckoterminal_pool_reference:'Pool history · reference',solana_reference:'Solana',gate_spot_reference:'Gate reference',alpha_spot_reference:'Alpha reference',bybit_spot_reference:'Bybit reference',okx_spot_reference:'OKX reference',okx:'OKX',gate:'Gate',mexc:'MEXC',binance_spot_reference:'Binance reference',solana_wrapped_reference:'Solana wrapped reference',ticking_reference_reconstruction:'Reconstructed reference'},source=c.sources?.map(s=>names[s]||s).join(' + '),quality=c.synthetic?' · Carried close · execution coverage unknown':c.quality?.basis==='mixed'?` · Mixed: ${c.quality.observedMinutes} recorded / ${c.quality.referenceMinutes} reference / ${c.quality.estimatedMinutes} estimated minutes · Volume from recorded trades only`:c.estimateMethod==='median_venue_ohlc_v1'?' · Composite reference · '+(c.components?.length||c.sources?.length||0)+' sources · '+(c.excludedComponents?.length||c.excludedComponentCount||0)+' exclusions · not traded OHLC':c.estimated?' · Reconstructed reference · not traded OHLC':c.referenceOnly?' · Reference price · not native executions':c.synthetic?' · Carried close · execution coverage unknown':'';this.tooltip.textContent=`O ${this.formatPrice(c.open)}  H ${this.formatPrice(c.high)}  L ${this.formatPrice(c.low)}  C ${this.formatPrice(c.close)}${source?' · '+source+' · '+c.quoteCurrency:''}${quality}`;this.container.dispatchEvent(new CustomEvent('candlehover',{detail:c}));}
 }
 destroy(){this.destroyed=true;cancelAnimationFrame(this.frame);cancelAnimationFrame(this.crosshairFrame);this.abort.abort();this.trackLayer?.destroy();this.drawingLayer?.destroy();this.resizeObserver.disconnect();this.intersectionObserver?.disconnect();this.container.replaceChildren();}
}
