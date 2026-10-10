import type {ChartEngine,TradingOverlay} from './types.js';
export interface SolanaAuctionIdentity {network:'mainnet-beta'|'devnet'|'testnet';venue:string;market:string;program:string;baseMint:string;quoteMint:string}
export interface PublicSolanaAuction {id:string;visibility:'public';side:'buy'|'sell';status:'open'|'filled'|'cancelled'|'expired';commitment:'processed'|'confirmed'|'finalized';slot:number;startSlot:number;endSlot:number;observedAt:number;price:number;remainingBaseAtoms:string;signature?:string}
export interface SolanaAuctionSnapshot {sequence:number;slot:number;auctions:PublicSolanaAuction[]}
export interface SolanaAuctionUpdate extends SolanaAuctionSnapshot {removed?:string[]}
export interface SolanaAuctionAdapter {identity:SolanaAuctionIdentity;snapshot(options:{minSequence:number}):Promise<SolanaAuctionSnapshot>;subscribe(callback:(update:SolanaAuctionUpdate)=>void,options:{afterSequence:number;onGap:()=>Promise<boolean>}):()=>void}
export class SolanaAuctionBook {constructor(identity:SolanaAuctionIdentity,options?:{maxAuctions?:number});readonly identity:Readonly<SolanaAuctionIdentity>;readonly sequence:number;readonly slot:number;readonly needsRecovery:boolean;replace(snapshot:SolanaAuctionSnapshot):this;apply(update:SolanaAuctionUpdate):boolean;active():Readonly<PublicSolanaAuction>[];overlays(limit?:number):TradingOverlay[]}
export interface SolanaAuctionConnection {book:SolanaAuctionBook;recover():Promise<boolean>;disconnect():void}
export function connectSolanaAuctions(engine:ChartEngine,adapter:SolanaAuctionAdapter,options?:{maxAuctions?:number}):Promise<SolanaAuctionConnection>;
