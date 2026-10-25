import {resolveIntelligence} from './intelligence-client.js';
import {conversation,rememberMessage,rememberResolution,safeRoute} from './conversation-state.js';
import {resolveBody,acceptResult,currentContext,contextFor,resultActions,routeContext} from './investigation-context.js';
import {serviceHeaders,observeServiceResponse} from './workspace-services.js';
// The Ticking assistant, always one click away in the header (owner direction 2026-10-20). The account service decides what it
// allows (em-account limits.assistant: daily, used_today, remaining, resets_at, preview): signed out, one preview question a day;
// Free, 5 a day; Pro, no daily cap (fair use). The server enforces it (401 gate signin, 429 daily_limit); this drawer shows what is
// left, the credits and their reset date, and when it blocks, one line on why and one on what to do. Answers come from the same
// resolver as the workspace assistant and keep its Economic Machine cards; the page's wallet, token or protocol is passed as context.
// Not investment advice.
import {esc,entitlement,injectExplorerStyles,short} from './explorer-common.js';

const EXAMPLES=['Who accumulated ARB last week?','Arbitrum Foundation wallet Flow','Show Whale Tracker large flows','Where did funds go from 0xdd998ed81234148f9b74092f6759c005c5751d3c?'];
let drawer=null,ent=null,blocked=null,opening=0,pending=null;
const dt=t=>t?new Date(t>1e11?t:t*1000):null;
const hm=t=>{const d=dt(t);return d?d.toISOString().slice(11,16)+' UTC':'00:00 UTC';};
const dmy=t=>{const d=dt(t);return d?d.toLocaleDateString('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}):'';};
// what is left today: the account's numbers, else the plan table (staging overrides, an older account service)
function allowance(){const a=ent?.limits?.assistant;if(a)return {daily:a.daily,fair:a.fair_use_daily??null,left:a.daily==null?Infinity:Math.max(0,a.remaining??a.daily-(a.used_today||0)),resets:a.resets_at,preview:!!a.preview};
 const d=ent?.signedIn?ent.assistantDaily:1;return {daily:d===Infinity?null:d,left:d===Infinity?Infinity:d,resets:null,preview:!ent?.signedIn};}
export async function openAssistant(prefill=''){
 injectExplorerStyles();injectAssistantStyles();
 if(!drawer){drawer=document.createElement('aside');drawer.className='tk tkx-asst';drawer.setAttribute('role','dialog');drawer.setAttribute('aria-label','Ticking assistant');drawer.setAttribute('aria-modal','false');document.body.append(drawer);
  drawer.addEventListener('click',onClick);drawer.addEventListener('submit',onSubmit);document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawer&&!drawer.hidden)close();});
  window.addEventListener('hashchange',()=>{if(drawer&&!drawer.hidden)close();});window.addEventListener('tickingconversation',()=>{if(drawer&&!drawer.hidden&&!pending)render(drawer.querySelector('textarea')?.value||'');});}   // a new page closes it; the conversation stays for the next open
 drawer.hidden=false;document.documentElement.classList.add('tkx-asst-open');place();
 // Paint the current conversation before the allowance request. A delayed
 // refresh must never replace text the user has already started entering.
 render(prefill);const version=++opening;
 const next=await entitlement({fresh:true});if(version!==opening)return;ent=next;
 if(allowance().left>0)blocked=null;
 const form=drawer.querySelector('[data-a-form]'),pending=drawer.querySelector('[data-a-pending]');
 const stopped=blocked||(allowance().left<=0?(ent?.signedIn?'limit':'signin'):null);
 if(!pending&&((!form&&!stopped)||(form&&stopped)))render(form?.querySelector('textarea')?.value||prefill);
 else {const statusNode=drawer.querySelector('[data-a-status]');if(statusNode)statusNode.innerHTML=status();}
}
// below the sticky header, so the header (search, Ask, account) stays usable while the assistant is open
function place(){const h=document.querySelector('[data-tkx-header]');drawer.style.top=Math.max(0,h?h.getBoundingClientRect().bottom:0)+'px';}
function close(){pending?.abort();pending=null;drawer.hidden=true;document.documentElement.classList.remove('tkx-asst-open');document.querySelector('[data-tkx-ask]')?.focus();}
function context(){const c=currentContext();return c?{...c.subject,label:c.subject.symbol||c.subject.asset||c.subject.address||c.subject.protocol||c.subject.entity}:null;}
function status(){const a=allowance(),c=ent?.credits;
 const ask=!ent?.signedIn?(a.left>0?'Signed out · 1 preview question today':'Signed out · today\'s preview is used')
  :a.daily==null?`${esc(ent.name||'Pro')} · ${a.fair?'up to '+a.fair+' questions a day (fair use)':'no daily cap (fair use)'}`:`${esc(ent.name||'Free')} · ${a.left} of ${a.daily} questions left today · resets ${hm(a.resets)}`;
 const cr=ent?.signedIn&&c&&c.balance!=null?`<span class="tkx-asst-cr"><b>${Number(c.balance).toLocaleString('en-US')}</b> research credit${c.balance===1?'':'s'}${c.resets_at?' · resets '+dmy(c.resets_at):''}</span>`:'';
 return `${ask}${cr?'<br>'+cr:''}`;}
// one line on why it is blocked, one on what to do
function gateHTML(kind){const a=allowance();
 if(kind==='signin')return `<div class="tkx-asst-limit tkx-gate"><b>Sign in to keep asking: signed-out visitors get one preview question a day.</b><p>A free account asks 5 questions a day and gets 3 trial research credits.</p><div><a class="tk-btn primary" href="#/console">Sign in</a> <a class="tk-btn" href="#/pricing">See plans</a></div></div>`;
 if(kind==='fair')return `<div class="tkx-asst-limit tkx-gate"><b>Today's fair-use limit of ${a.fair??300} questions is reached; it reopens at ${hm(a.resets)}.</b><p>Lookups and the investigation pages keep working in the meantime.</p></div>`;
 return `<div class="tkx-asst-limit tkx-gate"><b>You have used today's ${a.daily??5} questions; they reset at ${hm(a.resets)}.</b><p>Pro has no daily cap. Lookups on the investigation pages stay free.</p><a class="tk-btn primary" href="#/pricing">See Pro</a></div>`;}
function render(prefill){const a=allowance(),ctx=context();
 const head=`<header class="tkx-asst-h"><div><b>Ticking assistant</b><small data-a-status>${status()}</small></div><button type="button" class="tkx-icon" data-a-close aria-label="Close the assistant">×</button></header>`;
 const out=conversation().messages.map(m=>`<div class="tkx-asst-m ${m.role==='user'?'you':''}"><b>${m.role==='user'?'You':'Ticking'}</b><p>${esc(m.text)}</p>${(m.links||[]).filter(l=>safeRoute(l.route)).map(l=>`<a class="tk-btn sm" href="${esc(l.route)}">${esc(l.title)}</a>`).join(' ')}</div>`).join('');
 const stop=blocked||(a.left<=0?(ent?.signedIn?'limit':'signin'):null);
 drawer.innerHTML=head+`<div class="tkx-asst-b" data-a-log aria-live="polite">${out||`<p class="tk-muted">${ctx?'Asking about this page: '+esc(ctx.label)+'.':'Ask a Flow question — accumulation, fund routes, whale/asset movement. Answers mount Flow charts.'}${!ent?.signedIn&&a.left>0?' One preview question is free without an account.':''}</p>${EXAMPLES.map(q=>`<button type="button" class="tkx-asst-q" data-a-eg ${stop?'disabled':''}>${esc(q)}</button>`).join('')}`}</div>
  ${stop?gateHTML(stop):`<form class="tkx-asst-f" data-a-form><textarea name="q" rows="2" placeholder="Ask who moved an asset, a wallet Flow, or Whale Tracker" aria-label="Your question" maxlength="500">${esc(prefill)}</textarea><button class="tk-btn primary">Ask</button></form>`}
  <p class="tk-disclaimer tkx-asst-d">Not investment advice. Answers show what wallets did on-chain and how sure we are.</p>`;
 drawer.querySelector('textarea')?.focus();}
function onClick(e){if(e.target.closest('[data-a-close]'))return close();const eg=e.target.closest('[data-a-eg]');if(eg){const t=drawer.querySelector('textarea');if(t){t.value=eg.textContent;drawer.querySelector('[data-a-form]').requestSubmit();}}
 if(e.target.closest('a[href^="#/"]'))close();}
async function onSubmit(e){if(!e.target.matches('[data-a-form]'))return;e.preventDefault();const t=e.target.q,q=t.value.trim();if(!q)return;
 if(pending)return;if(allowance().left<=0){render();return;}
 const log=drawer.querySelector('[data-a-log]');if(!log.dataset.started){log.innerHTML='';log.dataset.started='1';}
 const controller=new AbortController();pending=controller;const query=q;rememberMessage({role:'user',text:q});
 log.insertAdjacentHTML('beforeend',`<div class="tkx-asst-m you"><b>You</b><p>${esc(q)}</p></div><div class="tkx-asst-m" data-a-pending><b>Ticking</b><p class="tk-muted">Reading the chain…</p></div>`);t.value='';log.scrollTop=log.scrollHeight;
 try{const d=await resolveIntelligence(query,{signal:controller.signal,onProgress:text=>{const n=log.querySelector('[data-a-pending] p');if(n)n.textContent=text;}});
  if(controller.signal.aborted||pending!==controller)return;acceptResult(d);rememberResolution(d);
  let card='';if(d.card){try{const m=await import('./em-api.js');m.injectStyles();card=m.assistantCard(d.card);}catch{}}
  const links=[...(d.choices||[]),...resultActions(d)].filter(c=>safeRoute(c.route)).slice(0,4).map(c=>`<a class="tk-btn sm" href="${esc(c.route)}">${esc(c.title||'Open')}</a>`).join(' ')+(d.route&&String(d.route).startsWith('#/')?`<a class="tk-btn sm" href="${esc(d.route)}">${esc(d.title||'Open view')}</a>`:'');
  log.querySelector('[data-a-pending]').outerHTML=`<div class="tkx-asst-m"><b>Ticking</b><p>${esc(d.message||'No answer.')}</p>${card}${links?`<p>${links}</p>`:''}</div>`;}
 catch(err){if(controller.signal.aborted)return;const d=err.data||{},code=d?.detail?.code||d?.code||d?.gate?.reason;if(err.status===401&&(d.gate||code==='signin'))blocked='signin';if(err.status===429&&/fair/.test(code||''))blocked='fair';else if(err.status===429&&/daily/.test(code||d?.detail?.error||''))blocked='limit';log.querySelector('[data-a-pending]')?.remove();log.insertAdjacentHTML('beforeend',`<div class="tkx-asst-m"><b>Ticking</b><p class="tkx-error">${esc(err.message)}</p></div>`);}
 finally{if(pending===controller)pending=null;}
 log.scrollTop=log.scrollHeight;
 // the account counts the question: read what is left now
 ent=await entitlement({fresh:true});const s=drawer.querySelector('[data-a-status]');if(s)s.innerHTML=status();if(allowance().left<=0)render();}

let styled=false;
function injectAssistantStyles(){if(styled)return;styled=true;const s=document.createElement('style');s.textContent=`
.tkx-asst{position:fixed;top:0;right:0;bottom:0;width:min(440px,100vw);z-index:70;background:var(--tk-surface);border-left:1px solid var(--tk-line);box-shadow:var(--tk-shadow-pop);display:flex;flex-direction:column}
.tkx-asst[hidden]{display:none}.tkx-asst-h{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;padding:16px;border-bottom:1px solid var(--tk-line)}.tkx-asst-h b{font:700 var(--tk-fs-lg) var(--tk-display)}.tkx-asst-h small{display:block;color:var(--tk-muted);font-size:var(--tk-fs-xs)}
.tkx-asst-h .tkx-icon{font-size:22px}.tkx-asst-b{flex:1;overflow:auto;padding:16px;display:grid;grid-template-columns:minmax(0,1fr);gap:10px;align-content:start}.tkx-asst-q{overflow-wrap:anywhere}
.tkx-asst-q{display:block;width:100%;text-align:left;padding:10px 12px;border:1px solid var(--tk-line);border-radius:10px;background:var(--tk-bg-sub);color:var(--tk-ink-2);font:500 var(--tk-fs-sm) var(--tk-font);cursor:pointer}.tkx-asst-q[aria-disabled=true]{cursor:default;opacity:.7}
.tkx-asst-eg{margin:6px 0 0;color:var(--tk-muted);font-size:var(--tk-fs-xs);font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.tkx-asst-m{padding:10px 12px;border-radius:12px;background:var(--tk-bg-sub)}.tkx-asst-m.you{background:var(--tk-accent-soft)}.tkx-asst-m b{font-size:var(--tk-fs-xs);color:var(--tk-muted)}.tkx-asst-m p{margin:4px 0}
.tkx-asst-f{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--tk-line)}.tkx-asst-f textarea{flex:1;resize:none;border:1px solid var(--tk-line-strong);border-radius:10px;padding:8px 10px;font:inherit;background:var(--tk-surface);color:var(--tk-ink)}
.tkx-asst-limit{margin:12px 16px}.tkx-asst-cr{color:var(--tk-accent)}.tkx-asst-q:disabled{opacity:.55;cursor:default}.tkx-asst-d{padding:0 16px 12px}.tkx-error{color:var(--tk-down)}
`;document.head.append(s);}
