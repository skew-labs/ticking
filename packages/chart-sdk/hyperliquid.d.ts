import type {ChartEngine,MarketAdapter} from './types.js';
export interface HyperliquidNodeOptions { market:string; interval?:1|3|5|15|30|60|120|240|360|480|720|1440|4320|10080; baseURL?:string; accessToken:()=>Promise<string>|string; fetcher?:typeof fetch; signal?:AbortSignal; }
export function createHyperliquidNodeAdapter(options:HyperliquidNodeOptions):Promise<MarketAdapter&{selection:unknown;trades(options?:{before?:number;limit?:number}):Promise<unknown>;book():Promise<unknown>;destroy():void}>;
export function connectHyperliquidNodeChart(chart:ChartEngine,options:HyperliquidNodeOptions):Promise<{feed:MarketAdapter;destroy():void}>;
