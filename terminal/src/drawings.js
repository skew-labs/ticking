import {toolById,TEXT_TOOLS,drawingGeometry,paintGeometry,hitGeometry} from './drawing-tools.js';
const clone=x=>JSON.parse(JSON.stringify(x));
const validPoint=p=>p&&Number.isFinite(p.time)&&Number.isFinite(p.price)&&p.price>0&&Math.abs(p.time)<8.64e15;
export function validateDrawings(input){
 if(!Array.isArray(input)||input.length>200)throw new Error('Drawing files can contain up to 200 objects.');
 const ids=new Set();
 return input.map(d=>{
  const tool=toolById(d.tool);
  if(!tool||!Array.isArray(d.points)||d.points.length>1500||!d.points.every(validPoint)||d.points.length<(tool.points>0?tool.points:2))throw new Error('This file contains an invalid drawing.');
  const points=d.points.map(p=>({time:p.time,price:p.price,rx:Number.isFinite(p.rx)?p.rx:.5,ry:Number.isFinite(p.ry)?p.ry:.5}));
  let id=typeof d.id==='string'&&/^[a-zA-Z0-9_-]{1,80}$/.test(d.id)?d.id:crypto.randomUUID();if(ids.has(id))id=crypto.randomUUID();ids.add(id);
  return {id,tool:d.tool,points,color:/^#[\da-f]{6}$/i.test(d.color)?d.color:'#7564c3',width:[1,2,3,4].includes(d.width)?d.width:2,dash:['solid','dashed','dotted'].includes(d.dash)?d.dash:'solid',text:typeof d.text==='string'?d.text.slice(0,160):'',fontSize:Number.isFinite(d.fontSize)?Math.max(9,Math.min(32,d.fontSize)):12,hidden:!!d.hidden,locked:!!d.locked};
 });
}
export class DrawingLayer{
 constructor(chart){
  this.chart=chart;this.tool='select';this.items=[];this.selected=null;this.pending=null;this.drag=null;this.hover=null;this.undoStack=[];this.redoStack=[];
  this.defaults={color:'#7564c3',width:2,dash:'solid',text:''};this.magnet='off';this.stay=false;this.hidden=false;this.locked=false;this.message='Select a tool, then place its anchors on the chart.';
  this.key=`tick-drawings-v1:${chart.options.storageScope||'workspace'}:${chart.token.market}`;
  try{const saved=localStorage.getItem(this.key);if(saved)this.items=validateDrawings(JSON.parse(saved));}catch{this.message='Saved drawings could not be loaded.';}
  this.abort=new AbortController();const opts={signal:this.abort.signal};
  for(const name of ['pointerdown','pointermove','pointerup','pointercancel','dblclick','keydown'])chart.canvas.addEventListener(name,e=>this[name](e),opts);
  chart.canvas.style.touchAction='none';chart.canvas.style.cursor='crosshair';
 }
 get current(){return this.items.find(d=>d.id===this.selected);}
 setMarket(market){const key=`tick-drawings-v1:${this.chart.options.storageScope||'workspace'}:${market}`;if(this.key===key)return;this.finishText();this.save();this.key=key;this.items=[];this.selected=null;this.pending=null;this.drag=null;this.undoStack=[];this.redoStack=[];try{const saved=localStorage.getItem(key);if(saved)this.items=validateDrawings(JSON.parse(saved));}catch{this.message='Saved drawings could not be loaded.';}this.refresh();}
 notify(){this.chart.container.dispatchEvent(new CustomEvent('drawingchange',{detail:{tool:this.tool,count:this.items.length,selected:this.current,message:this.message}}));}
 refresh(){this.chart.requestDraw();this.notify();}
 snapshot(){return clone(this.items);}
 remember(before=this.snapshot()){this.undoStack.push(before);if(this.undoStack.length>80)this.undoStack.shift();this.redoStack=[];}
 save(){try{localStorage.setItem(this.key,JSON.stringify(this.items));}catch{this.message='Storage is unavailable. Export to keep these drawings.';}}
 commit(before){this.remember(before);this.save();this.refresh();}
 undo(){this.finishText();if(this.pending){this.cancel();return;}if(!this.undoStack.length)return;this.redoStack.push(this.snapshot());this.items=this.undoStack.pop();this.selected=null;this.save();this.refresh();}
 redo(){this.finishText();if(!this.redoStack.length)return;this.undoStack.push(this.snapshot());this.items=this.redoStack.pop();this.selected=null;this.save();this.refresh();}
 setTool(id){if(id!=='select'&&!toolById(id))return;this.finishText();if(id!=='select'&&this.locked){this.message='Drawings locked · unlock to draw or edit.';this.notify();return;}if(id!=='select')this.hidden=false;this.cancel(false);this.tool=id;this.selected=null;this.chart.canvas.style.cursor='crosshair';const t=toolById(id);this.message=t?`${t.name} · ${t.points===0?'Drag to draw':t.points<0?'Click points; double-click or Enter to finish':t.points===2?'Drag and release, or click two anchors':`${t.points} ${t.points===1?'anchor':'anchors'} · click to place`}`:'Drag the chart to pan. Click a drawing to edit.';this.refresh();}
 cancel(refresh=true){this.finishText(false);this.pending=null;this.drag=null;this.hover=null;if(refresh){this.message='Drawing cancelled.';this.refresh();}}
 point(e){const r=this.chart.canvas.getBoundingClientRect();return {x:e.clientX-r.left,y:e.clientY-r.top};}
 anchor(p){const v=this.chart.view;let a=v.anchor(p);if(this.magnet!=='off'){
  const c=v.candleAtX?.(p.x)||this.chart.visible[Math.max(0,Math.min(this.chart.visible.length-1,Math.floor(p.x/v.step)))];
  const choices=['open','high','low','close'].map(k=>({time:c.time,price:c[k]})).sort((a,b)=>Math.abs(v.screen(a).y-p.y)-Math.abs(v.screen(b).y-p.y));
  if(this.magnet==='strong'||Math.abs(v.screen(choices[0]).y-p.y)<10)a={...a,...choices[0]};
 }return {...a,rx:p.x/v.width,ry:p.y/v.height};}
 hit(p){if(this.hidden)return null;for(const d of [...this.items].reverse()){if(!d.hidden&&hitGeometry(drawingGeometry(d,this.chart.view),p,this.chart.canvas.getContext('2d')))return d;}return null;}
 pointerdown(e){
  if(e.button!==0||!this.chart.view)return;const p=this.point(e),v=this.chart.view;if(p.x>v.width||p.y>v.height||p.x<0||p.y<0)return;
  this.finishText();e.preventDefault();this.chart.canvas.focus({preventScroll:true});this.chart.canvas.setPointerCapture(e.pointerId);
  if(this.tool==='select'){
   const hit=this.hit(p);this.selected=hit?.id||null;
   if(hit&&!hit.locked&&!this.locked){const handles=drawingGeometry(hit,v).handles,index=handles.findIndex(q=>Math.hypot(q.x-p.x,q.y-p.y)<10);this.drag={kind:'edit',before:this.snapshot(),origin:this.anchor(p),points:clone(hit.points),index,start:p};}
   else if(!hit)this.drag={kind:'pan',offset:this.chart.offset,start:p};
   this.refresh();return;
  }
  const t=toolById(this.tool);
  if(this.locked)return;
  if(this.items.length>=200){this.message='This chart holds up to 200 drawings. Remove or export a few first.';this.refresh();return;}
  if(!this.pending)this.pending={id:crypto.randomUUID(),tool:this.tool,points:[],...this.defaults};
  if(t.points===0){this.pending.points=[this.anchor(p)];this.drag={kind:'brush'};}
  else{this.pending.points.push(this.anchor(p));this.hover=null;if(t.points===2&&this.pending.points.length===1)this.drag={kind:'place',start:p,moved:false};if(t.points>0&&this.pending.points.length===t.points)this.finish();else this.message=`${t.name} · ${this.pending.points.length} placed${t.points>0?` / ${t.points}`:' · Enter to finish'} · Esc to cancel`;}
  this.refresh();
 }
 pointermove(e){
  if(!this.chart.view)return;const p=this.point(e),v=this.chart.view;
  if(this.drag?.kind==='brush'){if(this.pending.points.length<1500&&Math.hypot(p.x-(this.lastBrush?.x??-999),p.y-(this.lastBrush?.y??-999))>3){this.pending.points.push(this.anchor(p));this.lastBrush=p;}this.chart.requestDraw();return;}
  if(this.drag?.kind==='pan'){const bars=(p.x-this.drag.start.x)/v.step;this.chart.offset=this.chart.clampOffset(this.drag.offset+bars);this.chart.requestDraw();return;}
  if(this.drag?.kind==='place'){this.drag.moved||=Math.hypot(p.x-this.drag.start.x,p.y-this.drag.start.y)>5;this.hover=this.anchor(p);this.chart.requestDraw();return;}
  if(this.drag?.kind==='edit'&&this.current){const d=this.current,target=this.anchor(p);if(this.drag.index>=0)d.points[this.drag.index]=target;else{const dt=target.time-this.drag.origin.time,dp=target.price-this.drag.origin.price;d.points=this.drag.points.map(q=>({...q,time:q.time+dt,price:Math.max(Number.MIN_VALUE,q.price+dp),rx:q.rx+(p.x-this.drag.start.x)/v.width,ry:q.ry+(p.y-this.drag.start.y)/v.height}));}this.chart.requestDraw();return;}
  if(this.pending){this.hover=this.anchor(p);this.chart.requestDraw();}
 }
 pointerup(e){if(this.drag?.kind==='place'&&this.pending){const p=this.point(e),moved=this.drag.moved||Math.hypot(p.x-this.drag.start.x,p.y-this.drag.start.y)>5;if(moved){const v=this.chart.view;this.pending.points.push(this.anchor({x:Math.max(0,Math.min(v.width,p.x)),y:Math.max(this.chart.priceTop,Math.min(this.chart.priceTop+this.chart.priceHeight,p.y))}));this.finish();}}else if(this.drag?.kind==='brush'){if(this.pending.points.length>1)this.finish();else this.cancel();this.lastBrush=null;}else if(this.drag?.kind==='edit'){if(JSON.stringify(this.drag.before)!==JSON.stringify(this.items))this.commit(this.drag.before);}this.drag=null;if(this.chart.canvas.hasPointerCapture(e.pointerId))this.chart.canvas.releasePointerCapture(e.pointerId);this.chart.requestCrosshair();}
 pointercancel(){if(this.drag?.kind==='edit')this.items=this.drag.before;this.cancel();}
 dblclick(e){if(this.pending&&toolById(this.tool)?.points<0){e.preventDefault();const pts=this.pending.points;if(pts.length>2&&Math.abs(pts.at(-1).time-pts.at(-2).time)<this.chart.view.period/4)pts.pop();this.finish();return;}if(!this.pending){const hit=this.hit(this.point(e));if(hit&&TEXT_TOOLS.has(hit.tool)&&!hit.locked&&!this.locked){e.preventDefault();this.selected=hit.id;this.openTextEditor();}}}
 finish(){if(!this.pending)return;const t=toolById(this.pending.tool);if(this.pending.points.length<(t.points>0?t.points:2))return;
  if(['long','short'].includes(t.id)){const [entry,target,stop]=this.pending.points,isLong=t.id==='long';if((isLong&&(target.price<=entry.price||stop.price>=entry.price))||(!isLong&&(target.price>=entry.price||stop.price<=entry.price))){this.pending.points.pop();this.message=`${t.name}: target must be ${isLong?'above':'below'} entry and stop ${isLong?'below':'above'} it. Esc to restart.`;return;}}
  const before=this.snapshot(),item=this.pending;this.items.push(item);this.selected=item.id;this.pending=null;this.hover=null;if(!this.stay)this.tool='select';this.message=`${t.name} added · drag anchors to adjust.`;if(TEXT_TOOLS.has(item.tool))this.openTextEditor(before,true);else this.commit(before);
 }
 // A text edit is one undoable transaction, including the initial placement.
 beginText(element,before=this.snapshot(),isNew=false){
  this.finishText();const item=this.current;if(!item||!TEXT_TOOLS.has(item.tool)||item.locked||this.locked)return false;
  this.textEdit={id:item.id,before,isNew,element};return true;
 }
 previewText(value){const edit=this.textEdit,item=this.items.find(d=>d.id===edit?.id);if(!item)return;item.text=value.slice(0,160);this.chart.requestDraw();this.notify();}
 finishText(accept=true){
  const edit=this.textEdit;if(!edit)return;this.textEdit=null;const item=this.items.find(d=>d.id===edit.id);
  if(edit.inline)edit.element.remove();
  if(!accept||(edit.isNew&&!item?.text.trim())){this.items=edit.before;this.selected=edit.isNew?null:edit.id;this.message='Text edit cancelled.';this.refresh();return;}
  this.message='Text saved · double-click to edit.';if(JSON.stringify(edit.before)!==JSON.stringify(this.items))this.commit(edit.before);else this.refresh();
 }
 openTextEditor(before=this.snapshot(),isNew=false){
  const editor=document.createElement('div');editor.className='tick-text-editor';editor.innerHTML='<textarea aria-label="Chart annotation text" maxlength="160" rows="3" placeholder="Type your note…"></textarea><small>Enter to save · Shift+Enter for a new line · Esc to cancel</small>';
  const input=editor.querySelector('textarea');if(!this.beginText(editor,before,isNew))return;this.textEdit.inline=true;input.value=this.current.text||'';this.chart.container.append(editor);
  input.addEventListener('input',()=>this.previewText(input.value));input.addEventListener('keydown',event=>{event.stopPropagation();if(event.key==='Escape'||(event.key==='Enter'&&!event.shiftKey&&!event.isComposing)){event.preventDefault();this.finishText(event.key!=='Escape');this.chart.canvas.focus({preventScroll:true});}});
  input.addEventListener('blur',()=>{if(this.textEdit?.element===editor)this.finishText();});this.message='Type your note · Enter to save · Esc to cancel.';this.refresh();input.focus({preventScroll:true});input.setSelectionRange(input.value.length,input.value.length);
 }
 positionTextEditor(){
  const edit=this.textEdit,item=this.items.find(d=>d.id===edit?.id);if(!edit?.inline||!item||!this.chart.view)return;
  const v=this.chart.view,g=drawingGeometry(item,v),point=g.labels[0]||g.handles[0];if(!point)return;const el=edit.element,width=Math.min(260,Math.max(150,v.width-16));el.style.width=width+'px';el.style.left=Math.max(8,Math.min(v.width-width-8,point.x))+'px';el.style.top=Math.max(8,Math.min(v.height-el.offsetHeight-8,point.y-8))+'px';
 }
 update(patch){this.finishText();if(this.current){if(this.current.locked||this.locked)return;const before=this.snapshot();Object.assign(this.current,patch);this.commit(before);}else{Object.assign(this.defaults,patch);this.notify();}}
 toggle(id,field){this.finishText();const d=this.items.find(x=>x.id===id);if(!d)return;const before=this.snapshot();d[field]=!d[field];this.commit(before);}
 remove(id=this.selected){this.finishText();const index=this.items.findIndex(x=>x.id===id);if(index<0||this.items[index].locked||this.locked)return;const before=this.snapshot();this.items.splice(index,1);this.selected=null;this.commit(before);}
 clear(){this.finishText();const before=this.snapshot();this.items=this.items.filter(d=>d.locked||this.locked);this.selected=null;this.commit(before);}
 duplicate(){this.finishText();if(!this.chart.view||!this.current||this.current.locked||this.locked||this.items.length>=200)return;const before=this.snapshot(),d=clone(this.current);d.id=crypto.randomUUID();d.locked=false;d.points=d.points.map(p=>({...p,time:p.time+this.chart.view.period*3,rx:p.rx+.025}));this.items.push(d);this.selected=d.id;this.commit(before);}
 import(data){this.finishText();const items=validateDrawings(data);const before=this.snapshot();this.items=items;this.selected=null;this.commit(before);}
 keydown(e){if(e.key==='Escape'){this.cancel(false);this.tool='select';this.selected=null;this.message='Select a tool or drag to pan.';this.refresh();}if(e.key==='Enter'&&this.pending){e.preventDefault();this.finish();}if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();this.remove();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?this.redo():this.undo();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='y'){e.preventDefault();this.redo();}}
 paint(ctx){const v=this.chart.view;if(!v)return;ctx.save();ctx.beginPath();ctx.rect(0,0,v.width,v.height);ctx.clip();if(!this.hidden)for(const d of this.items){if(!d.hidden)paintGeometry(ctx,drawingGeometry(d,v),d,d.id===this.selected&&!d.locked&&!this.locked,this.chart.colors.bg);}
  if(this.pending){const d=clone(this.pending),t=toolById(d.tool);if(this.hover&&t.points!==0&&(t.points<0||d.points.length<t.points))d.points.push(this.hover);if(d.points.length)paintGeometry(ctx,drawingGeometry(d,v),d,true,this.chart.colors.bg);}
  ctx.restore();this.positionTextEditor();
 }
 destroy(){this.finishText();this.abort.abort();}
}
