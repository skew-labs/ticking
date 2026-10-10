// Native TICK drawing primitives. Screen geometry is derived from time/price anchors.
const groups=[
 ['Lines',[
 ['trend','Trend line',2],['arrow','Arrow',2],['ray','Ray',2],['extended','Extended line',2],['info','Info line',2],['angle','Trend angle',2],['horizontal','Horizontal line',1],['horizontal-ray','Horizontal ray',1],['vertical','Vertical line',1],['cross','Cross line',1],['parallel','Parallel channel',3],['regression','Regression channel',2],['flat-channel','Flat top / bottom',3],['disjoint','Disjoint channel',4],['vwap','Anchored VWAP',1]]],
 ['Fibonacci & Gann',[
 ['fib','Fib retracement',2],['fib-extension','Trend-based Fib extension',3],['fib-channel','Fib channel',3],['fib-time','Fib time zone',2],['fib-trend-time','Trend-based Fib time',3],['fib-fan','Fib resistance fan',2],['fib-circles','Fib circles',2],['fib-arcs','Fib resistance arcs',2],['fib-wedge','Fib wedge',3],['fib-spiral','Fib spiral',2],['pitchfork','Pitchfork',3],['schiff','Schiff pitchfork',3],['modified-schiff','Modified Schiff pitchfork',3],['inside-pitchfork','Inside pitchfork',3],['pitchfan','Pitchfan',3],['gann-box','Gann box',2],['gann-square','Gann square',2],['gann-fixed','Gann square fixed',2],['gann-fan','Gann fan',2]]],
 ['Shapes',[
 ['brush','Brush',0],['highlighter','Highlighter',0],['rectangle','Rectangle',2],['circle','Circle',2],['ellipse','Ellipse',2],['path','Path',-1],['polyline','Polyline',-1],['triangle','Triangle',3],['rotated-rectangle','Rotated rectangle',3],['curve','Curve',3],['double-curve','Double curve',4],['arc','Arc',3]]],
 ['Text & markers',[
 ['text','Text',1],['note','Note',1],['anchored-note','Anchored note',1],['signpost','Signpost',1],['callout','Callout',2],['comment','Comment',1],['price-label','Price label',1],['price-note','Price note',2],['arrow-marker','Arrow marker',2],['arrow-left','Arrow left',1],['arrow-right','Arrow right',1],['arrow-up','Arrow up',1],['arrow-down','Arrow down',1],['flag','Flag',1],['icon','Icon',1],['emoji','Emoji',1],['sticker','Sticker',1],['pin','Pin',1],['table','Table',1]]],
 ['Patterns',[
 ['xabcd','XABCD pattern',5],['cypher','Cypher pattern',5],['abcd','ABCD pattern',4],['triangle-pattern','Triangle pattern',5],['three-drives','Three drives pattern',7],['head-shoulders','Head and shoulders',7],['elliott-impulse','Elliott impulse wave',6],['elliott-triangle','Elliott triangle wave',6],['elliott-triple','Elliott triple combo',6],['elliott-correction','Elliott correction wave',4],['elliott-double','Elliott double combo',4],['cyclic','Cyclic lines',2],['cycles','Time cycles',2],['sine','Sine line',2]]],
 ['Measure & forecast',[
 ['long','Long position',3],['short','Short position',3],['forecast','Position forecast',-1],['date-range','Date range',2],['price-range','Price range',2],['range','Date & price range',2],['bars-pattern','Bars pattern',3],['ghost','Ghost feed',3],['sector','Sector',3],['volume-profile','Fixed range volume profile',2]]]
];
export const DRAWING_TOOLS=groups.flatMap(([group,items])=>items.map(([id,name,points])=>({id,name,points,group})));
export const TOOL_GROUPS=groups.map(([name])=>name);
export const toolById=id=>DRAWING_TOOLS.find(t=>t.id===id);
export const TEXT_TOOLS=new Set(['text','note','anchored-note','signpost','callout','comment','icon','emoji','sticker','pin','table']);
const fib=[0,.236,.382,.5,.618,.786,1,1.618];
const tau=Math.PI*2;
const add=(a,b)=>({x:a.x+b.x,y:a.y+b.y});
const sub=(a,b)=>({x:a.x-b.x,y:a.y-b.y});
const mul=(a,k)=>({x:a.x*k,y:a.y*k});
const mix=(a,b,f)=>add(a,mul(sub(b,a),f));
const distance=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
const duration=ms=>Math.abs(ms)>=86400000?`${(Math.abs(ms)/86400000).toFixed(1)}d`:Math.abs(ms)>=3600000?`${(Math.abs(ms)/3600000).toFixed(1)}h`:Math.abs(ms)>=60000?`${(Math.abs(ms)/60000).toFixed(1)}m`:`${Math.round(Math.abs(ms)/1000)}s`;
export function drawingGeometry(d,v){
 const points=d.points.map(p=>d.tool==='anchored-note'?{x:p.rx*v.width,y:p.ry*v.height}:v.screen(p));
 const a=points[0]||{x:0,y:0},b=points[1]||a,c=points[2]||b,e=points[3]||c;
 const paths=[],labels=[];
 const path=(pts,extra={})=>paths.push({points:pts,...extra});
 const line=(p,q,extra={})=>path([p,q],extra);
 const label=(p,text,extra={})=>labels.push({...p,text:String(text),...extra});
 const rect=(p,q,extra={})=>path([p,{x:q.x,y:p.y},q,{x:p.x,y:q.y}],{closed:true,fill:true,...extra});
 const ray=(p,q,both=false)=>{const delta=sub(q,p),length=distance(p,q);if(length<.01)return;const direction=mul(delta,Math.max(v.width,v.height)*4/length);line(both?sub(p,direction):p,add(p,direction));};
 const arrow=(p,q)=>{line(p,q);const r=Math.atan2(q.y-p.y,q.x-p.x);path([{x:q.x-9*Math.cos(r-.45),y:q.y-9*Math.sin(r-.45)},q,{x:q.x-9*Math.cos(r+.45),y:q.y-9*Math.sin(r+.45)}]);};
 const ellipse=(center,rx,ry,start=0,end=tau,extra={})=>path(Array.from({length:81},(_,i)=>({x:center.x+rx*Math.cos(start+(end-start)*i/80),y:center.y+ry*Math.sin(start+(end-start)*i/80)})),{closed:end-start>=tau-.001,...extra});
 const measured=()=>`${((d.points.at(-1).price-d.points[0].price)/d.points[0].price*100).toFixed(2)}% · ${duration(d.points.at(-1).time-d.points[0].time)}`;
 const levels=(start,end,origin=start)=>fib.forEach(f=>{const y=origin.y+(end.y-start.y)*f;line({x:Math.min(a.x,b.x),y},{x:Math.max(a.x,b.x,c.x)+35,y},{dash:f===0||f===1?[]:[3,3]});label({x:Math.max(a.x,b.x,c.x)+39,y},`${f}  ${v.price(v.value(y))}`,{size:9});});
 const patternLabels={xabcd:['X','A','B','C','D'],cypher:['X','A','B','C','D'],abcd:['A','B','C','D'],'triangle-pattern':['A','B','C','D','E'],'three-drives':['0','1','A','2','B','3','C'],'head-shoulders':['','LS','','Head','','RS',''],'elliott-impulse':['0','1','2','3','4','5'],'elliott-triangle':['0','A','B','C','D','E'],'elliott-triple':['0','W','X','Y','X','Z'],'elliott-correction':['0','A','B','C'],'elliott-double':['0','W','X','Y']};
 if(patternLabels[d.tool]){
  path(points);points.forEach((p,i)=>label({x:p.x,y:p.y+(i%2?-16:17)},patternLabels[d.tool][i]));
  if(['xabcd','cypher','abcd'].includes(d.tool)&&points.length>3){path([points[0],points[1],points[2]],{closed:true,fill:true});path([points[2],points[3],points.at(-1)],{closed:true,fill:true});for(let i=2;i<points.length;i++){const previous=Math.abs(d.points[i-1].price-d.points[i-2].price),ratio=Math.abs(d.points[i].price-d.points[i-1].price)/previous;if(Number.isFinite(ratio))label(mix(points[i-1],points[i],.5),ratio.toFixed(3),{size:9});}}
  if(d.tool==='triangle-pattern'&&points.length===5){ray(points[0],points[2]);ray(points[1],points[3]);}
  if(d.tool==='head-shoulders'&&points.length>4)line(points[2],points[4],{dash:[4,3]});
 }else switch(d.tool){
 case 'trend':line(a,b);break;
 case 'arrow':case 'arrow-marker':arrow(a,b);break;
 case 'ray':ray(a,b);break;
 case 'extended':ray(a,b,true);break;
 case 'info':line(a,b);label(mix(a,b,.5),measured());break;
 case 'angle':line(a,b);label(b,`${(-Math.atan2(b.y-a.y,b.x-a.x)*180/Math.PI).toFixed(1)}°`);break;
 case 'horizontal':line({x:0,y:a.y},{x:v.width,y:a.y});label({x:8,y:a.y-10},v.price(d.points[0].price));break;
 case 'horizontal-ray':line(a,{x:v.width,y:a.y});label({x:a.x+8,y:a.y-10},v.price(d.points[0].price));break;
 case 'vertical':line({x:a.x,y:0},{x:a.x,y:v.height});break;
 case 'cross':line({x:0,y:a.y},{x:v.width,y:a.y});line({x:a.x,y:0},{x:a.x,y:v.height});break;
 case 'parallel':case 'flat-channel':case 'disjoint':case 'fib-channel':{
  let a2=c,b2=d.tool==='disjoint'?e:add(c,sub(b,a));
  if(d.tool==='flat-channel')b2={x:b.x,y:c.y};
  path([a,b,b2,a2],{closed:true,fill:true});
  (d.tool==='fib-channel'?fib:[.5]).forEach(f=>{const p=mix(a,a2,f),q=mix(b,b2,f);line(p,q,{dash:[4,3]});if(d.tool==='fib-channel')label(q,f,{size:9});});break;}
 case 'regression':{
  const bars=v.series.filter(x=>x.time>=Math.min(d.points[0].time,d.points[1]?.time??0)&&x.time<=Math.max(d.points[0].time,d.points[1]?.time??0));
  if(bars.length<2){line(a,b);break;}
  const n=bars.length,mx=(n-1)/2,my=bars.reduce((s,x)=>s+x.close,0)/n;
  const slope=bars.reduce((s,x,i)=>s+(i-mx)*(x.close-my),0)/bars.reduce((s,x,i)=>s+(i-mx)**2,0);
  const sd=Math.sqrt(bars.reduce((s,x,i)=>s+(x.close-(my+slope*(i-mx)))**2,0)/n)*2;
  const p=v.screen({time:bars[0].time,price:my-slope*mx}),q=v.screen({time:bars.at(-1).time,price:my+slope*mx});
  const dy=Math.abs(v.screen({time:bars[0].time,price:my+sd}).y-v.screen({time:bars[0].time,price:my}).y);
  line(p,q);path([add(p,{x:0,y:-dy}),add(q,{x:0,y:-dy}),add(q,{x:0,y:dy}),add(p,{x:0,y:dy})],{closed:true,fill:true});label(q,'Linear regression · 2σ',{size:9});break;}
 case 'vwap':{let volume=0,total=0;const pts=[],bars=v.series.filter(bar=>bar.time>=d.points[0].time),base=bars.length&&bars.every(bar=>Number.isFinite(bar.baseVolume)),field=base?'baseVolume':'volume';for(const bar of bars){const weight=bar[field];if(bar.synthetic||!Number.isFinite(weight)||weight<=0)continue;volume+=weight;total+=(bar.high+bar.low+bar.close)/3*weight;pts.push(v.screen({time:bar.time,price:total/volume}));}if(pts.length){path(pts);label(pts.at(-1),base?'aVWAP · base-volume candle estimate':'aVWAP · candle-volume estimate',{size:9});}else label(a,'No recorded volume in range',{align:'left',size:10});break;}
 case 'fib':levels(a,b);break;
 case 'fib-extension':levels(a,b,c);line(a,b,{dash:[3,3]});line(b,c,{dash:[3,3]});break;
 case 'fib-time':case 'fib-trend-time':{const origin=d.tool==='fib-time'?a:c;[0,1,2,3,5,8,13,21].forEach(f=>{const x=origin.x+(b.x-a.x)*f;line({x,y:0},{x,y:v.height});label({x:x+4,y:17},f,{size:9});});break;}
 case 'pitchfork':case 'schiff':case 'modified-schiff':case 'inside-pitchfork':case 'pitchfan':{
  let origin=a;
  if(d.tool==='schiff')origin={x:a.x,y:(a.y+b.y)/2};
  if(d.tool==='modified-schiff')origin=mix(a,b,.5);
  if(d.tool==='inside-pitchfork')origin=mix(a,b,.5);
  const mid=mix(b,c,.5),delta=sub(mid,origin);
  if(d.tool==='pitchfan'){fib.forEach(f=>ray(origin,mix(b,c,f)));break;}
  ray(origin,mid);ray(b,add(b,delta));ray(c,add(c,delta));line(b,c,{dash:[3,3]});break;}
 case 'fib-fan':case 'gann-fan':{const ratios=d.tool==='fib-fan'?[.382,.5,.618,.786,1]:[.125,.25,.5,1,2,4,8];ratios.forEach(f=>{const q={x:b.x,y:a.y+(b.y-a.y)*f};ray(a,q);label(q,d.tool==='gann-fan'?`${f}:1`:f,{size:9});});break;}
 case 'gann-box':case 'gann-square':case 'gann-fixed':{
  const end=d.tool==='gann-fixed'?{x:a.x+Math.sign(b.x-a.x||1)*Math.abs(b.y-a.y),y:b.y}:b;
  rect(a,end);[.25,.5,.75].forEach(f=>{line({x:a.x+(end.x-a.x)*f,y:a.y},{x:a.x+(end.x-a.x)*f,y:end.y},{dash:[3,3]});line({x:a.x,y:a.y+(end.y-a.y)*f},{x:end.x,y:a.y+(end.y-a.y)*f},{dash:[3,3]});});line(a,end);line({x:end.x,y:a.y},{x:a.x,y:end.y});
  if(d.tool!=='gann-box')ellipse(mix(a,end,.5),Math.abs(end.x-a.x)/2,Math.abs(end.y-a.y)/2);break;}
 case 'fib-circles':case 'fib-arcs':{const r=distance(a,b),angle=Math.atan2(b.y-a.y,b.x-a.x);[.382,.5,.618,1,1.618].forEach(f=>{ellipse(a,r*f,r*f,d.tool==='fib-arcs'?angle-Math.PI:0,d.tool==='fib-arcs'?angle:tau);label({x:a.x+r*f,y:a.y},f,{size:9});});line(a,b,{dash:[3,3]});break;}
 case 'fib-wedge':{const r=distance(a,b),start=Math.atan2(b.y-a.y,b.x-a.x),end=Math.atan2(c.y-a.y,c.x-a.x);fib.slice(1).forEach(f=>ellipse(a,r*f,r*f,start,end));line(a,b);line(a,c);break;}
 case 'fib-spiral':{const r=distance(a,b),phase=Math.atan2(b.y-a.y,b.x-a.x);path(Array.from({length:241},(_,i)=>{const t=i/240*Math.PI*6,radius=r*Math.pow(1.618,t/(Math.PI/2)-12);return {x:a.x+radius*Math.cos(phase+t),y:a.y+radius*Math.sin(phase+t)};}));break;}
 case 'brush':path(points);break;
 case 'highlighter':path(points,{width:16,alpha:.3});break;
 case 'rectangle':rect(a,b);break;
 case 'circle':ellipse(a,distance(a,b),distance(a,b),0,tau,{fill:true});break;
 case 'ellipse':ellipse(mix(a,b,.5),Math.abs(b.x-a.x)/2,Math.abs(b.y-a.y)/2,0,tau,{fill:true});break;
 case 'triangle':path([a,b,c],{closed:true,fill:true});break;
 case 'rotated-rectangle':{const d1=sub(b,a),len=distance(a,b)||1,n={x:-d1.y/len,y:d1.x/len},height=(c.x-a.x)*n.x+(c.y-a.y)*n.y,off=mul(n,height);path([a,b,add(b,off),add(a,off)],{closed:true,fill:true});break;}
 case 'curve':{path(Array.from({length:61},(_,i)=>{const t=i/60;return add(add(mul(a,(1-t)**2),mul(c,2*(1-t)*t)),mul(b,t*t));}));break;}
 case 'arc':{const denominator=2*(a.x*(b.y-c.y)+b.x*(c.y-a.y)+c.x*(a.y-b.y));if(Math.abs(denominator)<.001){line(a,b);break;}const aa=a.x*a.x+a.y*a.y,bb=b.x*b.x+b.y*b.y,cc=c.x*c.x+c.y*c.y,center={x:(aa*(b.y-c.y)+bb*(c.y-a.y)+cc*(a.y-b.y))/denominator,y:(aa*(c.x-b.x)+bb*(a.x-c.x)+cc*(b.x-a.x))/denominator},r=distance(a,center),start=Math.atan2(a.y-center.y,a.x-center.x),finish=Math.atan2(b.y-center.y,b.x-center.x),control=Math.atan2(c.y-center.y,c.x-center.x),norm=x=>(x+tau)%tau;let sweep=norm(finish-start);if(norm(control-start)>sweep)sweep-=tau;ellipse(center,r,r,start,start+sweep);break;}
 case 'double-curve':{path(Array.from({length:81},(_,i)=>{const t=i/80;return add(add(mul(a,(1-t)**3),mul(c,3*(1-t)**2*t)),add(mul(e,3*(1-t)*t*t),mul(b,t**3)));}));break;}
 case 'path':case 'polyline':path(points);break;
 case 'forecast':path(points,{dash:[4,3]});if(points.length>1)arrow(points.at(-2),points.at(-1));label(points.at(-1),measured());break;
 case 'text':case 'note':case 'anchored-note':case 'comment':case 'signpost':case 'callout':case 'price-label':case 'price-note':{
  const target=['callout','price-note'].includes(d.tool)?b:a;
  if(target!==a)line(a,b);
  if(d.tool==='signpost'){line(a,{x:a.x,y:a.y-28});label({x:a.x,y:a.y-38},d.text||'Note',{box:true});}
  else label(target,['price-label','price-note'].includes(d.tool)?v.price(d.points[0].price):d.text||'Add a note',{box:d.tool!=='text',align:'left',size:d.fontSize||12,maxWidth:220});break;}
 case 'arrow-left':case 'arrow-right':case 'arrow-up':case 'arrow-down':{const shifts={'arrow-left':{x:22,y:0},'arrow-right':{x:-22,y:0},'arrow-up':{x:0,y:22},'arrow-down':{x:0,y:-22}};arrow(add(a,shifts[d.tool]),a);break;}
 case 'flag':line(a,{x:a.x,y:a.y-30});path([{x:a.x,y:a.y-30},{x:a.x+23,y:a.y-22},{x:a.x,y:a.y-15}],{closed:true,fill:true});break;
 case 'pin':ellipse({x:a.x,y:a.y-16},7,7,0,tau,{fill:true});line({x:a.x,y:a.y-9},a);label({x:a.x+13,y:a.y-17},d.text||'Pin',{align:'left'});break;
 case 'table':{const rows=(d.text||'Level | Price; Support | —; Target | —').split(';').slice(0,8).map(row=>row.split('|').slice(0,5)),cols=Math.max(...rows.map(row=>row.length)),cellWidth=76,rowHeight=24;rect(a,{x:a.x+cols*cellWidth,y:a.y+rows.length*rowHeight});rows.forEach((row,i)=>{if(i)line({x:a.x,y:a.y+i*rowHeight},{x:a.x+cols*cellWidth,y:a.y+i*rowHeight});row.forEach((text,j)=>label({x:a.x+j*cellWidth+5,y:a.y+i*rowHeight+12},text.trim().slice(0,12),{align:'left',size:10}));});for(let j=1;j<cols;j++)line({x:a.x+j*cellWidth,y:a.y},{x:a.x+j*cellWidth,y:a.y+rows.length*rowHeight});break;}
 case 'icon':label(a,d.text||'★',{size:25});break;
 case 'emoji':label(a,d.text||'⚡',{size:27});break;
 case 'sticker':label(a,d.text||'WATCH',{size:13,box:true});break;
 case 'cyclic':case 'cycles':case 'sine':{const step=Math.max(12,Math.abs(b.x-a.x)),start=a.x-Math.ceil(a.x/step)*step;for(let x=start;x<v.width+step;x+=step){if(d.tool==='cyclic')line({x,y:0},{x,y:v.height},{dash:[4,3]});else if(d.tool==='cycles')ellipse({x:x+step/2,y:a.y},step/2,Math.max(10,Math.abs(b.y-a.y)),Math.PI,tau);else path(Array.from({length:41},(_,i)=>({x:x+i*step/40,y:a.y+Math.sin(i/40*tau)*Math.max(8,Math.abs(b.y-a.y))})));}break;}
 case 'long':case 'short':{
  const width=Math.max(80,Math.abs(b.x-a.x)),right=a.x+width,target=b.y,stop=c.y;
  rect(a,{x:right,y:target},{color:'#239a82',alpha:.14});rect(a,{x:right,y:stop},{color:'#d36a79',alpha:.14});
  const risk=Math.abs(d.points[0].price-(d.points[2]?.price??d.points[0].price)),reward=Math.abs((d.points[1]?.price??d.points[0].price)-d.points[0].price);
  label({x:a.x+8,y:a.y-9},`${d.tool==='long'?'Long':'Short'} · R/R ${risk?(reward/risk).toFixed(2):'—'}`,{align:'left',box:true});
  label({x:right,y:target-10},`Target ${v.price(d.points[1]?.price??0)}`,{align:'right',size:9});label({x:right,y:stop+12},`Stop ${v.price(d.points[2]?.price??0)}`,{align:'right',size:9});break;}
 case 'date-range':case 'price-range':case 'range':{rect(a,b);const dp=d.points.at(-1).price-d.points[0].price;label(mix(a,b,.5),d.tool==='date-range'?`${duration(d.points.at(-1).time-d.points[0].time)} · ${Math.round(Math.abs(d.points.at(-1).time-d.points[0].time)/v.period)} bars`:d.tool==='price-range'?`${v.price(dp)} (${(dp/d.points[0].price*100).toFixed(2)}%)`:measured(),{box:true});break;}
 case 'bars-pattern':case 'ghost':{
  const bars=v.series.filter(x=>x.time>=Math.min(d.points[0].time,d.points[1]?.time??0)&&x.time<=Math.max(d.points[0].time,d.points[1]?.time??0));
  if(bars.length){const base=bars[0],start=d.points[2]||d.points[1]||d.points[0];for(const bar of bars.slice(0,500)){const t=start.time+bar.time-base.time,delta=start.price-base.open,p=v.screen({time:t,price:bar.high+delta}),q=v.screen({time:t,price:bar.low+delta});line(p,q,{alpha:.55});line(v.screen({time:t,price:bar.open+delta}),v.screen({time:t,price:bar.close+delta}),{width:Math.max(2,v.step*.6),alpha:.55});}label(c,d.tool==='ghost'?'Ghost projection':'Copied bars',{size:9});}break;}
 case 'volume-profile':{const bars=v.series.filter(x=>!x.synthetic&&x.volume>0&&x.time>=Math.min(d.points[0].time,d.points[1]?.time??0)&&x.time<=Math.max(d.points[0].time,d.points[1]?.time??0));if(!bars.length){label(a,'No recorded volume in range',{size:10});break;}const low=Math.min(...bars.map(x=>x.low)),high=Math.max(...bars.map(x=>x.high)),height=Math.max(high-low,Math.abs(low)*Number.EPSILON,Number.MIN_VALUE),bins=Array(24).fill(0);for(const bar of bars){if(bar.high===bar.low){bins[Math.min(23,Math.max(0,Math.floor((bar.low-low)/height*24)))]+=bar.volume;continue;}for(let i=0;i<24;i++){const lo=low+height*i/24,hi=low+height*(i+1)/24,overlap=Math.max(0,Math.min(hi,bar.high)-Math.max(lo,bar.low));bins[i]+=bar.volume*overlap/(bar.high-bar.low);}}const max=Math.max(...bins),left=Math.min(a.x,b.x),width=Math.max(50,Math.abs(b.x-a.x)*.65);bins.forEach((volume,i)=>{const y1=v.screen({time:d.points[0].time,price:low+height*i/24}).y,y2=v.screen({time:d.points[0].time,price:low+height*(i+1)/24}).y;rect({x:left,y:y1},{x:left+volume/max*width,y:y2},{alpha:.3,color:volume===max?'#d0a052':d.color});});label({x:left,y:Math.min(a.y,b.y)-18},'Volume profile · OHLC estimate',{align:'left',size:9});break;}
 case 'sector':{const r=distance(a,b),start=Math.atan2(b.y-a.y,b.x-a.x),end=Math.atan2(c.y-a.y,c.x-a.x),arc=Array.from({length:51},(_,i)=>({x:a.x+r*Math.cos(start+(end-start)*i/50),y:a.y+r*Math.sin(start+(end-start)*i/50)}));path([a,...arc,a],{closed:true,fill:true});break;}
 }
 return {paths,labels,handles:points};
}
export function labelLayout(ctx,t){
 const size=t.size||11,leading=size*1.35,lines=[];ctx.save();ctx.font=`400 ${size}px "DM Sans",sans-serif`;
 for(const paragraph of t.text.split('\n')){let row='';for(const char of paragraph){if(t.maxWidth&&row&&ctx.measureText(row+char).width>t.maxWidth){lines.push(row);row='';}row+=char;}lines.push(row);}
 const width=Math.max(1,...lines.map(line=>ctx.measureText(line).width)),height=size+(lines.length-1)*leading,pad=t.box?6:0,left=t.align==='left'?t.x:t.align==='right'?t.x-width:t.x-width/2,top=t.y-size/2;ctx.restore();return {size,leading,lines,width,height,left,top,pad};
}
export function paintGeometry(ctx,g,d,selected=false,background='#fff'){
 ctx.save();ctx.lineCap='round';ctx.lineJoin='round';
 for(const p of g.paths){if(!p.points.length)continue;ctx.strokeStyle=p.color||d.color;ctx.lineWidth=p.width||d.width||1.5;ctx.globalAlpha=p.alpha??1;ctx.setLineDash(p.dash||({solid:[],dashed:[6,4],dotted:[2,4]}[d.dash])||[]);ctx.beginPath();p.points.forEach((pt,i)=>i?ctx.lineTo(pt.x,pt.y):ctx.moveTo(pt.x,pt.y));if(p.closed)ctx.closePath();if(p.fill){ctx.fillStyle=p.color||d.color;ctx.globalAlpha=p.alpha??.09;ctx.fill();ctx.globalAlpha=1;}ctx.stroke();}
 ctx.globalAlpha=1;ctx.setLineDash([]);
 for(const t of g.labels){const l=labelLayout(ctx,t);ctx.font=`400 ${l.size}px "DM Sans",sans-serif`;ctx.textAlign='left';ctx.textBaseline='middle';if(t.box){ctx.fillStyle=background;ctx.strokeStyle=d.color;ctx.lineWidth=.7;ctx.fillRect(l.left-6,l.top-5,l.width+12,l.height+10);ctx.strokeRect(l.left-6,l.top-5,l.width+12,l.height+10);}ctx.fillStyle=d.color;l.lines.forEach((line,i)=>ctx.fillText(line,l.left,t.y+i*l.leading));}
 if(selected){ctx.setLineDash([]);for(const p of g.handles){ctx.beginPath();ctx.arc(p.x,p.y,4.5,0,tau);ctx.fillStyle='#fff';ctx.fill();ctx.strokeStyle=d.color;ctx.lineWidth=1.5;ctx.stroke();}}
 ctx.restore();
}
export function hitGeometry(g,p,ctx){
 const dist=(a,b)=>{const dx=b.x-a.x,dy=b.y-a.y,len=dx*dx+dy*dy,t=len?Math.max(0,Math.min(1,((p.x-a.x)*dx+(p.y-a.y)*dy)/len)):0;return distance(p,{x:a.x+dx*t,y:a.y+dy*t});};
 if(g.handles.some(h=>distance(h,p)<9))return true;
 if(g.labels.some(t=>{const l=ctx?labelLayout(ctx,t):{left:t.align==='left'?t.x:t.align==='right'?t.x-t.text.length*7:t.x-t.text.length*3.5,top:t.y-7,width:t.text.length*7,height:14,pad:6};return p.x>=l.left-l.pad-4&&p.x<=l.left+l.width+l.pad+4&&p.y>=l.top-l.pad-4&&p.y<=l.top+l.height+l.pad+4;}))return true;
 return g.paths.some(path=>{const pts=path.points;if(pts.some((a,i)=>i>0&&dist(pts[i-1],a)<7))return true;if(!path.closed||pts.length<3)return false;if(dist(pts.at(-1),pts[0])<7)return true;if(!path.fill)return false;let inside=false;for(let i=0,j=pts.length-1;i<pts.length;j=i++){const a=pts[i],b=pts[j];if((a.y>p.y)!==(b.y>p.y)&&p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)inside=!inside;}return inside;});
}
