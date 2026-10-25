import {accountStorageKey} from './workspace-cloud.js';
// One stable assistant thread per browser/user. The server keeps the turns
// (api.ticking.dev /api/public/intelligence/thread); this only carries its id.
const KEY='ticking-assistant-thread-id-v1',ID=/^[A-Za-z0-9_-]{16,64}$/,SCOPE='/api/public/intelligence/';
export function threadId(){try{const v=localStorage.getItem(accountStorageKey(KEY));return ID.test(v||'')?v:null;}catch{return null;}}
function keep(v){if(ID.test(v||''))try{localStorage.setItem(accountStorageKey(KEY),v);}catch{}}
function scoped(input){try{const u=new URL(typeof input==='string'?input:input.url,location.href);return u.origin===location.origin&&u.pathname.startsWith(SCOPE);}catch{return false;}}
if(!window.__tickingThreadFetch){
 const base=window.fetch.bind(window);window.__tickingThreadFetch=true;
 window.fetch=async(input,init={})=>{
  if(!scoped(input))return base(input,init);
  const headers=new Headers(init.headers||(input instanceof Request?input.headers:undefined));const id=threadId();
  if(id)headers.set('X-Ticking-Thread',id);
  const response=await base(input,{...init,headers});keep(response.headers.get('X-Ticking-Thread'));return response;
 };
}
export async function loadThread(signal){try{const r=await fetch(SCOPE+'thread',{credentials:'omit',signal});return r.ok?await r.json():null;}catch{return null;}}
export async function resetThread(){try{await fetch(SCOPE+'thread',{method:'DELETE',credentials:'omit'});}catch{}}
