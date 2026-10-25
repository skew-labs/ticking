import {insertWalletPrompt,walletPromptText} from './wallet-prompt-drop.js';
import {rememberMessage,rememberResolution,safeRoute} from './conversation-state.js';
import {resolveIntelligence} from './intelligence-client.js';
import {resolveBody,acceptResult,currentContext,contextFor,resultActions,routeContext,retainContext} from './investigation-context.js';
// Ask Ticking beside the investigation, always open on desktop (a bottom sheet on phones).
// Lookups answer at once from what the page already read (0 credits). Anything that computes goes plan → consent → run
// (em-account /intelligence/plan, /run, /runs/<id>, /followup): the plan lists its steps and the exact credits before anything is
// charged; follow-ups are free. Evidence rows point back at the map. Answers say what co-occurred, never what caused it.
// Ask chips open Flow-framed research (source of funds, where it went, related wallets on the same Flow axis).
// Runs still attach evidence; related wallets are Flow series — not topology map expand.
import {api,esc,short,num,usd,day,entitlement} from './explorer-common.js';
import {gradeChip} from './explorer-investigate-map.js';

// Research requests enter the same per-user engine/thread gateway as Ask.

const cname=c=>({arbitrum:'Arbitrum',robinhood:'Robinhood',ethereum:'Ethereum',base:'Base',optimism:'Optimism',polygon:'Polygon',linea:'Linea',unichain:'Unichain',hyperevm:'HyperEVM',arc:'Arc'}[c]||c);

// a start on the map → the question and the scope sent to the planner
// (em-account scope.intent → one graph step: funding_back = trace in, 90 days; forward = trace out, 30 days; related = wallet-links)
const EXPAND={funding:{q:'Where did this money come from before?',scope:{intent:'funding_back',hops:2},busy:'Tracing where the money came from…'},
 next:{q:'Where did the money go next?',scope:{intent:'forward',hops:2},busy:'Following where the money went…'},
 related:{q:'Which mapped wallets move with this one on Flow?',scope:{intent:'related'},busy:'Reading related wallets on Flow…'}};
const EXPAND_ASK=[[/어디서\s*왔|출처|자금원|came from|source of (the )?(funds|money)|funded (it|this)|where .*from\b/i,'funding'],[/그 다음|이후.*(이동|어디)|어디로 갔|went next|go next|where did .*go/i,'next'],[/관련\s*지갑|related wallets|connected wallets|move with this|on Flow/i,'related']];
export function mountAsk(root,{st,signal,highlight,openChart,showSnap,onGraph,onCredits,onBusy,onSaved}){
 const POST=(path,body,signal)=>api(path.replace('/api/public/em/intelligence/','/api/public/intelligence/research/'),{method:'POST',body:{...body,context:canonical()},credentials:'same-origin',signal,timeout:20000});
 root.innerHTML=`<div class="tkv-grip" data-k-grip></div><div class="tkv-ask-h"><h3><span aria-hidden="true">✦</span> Ask Ticking</h3><div class="tkv-cr" data-k-cr>…</div></div>
  <div class="tkv-ctx">About <span class="tk-chip accent" data-k-ctx>…</span></div><div class="tkv-msgs" data-k-msgs aria-live="polite"></div><div class="tkv-sugg" data-k-sugg></div>
  <div class="tkv-ask-in"><form class="tkv-ask-box" data-k-form data-wallet-dropzone><textarea rows="1" data-k-q placeholder="Ask about this…" aria-label="Ask Ticking"></textarea><button aria-label="Send">↑</button></form>
  <div class="tkv-ask-note">Deeper research shows its plan and credit cost before it runs. Lookups are free.</div></div>`;
 injectAskStyles();
 const dropzone=root.querySelector('[data-wallet-dropzone]'),input=root.querySelector('[data-k-q]');dropzone.addEventListener('ticking:wallet-drop',e=>{if(insertWalletPrompt(input,walletPromptText(e.detail)||''))root.classList.add('open');},{signal});dropzone.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('text/plain')){e.preventDefault();e.dataTransfer.dropEffect='copy';dropzone.classList.add('wallet-drop-active');}},{signal});dropzone.addEventListener('dragleave',()=>dropzone.classList.remove('wallet-drop-active'),{signal});dropzone.addEventListener('drop',e=>{dropzone.classList.remove('wallet-drop-active');const text=e.dataTransfer?.getData('text/plain')?.trim();if(insertWalletPrompt(input,text||'')){e.preventDefault();root.classList.add('open');}},{signal});
 const $=s=>root.querySelector(s),msgs=$('[data-k-msgs]');let ent=null,lastRun=null;
 const add=(html,cls)=>{const d=document.createElement('div');d.className=cls;d.innerHTML=html;msgs.append(d);msgs.scrollTop=1e9;return d;};
 entitlement({fresh:true}).then(e=>{ent=e;credits();});
 const resets=t=>t?new Date(t>1e11?t:t*1000).toLocaleDateString('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}):'';
 // the balance and when it resets, beside the question box (em-account credits)
 function credits(c){const cr=c||ent?.credits;$('[data-k-cr]').innerHTML=ent?.signedIn?`${esc(ent.name||ent.tier)} · <b>${cr?.balance??'—'}</b> credit${cr?.balance===1?'':'s'}${cr?.monthly_included?' of '+cr.monthly_included:''}${cr?.trial?' (trial)':''}<br>${cr?.resets_at?'Resets '+resets(cr.resets_at)+' · ':''}Lookups are free`
  :`<a class="tk-link" href="#/console">Sign in</a> for research credits<br>Free accounts get ${ent?.credits?.free_monthly??3} trial credits`;
  const n=$('.tkv-ask-note');if(n)n.textContent=ent?.signedIn?`Deeper research shows its plan and credit cost before it runs. ${cr?.balance!=null?`You have ${cr.balance} credit${cr.balance===1?'':'s'}${cr.resets_at?', reset '+resets(cr.resets_at):''}. `:''}Lookups are free.`:'Deeper research shows its plan and credit cost before it runs. Lookups are free.';}
 $('[data-k-grip]').onclick=()=>root.classList.toggle('open');
 $('[data-k-q]').addEventListener('focus',()=>root.classList.add('open'));
 $('[data-k-q]').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('[data-k-form]').requestSubmit();}});
 $('[data-k-form]').addEventListener('submit',e=>{e.preventDefault();const q=$('[data-k-q]').value.trim();if(!q)return;$('[data-k-q]').value='';
  const s=suggestions().find(x=>x.q.toLowerCase()===q.toLowerCase()),x=st.mode==='wallet'&&EXPAND_ASK.find(([re])=>re.test(q));
  s?ask(s):x?ask({q,expand:x[1]}):lastRun?followup(q):plan(q);});
 // evidence rows carry the map ids (or addresses) they stand for
 msgs.addEventListener('click',e=>{const li=e.target.closest('[data-k-ids]');if(li){highlight(JSON.parse(li.dataset.kIds));if(innerWidth<=980)root.classList.remove('open');return;}
  const a=e.target.closest('[data-k-act]');if(!a)return;const [k,arg]=JSON.parse(a.dataset.kAct);if(k==='snap')showSnap(arg);else if(k==='chart')openChart(arg);else if(k==='map')highlight(arg);if(innerWidth<=980)root.classList.remove('open');});

 function canonical(){
  const selected=st.selectedContext;
  if(selected?.schema==='ticking.investigation-context.v1'){
   const c=contextFor(selected.subject,selected.selection,selected.task);if(c)return c;
  }
  const point=st.snaps?.[st.snap]||{},requested=st.requestedSelection||{};
  const selection=Object.keys(requested).length?requested:point.requestedTime!=null?{time:point.requestedTime}:point.block!=null?{block:point.block}:point.t!=null?{time:point.t}:st.window?{window:st.window}:{};
  return contextFor(st.mode==='token'?{kind:'token',asset:st.token?.id,symbol:st.token?.symbol}:{kind:'wallet',address:st.address,...(st.chain?{chain:st.chain}:{})},selection);
 }
 function subject(){const s=canonical()?.subject||{};return st.mode==='token'?{kind:'token',market:s.asset}:{kind:'wallet',address:s.address,...(s.chain?{chain:s.chain}:{})};}
 function suggestions(){
  if(st.mode==='token')return [{q:'Who accumulated this asset recently?',scope:{intent:'asset_flow'}},{q:'Show this token’s inflow and outflow',scope:{intent:'asset_flow'}},{q:'Who held it at this moment?',local:holdersNow},{q:'What happened in the last 24 hours?',scope:{window:'24h'}}];
  return [{q:'Show this wallet’s Flow (in / out / holdings)',scope:{intent:'wallet_flow'}},{q:'Which mapped wallets move with this one on Flow?',expand:'related'},{q:'What does it hold?',local:holds},{q:'Where did this money come from before?',expand:'funding'}];}
 function intro(){retainContext(canonical());msgs.innerHTML='';lastRun=null;
  $('[data-k-ctx]').textContent=st.mode==='token'?`${st.token?.symbol||'Token'} · ${cname(st.token?.chain)}`:`Wallet ${short(st.address)}`;
  $('[data-k-q]').placeholder=st.mode==='token'?`Ask about ${st.token?.symbol||'this token'} Flow…`:'Ask a Flow question about this wallet…';
  if(st.mode==='wallet'&&st.model){const m=st.model,known=m.observedChainCount>0,hasActivity=known&&Number.isFinite(m.homeC?.txs),w=m.nodes.filter(n=>n.kind==='wallet'||n.kind==='exchange').length;
   add(`<p>${hasActivity?`Returned activity on <b>${esc(cname(m.home))}</b>: ${num(m.homeC.txs)} recorded transactions${m.homeC.first?' since '+day(m.homeC.first):''}.`:'This exact wallet subject is selected. Activity and chain coverage have not been established by the returned source.'}</p>
    <p>${m.graphAvailable?`${num(w)} linked wallet rows returned by the bounded graph read. Transfer connections do not prove common ownership.`:'Connection coverage is unavailable; no zero links or ownership is inferred.'} Open a returned row for its on-chain balances and evidence.</p><div class="tkv-src">From the Economic Machine · lookup, 0 credits</div>`,'tkv-bot');}
  else if(st.mode==='token')add(`<p><b>${esc(st.token?.symbol||'This token')}</b> on ${esc(cname(st.token?.chain))}. Ask who accumulated or moved it — answers surface as Flow charts (in/out/holdings), not a holder bubble map. A transfer link does not prove common ownership.</p><div class="tkv-src">From the Economic Machine · lookup, 0 credits</div>`,'tkv-bot');
  $('[data-k-sugg]').innerHTML=suggestions().map((s,i)=>`<button type="button" data-k-s="${i}">${esc(s.q)}${s.local?' <small>0 credits</small>':''}</button>`).join('');
  root.querySelectorAll('[data-k-s]').forEach(b=>b.onclick=()=>ask(suggestions()[+b.dataset.kS]));}
 function cachedContextMatches(body,page){
  const key=c=>{if(!c?.subject)return null;const s=c.subject,selection=c.selection||{};return JSON.stringify([s.kind,s.chain||'',s.address?.toLowerCase()||'',s.asset?.toLowerCase()||'',s.entity||'',Object.keys(selection).sort().map(k=>[k,selection[k]])]);};
  const expected=key(page);return !!expected&&[body.context,body.previousContext].filter(Boolean).every(c=>key(c)===expected);
 }
 function ask(s){root.classList.add('open');add(esc(s.q),'tkv-user');if(s.local){const page=canonical(),body=resolveBody(s.q,{},page);if(!cachedContextMatches(body,page)){plan(s.q);return;}const r=s.local();answer(r);}else if(s.expand){const x=EXPAND[s.expand];plan(s.q,x.scope,s.expand);}else plan(s.q,s.scope);}
 function expand(kind){const x=EXPAND[kind];if(!x)return;ask({q:x.q,expand:kind});}
 const busy=t=>{st.expanding=t||null;onBusy?.();};
 const ev=(g,text,ids)=>`<li ${ids?`data-k-ids='${esc(JSON.stringify(ids))}'`:''}>${gradeChip(g)}<span>${esc(text)}</span></li>`;
 function answer({p,rows=[],against,acts=[],src='Lookup · 0 credits'}){
  add(`${p.map(x=>`<p>${x}</p>`).join('')}${rows.length||against?`<ul class="tkv-evl">${rows.join('')}${against?`<li class="counter"><span>⚖</span><span>Against: ${esc(against)}</span></li>`:''}</ul>`:''}
   ${acts.length?`<div class="tkv-kacts">${acts.map(([t,a])=>`<button type="button" class="tk-btn sm" data-k-act='${esc(JSON.stringify(a))}'>${esc(t)}</button>`).join('')}</div>`:''}<div class="tkv-src">${esc(src)}</div>`,'tkv-bot');}
 // ---------- lookups from what the page already read
 function whereElse(){const m=st.model;
  if(!m||!(m.observedChainCount>0))return {p:['Cross-chain presence was not established for this address. Registry coverage does not establish wallet coverage; no default chain or common owner is inferred.']};
  const lanes=m.lanes.filter(l=>!m.chains.find(c=>c.id===l.id)?.coverageUnknown);
  const rows=lanes.map(l=>{const c=m.chains.find(x=>x.id===l.id)||{},same=c.sameOwner===true;
   return ev(same?'same':'unknown',`Returned address presence on ${l.name}${c.txs!=null?` · ${num(c.txs)} recorded txs`:''}${c.first?' since '+day(c.first):''}${same?' · source-confirmed EOA key':' · ownership not established by address alone'}`,l.id===m.home?['self']:['self','self-on:'+l.id]);});
  const br=m.links.filter(l=>l.g==='matched'||l.g==='inferred').slice(0,3).map(l=>ev(l.g,l.why,[l.a,l.b]));
  return {p:[`Returned presence on ${lanes.length} chain${lanes.length===1?'':'s'}: ${lanes.map(l=>esc(cname(l.id))).join(', ')}. Additional chains may be unavailable in this source.`,m.contract?'Contract control can differ between chains.':'Common control requires explicit EOA or other ownership evidence; it is not inferred from an unclassified address.'],rows:[...rows,...br],against:'A transfer or shared address alone does not establish institutional ownership.',acts:[['Show returned chains',['map',['self',...lanes.filter(l=>l.id!==m.home).map(l=>'self-on:'+l.id)]]]]};}
 function partners(){const m=st.model;
  if(!m?.graphAvailable)return {p:['Connection coverage was not returned. No zero partners or common ownership is inferred.']};
  const ls=m.links.filter(l=>l.g==='proven').sort((a,b)=>(b.usd||0)-(a.usd||0)).slice(0,5);
  if(!ls.length)return {p:['No direct transfer partner rows were returned in this bounded source read. This does not prove zero activity or zero counterparties.']};
  return {p:[`Up to ${ls.length} returned transfer partners. Transfer-map edges cover their reported source scope, often since first seen; protocol/trade windows are separate. Exchanges end further ownership tracing, while on-chain balances can still be read.`],rows:ls.map(l=>{const n=m.nodes.find(x=>x.id===l.b);return ev('proven',`${n?.label||''} — ${l.why}`,[l.a,l.b]);}),acts:[['Show returned partners',['map',['self',...ls.map(l=>l.b)]]]]};}
 function holds(){const m=st.model,all=m?.balanceRows||[],held=all.filter(r=>r.held),usable=all.filter(r=>!r.held),cards=Object.values(st.data?.cards||{}),partial=cards.some(c=>c?.sheet?.partial),truncated=cards.some(c=>c?.positionsTruncated),unpriced=usable.filter(r=>r.valueUsd==null).length;
  const ranked=usable.slice().sort((a,b)=>(b.valueUsd??-Infinity)-(a.valueUsd??-Infinity)).slice(0,5);
  const notes=[partial?'The balance sheet is partial; priced amounts are not the full portfolio.':'',truncated?'The bounded source positions are truncated.':'',unpriced?`${unpriced} returned position${unpriced===1?' has':'s have'} no verified price.`:'',held.length?`${held.length} returned position amount${held.length===1?' is':'s are'} held: ${[...new Set(held.map(r=>r.reason||'source reconciliation'))].map(esc).join('; ')}.`:''].filter(Boolean);
  if(!ranked.length){const trades=(m?.assets||[]).filter(r=>r.valueUsd!=null).slice(0,5);return {p:[held.length?'Returned balance amounts are withheld by the source. No zero holdings are inferred.':'Actual balance positions were not returned. This does not establish that the wallet owns no assets.',...notes,...(trades.length?[`Separate trading-position analytics: ${trades.map(r=>`${esc(r.symbol||'Unknown asset')} ${usd(r.valueUsd)}`).join(', ')}. These are not a complete wallet balance sheet.`]:[]) ]};}
  return {p:[`Returned balance positions: ${ranked.map(r=>`<b>${esc(r.symbol||'Unknown asset')}</b> ${typeof r.amountText==='string'?esc(r.amountText):r.amount==null?'amount unavailable':num(r.amount)+' (source precision)'}${r.valueUsd==null?' · price unavailable':' · '+usd(r.valueUsd)} · ${esc(cname(r.chain))}${r.kind?' · '+esc(r.kind):''}`).join('; ')}.`,...notes,'Acquisition method is reported only where its source provides it; these positions are not all assumed to be DEX purchases.'],acts:ranked.filter(r=>r.market).slice(0,2).map(r=>[`Chart ${r.symbol}`,['chart',r]])};}
 function holdersNow(){const r=st.shares?.[st.snap];
  if(st.hold?.held)return {p:['Holder amounts or ownership shares are held by the source. '+esc(st.hold.reason||st.hold.heldReason||'Source reconciliation pending.')]};
  if(st.tokenPrep||!r||r instanceof Promise||r.available===false)return {p:['No proven holder rows are available for the selected point. No latest snapshot or zero holders is substituted. '+esc(r?.reason||st.hold?.reason||'')]};
  return {p:[`${num(r.holders?.length??0)} holder rows returned at the selected source point${r.block!=null?' · block '+num(r.block):''}. ${r.complete===true?'The source marks this snapshot complete.':'Snapshot coverage is partial or unverified; returned rows are not the full holder population.'}`]};}
 function linked(){const r=st.shares?.[st.snap],c=r?.connections;
  if(st.hold?.held||st.tokenPrep||!r||r instanceof Promise||r.available===false)return {p:['Recorded connection evidence is unavailable for this selected holder point. No latest snapshot or common ownership is substituted.']};
  return {p:[c?`${c.links.length} recorded token-transfer connections and ${c.groups.length} transfer groups among the displayed holders. The map and Connections tab show the same evidence. ${esc(c.reason||'This is a bounded sample of graph reads, not a complete relationship scan.')}`:'The map is reading recorded token-transfer links between displayed holders. Open Connections for the returned evidence.'],against:'A transfer group is not proof of common ownership. Pools and exchanges do not merge holder groups.'};}
 // ---------- plan → consent → run → follow-up
 async function plan(q,scope,kind){
  rememberMessage({role:'user',text:q});
  if(!kind&&!lastRun){try{const r=await resolveIntelligence(q,{context:canonical(),signal});if(r.state!=='clarify'&&r.message){rememberResolution(r);answer({p:[esc(r.message)],acts:[],src:'Economic Machine · shared investigation'});if(r.card)import('./em-api.js').then(m=>{if(!signal.aborted){m.injectStyles();add(m.assistantCard(r.card),'tkv-bot');}});const links=[...(r.choices||[]),...resultActions(r),...(r.route?[{title:r.title||'Open view',route:r.route}]:[])].filter(x=>safeRoute(x.route));if(links.length)add(links.map(x=>`<a class="tk-btn sm" href="${esc(x.route)}">${esc(x.title)}</a>`).join(' '),'tkv-bot');return;}if(r.state==='clarify'){rememberResolution(r);answer({p:[esc(r.message)]});return;}}catch(e){add(`<p class="tkx-error">${esc(e.message)}</p>`,'tkv-bot');return;}}
  const requested=canonical()?.selection||{};if(Object.keys(requested).some(k=>['block','time','from','to'].includes(k))){answer({p:['The research planner cannot preserve this selected historical point yet. Keep this point for exact holder reads or explicitly select a current research window.']});return;}const pending=add('<div class="tkv-prog"><div class="run"><i></i>Planning what to read…</div></div>','tkv-bot');
  try{const p=await POST('/api/public/em/intelligence/plan',{question:q,subject:subject(),...(scope?{scope}:{})},signal);pending.remove();p.expand=kind||null;
   const cost=p.estimate_credits||0,steps=p.steps||[];
   // the source-of-funds start runs only on a step that reads backwards: a forward trace would answer a different question
   if(kind==='funding'&&!steps.some(x=>/funding_back|fund|source|origin|inflow/.test(x.kind||'')||x.direction==='in'||x.dir==='in')){
    add('<p>Tracing where money came from, hop by hop: <span class="tkv-prep">Preparing</span>. Forward traces run today; ask Flow for inflow/outflow over time on this wallet.</p>','tkv-bot');return;}
   const card=add(`<div class="tkv-pt">Plan</div><ul>${steps.map(s=>`<li>${esc(s.title||s.kind)} <small>${s.credits?s.credits+' credit'+(s.credits>1?'s':''):'0 · already computed'}</small></li>`).join('')}</ul>
    <div class="tkv-cost">${p.requires_signin?`<a class="tk-btn primary sm" href="#/console">Sign in to run${cost?' · '+cost+' credit'+(cost>1?'s':''):''}</a>`:p.affordable===false?`<a class="tk-btn primary sm" href="#/pricing">See plans</a>`:`<button type="button" class="tk-btn primary sm" data-k-run>${cost?`Run · ${cost} credit${cost>1?'s':''}`:'Run · free'}</button>`}
    <span class="tk-muted">${p.credits?`You have ${p.credits.balance}`:''}</span></div>
    ${p.requires_signin?`<p class="tkv-why"><b>Sign in to run this: deep research needs an account.</b> A free account gets ${ent?.credits?.free_monthly??3} trial credits; the quote above stays the same.</p>`
     :p.affordable===false?`<p class="tkv-why"><b>Not enough credits: this plan needs ${cost}, you have ${p.credits?.balance??0}.</b> ${p.credits?.resets_at?'Credits reset '+resets(p.credits.resets_at)+'; ':''}narrow the plan (fewer hops or chains) or move to Pro.</p>`:''}
    ${(p.notes||[]).length?`<div class="tkv-src">${esc(p.notes.join(' '))}</div>`:''}`,'tkv-plan');
   if(p.credits)credits(p.credits);
   card.querySelector('[data-k-run]')?.addEventListener('click',()=>run(p,card));
   if(!cost&&!p.requires_signin)run(p,card);}
  catch(e){pending.remove();
   if(kind==='funding'&&e.status===422)add('<p>Tracing where money came from, hop by hop: <span class="tkv-prep">Preparing</span>. The research engine follows money forward today; the map already shows who sent to this wallet directly.</p>','tkv-bot');
   else add(`<p class="tkx-error">${esc(e.message)}</p>`,'tkv-bot');}}
 async function run(p,card){const cost=p.estimate_credits||0;card.classList.add('done');card.querySelector('.tkv-cost').innerHTML=`<span class="tkv-ok">✓ Running${cost?' · '+cost+' credits':''}</span>`;
  if(p.expand)busy(EXPAND[p.expand]?.busy);let found=0;
  try{const r=await POST('/api/public/em/intelligence/run',{plan_id:p.plan_id,confirm_credits:cost},signal);if(r.balance!=null&&ent?.credits)credits({...ent.credits,balance:r.balance});if(cost)onCredits?.(cost);
   const prog=add(`<div class="tkv-prog">${(p.steps||[]).map(s=>`<div data-k-step="${esc(s.id)}"><i></i>${esc(s.title||s.kind)}</div>`).join('')}</div>`,'tkv-bot');
   let done={};for(let i=0;i<120&&!signal.aborted;i++){const x=await api('/api/public/intelligence/research/runs/'+encodeURIComponent(r.run_id),{credentials:'same-origin',signal}).catch(()=>null);
    for(const s of x?.steps||[]){const row=prog.querySelector(`[data-k-step="${CSS.escape(s.id)}"]`);if(row)row.className=s.status==='done'?'ok':s.status==='failed'?'fail':'run';
     if(!done[s.id]&&(s.status==='done'||s.status==='failed')){done[s.id]=1;stepAnswer(s);}}
    if(x&&['done','partial','failed'].includes(x.status)){lastRun=r.run_id;if(x.status!=='failed'){st.lastRun=r.run_id;onSaved?.();}
     // the run's graph (every graph step merged) goes on the map once; older runs without it: each step that carries a graph
     if(onGraph&&st.mode==='wallet'){const gs=(x.steps||[]).filter(s=>s.status==='done'),title=gs.find(s=>/^graph\./.test(s.kind))?.title||gs[0]?.title||p.question;
      if(x.graph?.nodes?.length){found=onGraph({kind:p.expand||'graph',step:{title,data:x.graph,hops:p.hops,credits:cost}})||0;
       const g=x.graph,e=g.edges||g.links||[];add(`<p>The research found ${num(g.nodes.length)} wallet${g.nodes.length===1?'':'s'} and ${num(e.length)} link${e.length===1?'':'s'}${(g.stops||[]).length?`; tracing stopped at ${g.stops.length} hub${g.stops.length>1?'s':''}`:''}.</p>`,'tkv-bot');}
      else for(const s of gs)if(s.data?.nodes||Object.values(s.data||{}).some(v=>v?.nodes))found+=onGraph({kind:p.expand||s.kind,step:s})||0;}if(x.credits_refunded){add(`<p class="tk-muted">${x.credits_refunded} credit${x.credits_refunded>1?'s':''} refunded for a step that failed.</p>`,'tkv-bot');onCredits?.(-x.credits_refunded);}
     if(p.expand)add(found?`<p><b>${found} related wallet${found>1?'s':''} ready for Flow</b> — open them as series on the same time axis (not a topology expand).</p>`:'<p class="tk-muted">This run found no new related wallets for Flow.</p>','tkv-bot');
     add('<p class="tk-muted">Ask a follow-up about this result: it is free.</p>','tkv-bot');break;}
    await new Promise(ok=>setTimeout(ok,1500));}}
  catch(e){const d=e.status===402?'Not enough credits for this plan. Narrow it (fewer hops or chains), wait for the monthly reset, or move to Pro.':e.status===401?'Sign in to run this: deep research needs an account.':e.status===410?'The quote expired after 30 minutes. Ask again for a new plan.':e.message;
   add(`<p class="tkx-error">${esc(d)}</p>${e.status===402?'<a class="tk-btn sm" href="#/pricing">See plans</a>':''}`,'tkv-bot');}
  finally{if(p.expand)busy(null);}}
 // one finished step → a short, sourced summary; rows that name wallets light them up on the map
 function stepAnswer(s){if(s.status==='failed'){add(`<p class="tk-muted">${esc(s.title||s.kind)}: could not finish (${esc(s.error||'error')}).</p>`,'tkv-bot');return;}
  const d=s.data||{},rows=[];let p='';
  if(s.kind==='wallet.crosschain'){const cs=(d.chains||[]).map(c=>c.chain).filter(Boolean);p=`On ${cs.length} chain${cs.length>1?'s':''}: ${cs.map(cname).join(', ')}.${d.entity?` Same owner: ${esc(d.entity.link)} (${esc(String(d.entity.basis||'').replace(/_/g,' '))}).`:''}`;
   for(const l of (d.bridges?.legs||[]).slice(0,4)){const g=/exact|structural/.test(l.match?.class)?'matched':l.match?.class==='heuristic'?'inferred':'used';rows.push(ev(g,`${l.bridge} ${l.dir} · ${day(l.ts)}${l.counterparty?.wallet?' · '+short(l.counterparty.wallet):''}`,[st.address,l.counterparty?.wallet].filter(Boolean)));}}
  else if(s.kind==='wallet.counterparties'){for(const [c,g] of Object.entries(d)){const es=(g?.edges||[]).slice(0,4);for(const e of es){const a=String(e.target||'').split(':w:')[1];if(a)rows.push(ev('proven',`${cname(c)}: ${short(a)} · ${usd(e.usd)} · ${num(e.events)} transfers`,[a]));}}p='Largest transfer partners:';}
  else if(s.kind==='wallet.holdings'){const all=Object.entries(d).flatMap(([c,x])=>(x?.tokens||x?.rows||[]).map(t=>({...t,chain:c})));p=`${all.length} token position${all.length===1?'':'s'} on ${Object.keys(d).map(cname).join(', ')}.`;}
  else if(s.kind==='wallet.trace'||s.kind==='wallet.path'){for(const [c,t] of Object.entries(d)){const n=(t?.nodes||[]).filter(x=>x.address).slice(0,5);p+=`${cname(c)}: ${t?.found===false?'no path found':(n.length+' wallets on the trail')}. `;
   for(const x of n)rows.push(ev('proven',`${cname(c)} · ${x.name||'Wallet'} ${short(x.address)}`,[x.address]));}}
  else if(s.kind==='market.investigate'){p=`${d.symbol||'Token'}: ${d.price?.changePct!=null?(d.price.changePct>0?'+':'')+Number(d.price.changePct).toFixed(1)+'% ':''}${d.volume?.usd!=null?'on '+usd(d.volume.usd)+' volume':''}, ${num(d.volume?.traders)} traders, ${(d.clusters||[]).length} linked group${(d.clusters||[]).length===1?'':'s'}. Events in the range co-occur; they are not shown as causes.`;
   for(const e of (d.events||[]).slice(0,4))rows.push(ev('proven',`${String(e.kind).replace(/_/g,' ')} · ${usd(e.usd)} · ${(e.text||'').slice(0,90)}`,e.wallets||null));}
  else if(/^graph\./.test(s.kind)&&!(s.data?.graph||s.data)?.nodes){p='done.';}
  else if(/^graph\./.test(s.kind)){const g=s.data?.graph||s.data||{},n=(g.nodes||[]).length,e=g.edges||g.links||[],br=e.filter(x=>x.kind==='bridge').length,stp=(g.stops||[]).length;
   p=`${num(n)} wallet${n===1?'':'s'} and ${num(e.length)} link${e.length===1?'':'s'}${br?` (${br} bridge move${br>1?'s':''})`:''}${stp?`; tracing stopped at ${stp} hub${stp>1?'s':''}`:''}.`;
   for(const x of e.slice(0,4))rows.push(ev(x.grade||'proven',`${String(x.a||'').split(':')[0]} ${short(String(x.a||'').split(':').pop())} → ${short(String(x.b||'').split(':').pop())}${x.txCount!=null?' · '+num(x.txCount)+' transfers':''}${x.symbol?' of '+x.symbol:''}`,[String(x.b||'').split(':').pop()]));}
  else if(s.kind==='cluster.first_entry'){p=d.first?`First in: ${short(d.first.address)} on ${day(d.first.time)}.`:'No first buy recorded.';}
  else if(s.kind==='wallet.profile'){p=`${esc(d.categoryName||'Wallet')}${d.tx_out!=null?` · ${num(d.tx_out)} sent, ${num(d.tx_in)} received`:''}${d.first_seen?' · since '+day(d.first_seen):''}.`;}
  else p=esc(s.title||s.kind)+': done.';
  answer({p:[`<b>${esc(s.title||s.kind)}</b> — ${p}`],rows,src:s.credits?`${s.credits} credit${s.credits>1?'s':''} · re-running gives the same evidence`:'Already computed · 0 credits'});}
 async function followup(q){const pend=add('<div class="tkv-prog"><div class="run"><i></i>Reading the stored result…</div></div>','tkv-bot');
  try{const r=await POST('/api/public/em/intelligence/followup',{run_id:lastRun,question:q},signal);pend.remove();
   answer({p:(r.sections||[]).map(x=>`<b>${esc(x.title||x.kind)}</b> — ${esc(x.summary||'')}`).concat(r.sections?.length?[]:['Nothing in the stored result answers that.']),src:'Follow-up · 0 credits'});
   if(r.needs_plan){const p={...r.needs_plan,steps:r.needs_plan.steps||[]};const card=add(`<div class="tkv-pt">This needs more work</div><p>${esc(r.needs_plan.why||'')}</p><div class="tkv-cost"><button type="button" class="tk-btn primary sm" data-k-run>Run · ${p.estimate_credits} credits</button></div>`,'tkv-plan');card.querySelector('[data-k-run]').onclick=()=>run(p,card);}}
  catch(e){pend.remove();if(e.status===409||e.status===404){lastRun=null;plan(q);}else add(`<p class="tkx-error">${esc(e.message)}</p>`,'tkv-bot');}}
 return {intro,ask,expand,destroy(){}};
}

let styled=false;
function injectAskStyles(){if(styled)return;styled=true;const s=document.createElement('style');s.textContent=`
.tkv-ask-h{padding:14px 16px 12px;border-bottom:1px solid var(--tk-line);display:flex;align-items:center;gap:10px}.tkv-ask-h h3{margin:0;font:700 16px var(--tk-display);display:flex;gap:7px;align-items:center}.tkv-ask-h h3 span{color:var(--tk-accent)}
.tkv-cr{margin-left:auto;font-size:12px;color:var(--tk-muted);text-align:right;line-height:1.3}.tkv-cr b{color:var(--tk-ink)}
.tkv-ctx{display:flex;align-items:center;gap:6px;padding:10px 16px;font-size:12.5px;color:var(--tk-muted);border-bottom:1px solid var(--tk-line)}
.tkv-msgs{flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.tkv-user{align-self:flex-end;max-width:86%;background:var(--tk-accent);color:var(--tk-accent-ink);padding:9px 13px;border-radius:14px 14px 4px 14px;font-weight:500;font-size:13.5px}
.tkv-bot{background:var(--tk-bg-sub);border:1px solid var(--tk-line);border-radius:14px;padding:12px 13px;font-size:13.5px}.tkv-bot p{margin:0}.tkv-bot p+p{margin-top:6px}
.tkv-src{font-size:11.5px;color:var(--tk-faint);margin-top:8px}
.tkv-evl{list-style:none;padding:0;margin:10px 0 0;display:grid;gap:4px}.tkv-evl li{display:flex;align-items:flex-start;gap:8px;font-size:12.5px;padding:5px 6px;border-radius:8px;min-width:0}
.tkv-evl li[data-k-ids]{cursor:pointer}.tkv-evl li[data-k-ids]:hover{background:var(--tk-surface)}.tkv-evl li.counter{color:var(--tk-muted)}
.tkv-kacts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.tkv-plan{border:1px dashed var(--tk-line-strong);border-radius:14px;padding:12px 13px;font-size:13px}.tkv-plan.done{opacity:.8}.tkv-pt{font-size:12px;color:var(--tk-muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.tkv-plan ul{margin:6px 0 10px;padding-left:18px;color:var(--tk-ink-2)}.tkv-plan small{color:var(--tk-muted)}.tkv-cost{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.tkv-ok{color:var(--tk-up);font-weight:700;font-size:12.5px}
.tkv-prog{display:grid;gap:6px;font-size:12.5px;color:var(--tk-muted)}.tkv-prog div{display:flex;gap:8px;align-items:center}.tkv-prog i{width:14px;height:14px;border-radius:50%;border:2px solid var(--tk-line-strong);flex:none}
.tkv-prog .ok i{border-color:var(--tk-up);background:var(--tk-up)}.tkv-prog .fail i{border-color:var(--tk-down)}.tkv-prog .run i{border-color:var(--tk-accent);border-right-color:transparent;animation:tkvspin .7s linear infinite}
@keyframes tkvspin{to{transform:rotate(360deg)}}
.tkv-sugg{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 10px}.tkv-sugg button{border:1px solid var(--tk-line-strong);background:var(--tk-surface);border-radius:999px;padding:6px 11px;font:600 12.5px var(--tk-font);color:var(--tk-ink-2);text-align:left;cursor:pointer}
.tkv-sugg button:hover{border-color:var(--tk-accent);color:var(--tk-accent)}.tkv-sugg small{color:var(--tk-up);font-weight:600}
.tkv-ask-in{padding:10px 12px 12px;border-top:1px solid var(--tk-line)}.tkv-ask-box.wallet-drop-active{outline:2px solid #6de9dc;outline-offset:3px;box-shadow:0 0 25px #6de9dc33}.tkv-ask-box{display:flex;align-items:flex-end;gap:8px;border:1.5px solid var(--tk-line-strong);border-radius:14px;padding:8px 8px 8px 12px;background:var(--tk-surface)}
.tkv-ask-box:focus-within{border-color:var(--tk-accent)}.tkv-ask-box textarea{flex:1;border:0;outline:0;resize:none;background:none;font:14px/1.45 var(--tk-font);color:var(--tk-ink);max-height:120px;min-height:22px}
.tkv-ask-box button{width:32px;height:32px;border-radius:9px;border:0;background:var(--tk-accent);color:var(--tk-accent-ink);font-weight:800;cursor:pointer}
.tkv-ask-note{font-size:11.5px;color:var(--tk-faint);margin-top:6px;padding-left:4px}.tkv-why{margin:8px 0 0;font-size:12.5px;color:var(--tk-muted)}.tkv-why b{color:var(--tk-ink)}.tkv-grip{display:none}
@media (max-width:980px){.tkv-grip{display:block;width:40px;height:4px;border-radius:9px;background:var(--tk-line-strong);margin:7px auto 0;cursor:pointer}
 .tkv-right:not(.open) .tkv-ask-h,.tkv-right:not(.open) .tkv-ctx,.tkv-right:not(.open) .tkv-msgs,.tkv-right:not(.open) .tkv-sugg,.tkv-right:not(.open) .tkv-ask-note{display:none}.tkv-right:not(.open) .tkv-ask-in{border-top:0}}
`;document.head.append(s);}
