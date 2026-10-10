/** Read-only public auction state. Protocol decoders and access credentials live
 * in the host's data service. An auction is never an execution or OHLCV input. */
const address=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const text=(value,max)=>typeof value==='string'&&value.length>0&&value.length<=max;
const slot=value=>Number.isSafeInteger(value)&&value>=0;
function identity(value){
  if(!value||!['mainnet-beta','devnet','testnet'].includes(value.network)||!text(value.venue,80)||!text(value.market,160)||![value.program,value.baseMint,value.quoteMint].every(v=>typeof v==='string'&&address.test(v)))throw Error('Invalid Solana auction identity');
  return Object.freeze(Object.fromEntries(['network','venue','market','program','baseMint','quoteMint'].map(k=>[k,value[k]])));
}
function auction(value,atSlot){
  if(!value||value.visibility!=='public'||!text(value.id,160)||!['buy','sell'].includes(value.side)||!['open','filled','cancelled','expired'].includes(value.status)||!['processed','confirmed','finalized'].includes(value.commitment)||!slot(value.slot)||value.slot>atSlot||!slot(value.startSlot)||!slot(value.endSlot)||value.endSlot<value.startSlot||!Number.isSafeInteger(value.observedAt)||value.observedAt<=0||!Number.isFinite(value.price)||value.price<=0||typeof value.remainingBaseAtoms!=='string'||!/^(0|[1-9][0-9]{0,39})$/.test(value.remainingBaseAtoms))throw Error('Invalid public Solana auction');
  if(value.signature!=null&&(typeof value.signature!=='string'||!/^[1-9A-HJ-NP-Za-km-z]{64,88}$/.test(value.signature)))throw Error('Invalid Solana event signature');
  if(value.status==='open'&&value.remainingBaseAtoms==='0')throw Error('An open auction must have remaining size');
  if(value.status==='filled'&&value.remainingBaseAtoms!=='0')throw Error('A filled auction cannot have remaining size');
  // Do not carry wallets, signed intents, balances or account payloads forward.
  return Object.freeze(Object.fromEntries(['id','visibility','side','status','commitment','slot','startSlot','endSlot','observedAt','price','remainingBaseAtoms','signature'].filter(k=>value[k]!==undefined).map(k=>[k,value[k]])));
}
function batch(value,max){
  if(!value||!Number.isSafeInteger(value.sequence)||value.sequence<0||!slot(value.slot)||!Array.isArray(value.auctions)||value.auctions.length>max||value.removed!=null&&(!Array.isArray(value.removed)||value.removed.length>max||value.removed.some(id=>!text(id,160))))throw Error('Invalid sequenced Solana auction batch');
  const auctions=value.auctions.map(a=>auction(a,value.slot)),removed=value.removed||[],ids=[...auctions.map(a=>a.id),...removed];
  if(new Set(ids).size!==ids.length)throw Error('Duplicate auction IDs in one batch');
  return {sequence:value.sequence,slot:value.slot,auctions,removed};
}
export class SolanaAuctionBook {
  constructor(market,{maxAuctions=500}={}){
    if(!Number.isInteger(maxAuctions)||maxAuctions<1||maxAuctions>2000)throw Error('Auction retention must be 1–2000');
    this.identity=identity(market);this.maxAuctions=maxAuctions;this.sequence=-1;this.slot=0;this.needsRecovery=false;this.entries=new Map();
  }
  replace(snapshot){
    const value=batch(snapshot,this.maxAuctions);if(value.removed.length)throw Error('Snapshots must contain complete auction state');
    if(value.sequence<this.sequence)throw Error('Auction recovery snapshot is older than the stream cursor');
    this.entries=new Map(value.auctions.filter(a=>a.status==='open'&&a.endSlot>=value.slot).map(a=>[a.id,a]));this.sequence=value.sequence;this.slot=value.slot;this.needsRecovery=false;return this;
  }
  apply(update){
    const value=batch(update,this.maxAuctions);if(value.sequence<=this.sequence)return false;
    if(this.needsRecovery||value.sequence!==this.sequence+1||value.slot<this.slot){this.needsRecovery=true;throw Error('Solana auction stream gap: obtain an authoritative snapshot')}
    const next=new Map(this.entries);
    for(const a of value.auctions){if(next.has(a.id)&&a.slot<next.get(a.id).slot)throw Error('Auction state moved backwards: obtain an authoritative snapshot');a.status==='open'?next.set(a.id,a):next.delete(a.id)}
    for(const id of value.removed)next.delete(id);
    for(const [id,a] of next)if(a.endSlot<value.slot)next.delete(id);
    if(next.size>this.maxAuctions){this.needsRecovery=true;throw Error('Auction capacity exceeded: narrow the market subscription')}
    this.entries=next;this.sequence=value.sequence;this.slot=value.slot;return true;
  }
  active(){
    if(this.needsRecovery)return [];
    return [...this.entries.values()].filter(a=>a.startSlot<=this.slot&&a.endSlot>=this.slot).sort((a,b)=>b.slot-a.slot||a.id.localeCompare(b.id));
  }
  overlays(limit=100){
    if(!Number.isInteger(limit)||limit<1||limit>100)throw Error('Display 1–100 public auction lines');
    return this.active().slice(0,limit).map(a=>({price:a.price,label:`JIT ${a.side.toUpperCase()} · ${a.commitment} · slot ${a.slot}`,color:a.side==='buy'?'#6d9ed6':'#d8929e'}));
  }
}
const connections=new WeakMap();
export async function connectSolanaAuctions(engine,adapter,options={}){
  const book=new SolanaAuctionBook(adapter.identity,options);connections.get(engine)?.disconnect();
  let closed=false,recovering=null,minimumSequence=0,unsubscribe=()=>{},stopDestroy=()=>{};
  const report=error=>{if(!closed)engine.emit('error',error instanceof Error?error.message:String(error))};
  const publish=()=>{if(!closed&&engine.alive)engine.setMarketOverlays(book.overlays())};
  const recover=()=>{
    if(closed)return Promise.resolve(false);if(recovering)return recovering;
    recovering=Promise.resolve().then(async()=>{try{const snapshot=await adapter.snapshot({minSequence:Math.max(book.sequence,minimumSequence)});if(closed)return false;if(snapshot.sequence<Math.max(book.sequence,minimumSequence))throw Error('Auction recovery snapshot is older than the newest observed stream cursor');book.replace(snapshot);publish();return true}catch(error){report(error);return false}}).finally(()=>{recovering=null});return recovering;
  };
  const visible=()=>{if(!document.hidden){book.needsRecovery=true;publish();void recover()}};
  const hasVisibility=typeof document!=='undefined'&&typeof document.addEventListener==='function';
  const disconnect=()=>{if(closed)return;closed=true;stopDestroy();if(hasVisibility)document.removeEventListener('visibilitychange',visible);try{unsubscribe()}finally{if(connections.get(engine)?.book===book)connections.delete(engine);if(engine.alive)engine.setMarketOverlays([])}};
  const connection={book,recover,disconnect};connections.set(engine,connection);stopDestroy=engine.on('destroy',disconnect);
  if(!engine.alive){disconnect();return connection}
  try{
    const snapshot=await adapter.snapshot({minSequence:0});if(closed)return connection;book.replace(snapshot);publish();
    const stop=adapter.subscribe(update=>{if(closed)return;try{if(Number.isSafeInteger(update?.sequence)&&update.sequence>=0)minimumSequence=Math.max(minimumSequence,update.sequence);if(book.apply(update))publish()}catch(error){book.needsRecovery=true;publish();report(error);void recover()}},{afterSequence:book.sequence,onGap:()=>{book.needsRecovery=true;publish();return recover()}});
    if(typeof stop!=='function')throw Error('Solana auction adapter must return an unsubscribe function');unsubscribe=stop;if(closed)unsubscribe();else if(hasVisibility)document.addEventListener('visibilitychange',visible);return connection;
  }catch(error){disconnect();throw error}
}
