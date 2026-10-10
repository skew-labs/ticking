import './tick-embed.js';
import {createTradeFeed,mountExecutionTape} from './trade-feed.js';
/** Free renderer; project credentials select paid managed supply. */
class TickingEmbed extends HTMLElement{
 connectedCallback(){this.controller=new AbortController();this.start();}
 disconnectedCallback(){this.alive=false;this.controller?.abort();this.tapeDispose?.();this.feed?.destroy();this.replaceChildren();}
 async start(){this.alive=true;this.textContent='Connecting your market…';let grant;
  const base=this.getAttribute('api-base')||'https://api.ticking.dev',project=this.getAttribute('project'),market=this.getAttribute('market')||'SOL',trades=this.hasAttribute('trades');
  try{
  const origin=new URL(base);if(origin.username||origin.password||origin.search||origin.hash||origin.protocol!=='https:'&&!(origin.protocol==='http:'&&['localhost','127.0.0.1'].includes(origin.hostname)))throw Error('Use an HTTPS API origin.');
  const provider=async()=>{if(grant&&grant.expires*1000>Date.now()+15000)return grant;const r=await fetch(new URL('/api/embeds/'+encodeURIComponent(project)+'/token',base),{method:'POST',credentials:'omit',signal:this.controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({market,trades})});if(!r.ok){const d=await r.json();const error=Error(d.detail||'Chart access unavailable.');error.status=r.status;error.retry=Math.max(5000,Math.min(60000,(Number(r.headers.get('Retry-After'))||15)*1000));throw error;}return grant=await r.json();};
  for(;;){try{await provider();break;}catch(e){if(!this.alive||e.name==='AbortError')return;if(![409,429,502,503,504].includes(e.status))throw e;this.textContent=e.status===409?'Waiting for verified token data…':'Reconnecting to the chart engine…';await new Promise(resolve=>{const finish=()=>{clearTimeout(t);this.controller.signal.removeEventListener('abort',finish);resolve();},t=setTimeout(finish,e.retry||15000);this.controller.signal.addEventListener('abort',finish,{once:true});});}}if(!this.alive)return;const chart=document.createElement('tick-chart');chart.setAttribute('api-base',base);chart.setAttribute('market',market);chart.setAttribute('aggregation',grant.aggregation);if(grant.mint)chart.setAttribute('mint',grant.mint);chart.setAttribute('interval',this.getAttribute('interval')||'1m');chart.setAttribute('theme',this.getAttribute('theme')||'light');if(this.hasAttribute('tools'))chart.setAttribute('tools','');chart.tokenProvider=provider;this.replaceChildren(chart);
   if(trades){const tape=document.createElement('div');tape.style.cssText='max-height:220px;overflow:auto;font:12px system-ui';this.append(tape);this.feed=createTradeFeed({tokenProvider:provider,apiBase:base});this.tapeDispose=mountExecutionTape(tape,this.feed);}
  }catch(e){if(this.alive)this.textContent=e.message;}
 }
}
if(!customElements.get('ticking-embed'))customElements.define('ticking-embed',TickingEmbed);
