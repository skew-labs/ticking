import type {MarketAdapter} from './types.js';
export function createSolanaMarketAdapter(options:{market:string;baseURL?:string;interval?:1;fetcher?:typeof fetch;eventSource?:typeof EventSource}):Promise<MarketAdapter>;
