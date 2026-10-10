import type {ChartEngine,ChartSDK,MarketAdapter,PublicSigningKey} from './types.js';
export type * from './types.js';
export function createChartSDK(options:{licenseProvider:()=>Promise<{license:string}>;publicKey:PublicSigningKey;usageEndpoint?:string;onLicenseError?:(error:Error)=>void}):Promise<ChartSDK>;
export function connectData(engine:ChartEngine,adapter:MarketAdapter,options?:{quote?:string;limit?:number}):Promise<()=>void>;
