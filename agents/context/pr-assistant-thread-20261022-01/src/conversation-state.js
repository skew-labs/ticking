import {loadThread,resetThread} from './assistant-thread.js';
import {resolveBody,acceptResult,currentContext,contextFor,resultActions,routeContext} from './investigation-context.js';
import {accountStorageKey,cloudDocument} from './workspace-cloud.js';
import {clearWalletResearch} from './wallet-research.js';
import {validateIssues} from './asset-issues.js';
import {publicQuery} from './query-safety.js';
import {clearAgentWallet} from './agent-wallet-context.js';
import {clearWalletToolResults} from './agent-wallet-tools.js';
// A bounded, tab-local conversation. No account tokens, keys or wallet access.
const KEY='ticking-intelligence-thread-v1',MAX=30;
let memory={version:1,messages:[]};
export const safeRoute=r=>typeof r==='string'&&(/^#\/console\/workspace\/(?:arbitrum|robinhood|arc|ethereum|base|optimism|linea|polygon|unichain|hyperevm|xlayer)%3[Aa]0x[0-9a-f]{40}\?panel=wallets&wallet=0x[0-9a-f]{40}$/.test(r)||/^#\/console\/map\?chain=bitcoin&focus=(?:bc1[a-z0-9]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})&from=BTC$/.test(r)||/^(#\/(markets|analytics|blog|news|docs|docs\/agent-tools|developers|pricing|console|console\/billing|console\/agent))$/.test(r)||/^#\/console\/workspace(?:\/[A-Za-z0-9_.:%-]+)?$/.test(r)||/^#\/(token|terminal|investigate)\/[A-Za-z0-9_.:%-]+(?:\?(?:(?:block|time|from|to)=[0-9]+|(?:window=(?:1h|24h|7d|30d|90d|all)|interval=[1-9][0-9]{0,3}[mhdw]))(?:&(?:(?:block|time|from|to)=[0-9]+|(?:window=(?:1h|24h|7d|30d|90d|all)|interval=[1-9][0-9]{0,3}[mhdw])))*)?$/.test(r)||/^#\/developers\?mint=[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(r)||/^#\/console\/workspace\/[A-Za-z0-9_.:%-]{1,120}\?map=open(?:&focus=0x[a-f0-9]{40})?$/.test(r)||/^#\/console\/map(?:\?chain=(?:solana|arbitrum|robinhood|arc|ethereum|base|optimism|linea|polygon|unichain|hyperevm|xlayer)(?:&focus=(?:0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}))?(?:&from=[A-Za-z0-9_.%-]{1,120})?)?$/.test(r)||/^#\/\?view=map(?:&chain=(solana|arbitrum|robinhood|arc|ethereum|base|optimism|linea|polygon|unichain|hyperevm|xlayer))?(?:&address=(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}))?$/.test(r));
export function safeQuery(text){
 return publicQuery(text);
}
function messageId(m){
 if(typeof m.id==='string'&&/^[-A-Za-z0-9_]{1,80}$/.test(m.id))return m.id;
 // Deterministic migration: old messages keep their DOM identity after trimming.
 let hash=2166136261;for(const c of String(m.role)+'|'+m.at+'|'+m.text)hash=Math.imul(hash^c.charCodeAt(0),16777619);
 return 'legacy_'+m.at+'_'+(hash>>>0).toString(36);
}
function savedIssues(c){try{return c?validateIssues(c):null;}catch{return null;}}
function clean(value){
 if(value?.version!==1||!Array.isArray(value.messages))return {version:1,messages:[]};
 return {version:1,messages:value.messages.slice(-MAX).filter(m=>m&&['user','assistant'].includes(m.role)&&typeof m.text==='string'&&m.text.length<=5000&&Number.isFinite(m.at)).map(m=>({id:messageId(m),role:m.role,text:m.text,at:m.at,source:String(m.source||'').slice(0,80),walletToolRef:/^aw-[0-9a-f-]{36}$/.test(m.walletToolRef)?m.walletToolRef:null,issues:savedIssues(m.issues),walletResearchRef:/^wallet-[0-9a-f]{16}$/.test(m.walletResearchRef)?m.walletResearchRef:null,investigationRef:Number.isFinite(m.investigationRef)?m.investigationRef:null,context:String(m.context||'').slice(0,100),links:(Array.isArray(m.links)?m.links:[]).slice(0,8).filter(l=>safeRoute(l?.route)&&typeof l.title==='string').map(l=>({title:l.title.slice(0,120),route:l.route})),...(m.card&&typeof m.card==='object'&&!Array.isArray(m.card)&&['wallet','top','holders','holders-at','graph','accumulators','trace','economy','investigation','cluster','first_entry','xc'].includes(m.card.kind)&&JSON.stringify(m.card).length<16000?{card:m.card}:{})}))};
}
export function conversation(){try{const raw=sessionStorage.getItem(accountStorageKey(KEY));if(raw&&raw.length<=160000)memory=clean(JSON.parse(raw));}catch{}return clean(memory);}
export function rememberMessage(message){const state=conversation();state.messages.push({...message,id:'m_'+crypto.randomUUID(),at:Date.now()});memory=clean(state);try{sessionStorage.setItem(accountStorageKey(KEY),JSON.stringify(memory));}catch{}window.dispatchEvent(new Event('tickingconversation'));return memory.messages.at(-1);}
export function clearConversation(){resetThread();clearWalletResearch();clearAgentWallet();clearWalletToolResults();memory={version:1,messages:[]};try{sessionStorage.removeItem(accountStorageKey(KEY));}catch{}window.dispatchEvent(new Event('tickingconversation'));}
export function rememberResolution(result){
 acceptResult(result);
 const extra=resultActions(result).filter(a=>safeRoute(a.route));
 const links=result.state==='ready'&&safeRoute(result.route)?[{title:result.title||'Open view',route:result.route}]:(result.choices||[]).filter(c=>safeRoute(c.route));
 rememberMessage({role:'assistant',text:result.message||(links.length===1?'Ready: '+links[0].title+'.':'Choose a view to continue.'),links:[...links,...extra],source:result.resolver==='economic-machine-wallets'?'Economic Machine · wallets':result.resolver==='economic-machine'?'Economic Machine':'Muse · navigation',...(result.card?{card:result.card}:{})});
}

let cloudConversation=null,applyingCloud=false;
window.addEventListener('tickingaccountchange',event=>{cloudConversation?.destroy();if(event.detail.previous){memory={version:1,messages:[]};window.dispatchEvent(new Event('tickingconversation'));}cloudConversation=event.detail.id?cloudDocument('conversation','main',{read:conversation,apply:value=>{applyingCloud=true;memory=clean(value);try{sessionStorage.setItem(accountStorageKey(KEY),JSON.stringify(memory));}catch{}window.dispatchEvent(new Event('tickingconversation'));applyingCloud=false;}}):null;});
window.addEventListener('tickingconversation',()=>{if(!applyingCloud)cloudConversation?.save();});

// Restore this user's server thread when the tab has no local conversation yet.
let restored=false;
export async function restoreConversation(){
 if(restored)return conversation();restored=true;
 if(conversation().messages.length)return conversation();
 const t=await loadThread();if(!t||!Array.isArray(t.recent)||!t.recent.length)return conversation();
 const messages=[];for(const r of t.recent){const at=Date.now();if(r.query)messages.push({id:'t_'+r.seq+'_u',role:'user',text:String(r.query).slice(0,5000),at,source:'thread'});if(r.summary)messages.push({id:'t_'+r.seq+'_a',role:'assistant',text:String(r.summary).slice(0,5000),at,source:'thread:'+String(r.capability||'').slice(0,60)});}
 memory=clean({version:1,messages});try{sessionStorage.setItem(accountStorageKey(KEY),JSON.stringify(memory));}catch{}
 window.dispatchEvent(new Event('tickingconversation'));return conversation();
}
if(typeof window!=='undefined')queueMicrotask(()=>restoreConversation());
