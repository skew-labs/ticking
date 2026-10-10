// Styled select with native value/change semantics and a keyboard-operated listbox.
export function enhanceSelect(select, renderOption) {
  const previous={hidden:select.hidden,tabIndex:select.getAttribute('tabindex'),ariaHidden:select.getAttribute('aria-hidden'),focused:document.activeElement===select};
  const wrapper=document.createElement('div');wrapper.className='tick-select';
  const trigger=document.createElement('button');trigger.type='button';trigger.className='select-trigger';
  trigger.id=select.id+'-control';trigger.setAttribute('role','combobox');trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');
  const value=document.createElement('span');value.className='select-value';value.id=select.id+'-value';
  trigger.setAttribute('aria-describedby',value.id);
  const chevron=document.createElementNS('http://www.w3.org/2000/svg','svg');chevron.setAttribute('viewBox','0 0 24 24');chevron.setAttribute('class','select-chevron');chevron.setAttribute('aria-hidden','true');chevron.innerHTML='<path d="m7 10 5 5 5-5"/>';
  trigger.append(value,chevron);wrapper.append(trigger);select.after(wrapper);
  const label=document.querySelector(`label[for="${select.id}"]`);if(label){label.htmlFor=trigger.id;label.id=select.id+'-label';trigger.setAttribute('aria-labelledby',label.id);}
  select.hidden=true;select.tabIndex=-1;select.setAttribute('aria-hidden','true');
  const popup=document.createElement('div');popup.className='select-popover';popup.id=select.id+'-listbox';popup.setAttribute('role','listbox');popup.setAttribute('aria-label',label?.textContent||'Options');popup.hidden=true;
  trigger.setAttribute('aria-controls',popup.id);document.body.append(popup);
  let active=select.selectedIndex,open=false,typeahead='',typeTimer;
  const options=[...select.options];
  const optionEls=options.map((option,index)=>{
    const item=document.createElement('div');item.id=select.id+'-option-'+index;item.className='select-option';item.setAttribute('role','option');
    item.setAttribute('aria-label',option.textContent);item.innerHTML=renderOption(option)+'<svg class="option-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4 10-10"/></svg>';
    item.addEventListener('pointermove',()=>highlight(index,false));item.addEventListener('click',()=>choose(index));popup.append(item);return item;
  });
  function refresh(){value.innerHTML=renderOption(options[select.selectedIndex]);optionEls.forEach((el,i)=>el.setAttribute('aria-selected',String(i===select.selectedIndex)));}
  function position(){if(!open)return;const r=trigger.getBoundingClientRect(),gap=8,desired=Math.min(options.length*49+12,354),below=innerHeight-r.bottom-gap-12,above=r.top-gap-12,up=below<Math.min(200,desired)&&above>below;
    const height=Math.max(80,Math.min(desired,up?above:below)),width=Math.min(Math.max(r.width,218),innerWidth-24);
    popup.style.width=width+'px';popup.style.maxHeight=height+'px';popup.style.left=Math.max(12,Math.min(r.left,innerWidth-width-12))+'px';popup.style.top=(up?r.top-gap-height:r.bottom+gap)+'px';popup.classList.toggle('opens-up',up);
  }
  function highlight(index,scroll=true){active=Math.max(0,Math.min(options.length-1,index));optionEls.forEach((el,i)=>el.classList.toggle('highlighted',i===active));trigger.setAttribute('aria-activedescendant',optionEls[active].id);if(scroll)optionEls[active].scrollIntoView({block:'nearest'});}
  function show(){document.dispatchEvent(new CustomEvent('tick:select-open',{detail:trigger.id}));open=true;popup.hidden=false;trigger.setAttribute('aria-expanded','true');wrapper.classList.add('is-open');position();highlight(select.selectedIndex);}
  function close(){open=false;popup.hidden=true;trigger.setAttribute('aria-expanded','false');trigger.removeAttribute('aria-activedescendant');wrapper.classList.remove('is-open');}
  function choose(index){select.selectedIndex=index;refresh();close();trigger.focus({preventScroll:true});select.dispatchEvent(new Event('change',{bubbles:true}));}
  trigger.addEventListener('click',()=>open?close():show());
  trigger.addEventListener('keydown',e=>{
    if(['ArrowDown','ArrowUp','Home','End','Enter',' ','Escape'].includes(e.key)){e.preventDefault();e.stopPropagation();
      if(e.key==='Escape'){close();return;}
      if(e.key==='Enter'||e.key===' '){open?choose(active):show();return;}
      if(!open){show();return;}
      highlight(e.key==='Home'?0:e.key==='End'?options.length-1:active+(e.key==='ArrowDown'?1:-1));return;
    }
    if(e.key==='Tab'){close();return;}
    if(e.key.length===1&&!e.metaKey&&!e.ctrlKey){e.preventDefault();clearTimeout(typeTimer);typeahead+=e.key.toLowerCase();typeTimer=setTimeout(()=>typeahead='',700);const i=options.findIndex(o=>o.textContent.toLowerCase().startsWith(typeahead));if(i>=0){if(!open)show();highlight(i);}}
  });
  const outside=e=>{if(!wrapper.contains(e.target)&&!popup.contains(e.target))close();};
  const other=e=>{if(e.detail!==trigger.id)close();};
  const focus=e=>{if(open&&!wrapper.contains(e.target)&&!popup.contains(e.target))close();};
  document.addEventListener('pointerdown',outside);document.addEventListener('focusin',focus);document.addEventListener('tick:select-open',other);
  window.addEventListener('resize',position);window.addEventListener('scroll',position,true);select.addEventListener('change',refresh);refresh();if(previous.focused)trigger.focus({preventScroll:true});
  return ()=>{const focused=document.activeElement===trigger;close();clearTimeout(typeTimer);popup.remove();wrapper.remove();if(label)label.htmlFor=select.id;select.hidden=previous.hidden;for(const [name,value] of [['tabindex',previous.tabIndex],['aria-hidden',previous.ariaHidden]]){if(value===null)select.removeAttribute(name);else select.setAttribute(name,value);}document.removeEventListener('pointerdown',outside);document.removeEventListener('focusin',focus);document.removeEventListener('tick:select-open',other);window.removeEventListener('resize',position);window.removeEventListener('scroll',position,true);select.removeEventListener('change',refresh);if(focused)select.focus({preventScroll:true});};
}
