import type { DisplayToken } from './data-feed.js';
/** Prices and volumes keep each execution's native quote; no USD conversion is implied. */
export interface Execution { identity:string; mint?:string; quote_mint?:string; pool?:string; signature?:string; slot?:number; tx_order?:number; event_order?:number; tradeId?:number; side?:"A"|"B"; hash?:string; time?:number; price:number; base_volume:number; quote_volume?:number; commitment?:string }
export interface ExecutionPage { trades:Execution[]; identity:{network:'solana'|'exchange';market:string;baseMint?:string;venue?:string;quoteMint?:string;series?:string}; hasMore?:boolean; nextBefore?:string|null; commitment?:string; canonicalFinalityVerified?:false; complete?:false; receipt?:string; cursor?:string }
export interface TradeFeed { subscribe(onData:(data:ExecutionPage)=>void,onState?:(state:{state:string;status?:number})=>void):()=>void; history(options?:{before?:string;limit?:number;signal?:AbortSignal}):Promise<ExecutionPage>; destroy():void }
export function tradeIdentity(row:Execution):string;
export function createTradeFeed(options:{tokenProvider:()=>Promise<DisplayToken>;apiBase?:string}):TradeFeed;
export function mountExecutionTape(root:HTMLElement,feed:TradeFeed):()=>void;
