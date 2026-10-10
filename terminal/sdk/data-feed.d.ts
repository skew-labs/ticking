export type ReferenceQuality='liquid_reference'|'thin_reference'|'quote_only'|'disputed_quote_only'|'disputed_pool_reference'|'legacy_reference';
export interface CandleQuality {basis:'observed'|'reference'|'estimated'|'mixed'|'carried';observedMinutes:number;referenceMinutes:number;estimatedMinutes:number;carriedMinutes:number}
export interface Candle { time:number;open:number;high:number;low:number;close:number;volume:number;baseVolume:number;count:number;synthetic?:boolean;sourceTime?:number;gapKind?:'unverified_no_record';commitment?:'finalized'|'processed_observation';sources?:Array<'binance_spot'|'binance_alpha'|'binance_usdm'|'solana_reference'|'solana_pool_mark'|'binance_spot_gap'|'binance_alpha_gap'|'gate_spot_reference'|'alpha_spot_reference'|'bybit_spot_reference'|'ticking_reference_reconstruction'|'okx_spot_reference'|'okx'|'gate'|'mexc'|'binance_spot_reference'|'solana_wrapped_reference'>;quoteCurrency?:string;mixedSource?:boolean;
 /** Modelled reference OHLC, not recovered executions. synthetic denotes a carried close. */
 estimated?:boolean;referenceOnly?:boolean;quality?:CandleQuality;components?:Array<{provider:string;market?:string;open:number;high:number;low:number;close:number;received:number;digest?:string}>;excludedComponents?:Array<{provider:string;market:string;reason:string}>;referenceBasis?:string;estimateMethod?:'median_venue_ohlc_v1'|'linear_between_recorded_endpoints'|'previous_reference_close_to_recorded_price';
 /** Endpoint timestamps in Unix seconds; chart time is in milliseconds. */
 anchors?:number[];receipt?:string;conversionReceipt?:string;referenceQualities?:ReferenceQuality[];sourceMarket?:string;sourceBaseVolume?:number;sourceQuoteVolume?:number;priceConversion?:'none'|'same_minute_USDCUSDT_close'; }
export interface DisplayToken { token:string;expires:number;market?:string;venue?:string;origin?:string;pool?:string|null;mint?:string;quoteMint?:string;aggregation?:'pool'|'token'|'exchange' }
export interface FeedOptions {
 mint?:string;pool?:string;market?:string;apiBase?:string;aggregation?:'pool'|'token'|'exchange';history?:'full'|'executions';
 tokenProvider?:(request:{mint?:string;market?:string;pool?:string;aggregation:'pool'|'token'|'exchange';origin:string;signal:AbortSignal})=>Promise<DisplayToken>;
 onStatus?:(response:HistoryResponse|FeedStatus)=>void;
 refreshMs?:number;
 onConnection?:(state:{state:"connecting"|"streaming"|"retrying";publishedAt?:number})=>void;
 onUpdate?:(response:HistoryResponse)=>void;
}
export interface HistoryResponse {
 mode:'executed_history'|'asset_history'|'exchange_history';live:false;observationAvailable?:boolean;refresh_seconds:number;pool:string|null;mint:string|null;quoteMint:string;quote:string;interval:string;updated_at:number|null;aggregation?:'token'|'exchange';
 history?:{source?:string;sourceLabel?:string;earliest?:number;resolutionStarts?:Record<string,number>;market:string;start:number;archiveQuote:string;currentQuote:string;sourceTransition?:number;policy?:string;priceConversion?:'none'}|null;
 transport?:"stream";publishedAt?:number;
 candles:Candle[];revision:number;cursor:number;hasMore:boolean;nextBefore:number|null;
 coverage:{complete:false;source?:string;archiveCompleteness?:'not_attested';shredJoinVerified?:false;commitment?:string;canonicalFinalityVerified?:false;seamPolicy?:string;policy?:string;priceSampling?:string;quoteMint?:string;nativeQuoteReference?:boolean;observedPoolCount?:number;pricedPoolCount?:number;unpricedQuotePoolCount?:number;quoteConversion?:string;precisionRule?:string;volumeDefinition?:string;displayPolicy?:'attributed_reference_composite_v1';supportMinutes?:number;reconstructedMinutes?:number;referencePolicy?:string;referenceQualities?:ReferenceQuality[]};
 secondaryReferences?:Array<{underlying:string;mint:string;wrappedSymbol:string;price:number;quote:string;pools:string[];observedAt:number;slot:number;grade:ReferenceQuality;stale:boolean;source:string;basis:string}>;
 composition?:{policy:string;quote:string;volume:string;sources:string[];componentSources:Record<string,{provider:string;market:string;quote:string;instrumentType:string}>};
 identity:{asset?:string;network:'solana'|'exchange';venue?:string;instrumentType?:'spot'|'perpetual'|'reference';baseMint:string;quoteMint:string;market:string;inverted?:boolean;series?:'trade'|'reference';aggregation?:string;historyPolicy?:string};stale:boolean;fillPolicy:'recorded'|'carry';carriedBars:number;gapDisclosure?:string|null;loadingHistory?:boolean;
}
export interface FeedStatus {error?:string;loading?:boolean;retained?:boolean;displayedInterval?:string}
export function createDataFeed(options?:FeedOptions):{subscribe(interval:string,onData:(data:HistoryResponse)=>void,onState?:(state:unknown)=>void):()=>void;history(interval?:string,before?:number|null):Promise<HistoryResponse>;trades():Promise<unknown>;cancelPending():void;destroy():void};
export function bindDataFeed(chart:unknown,options?:FeedOptions):()=>void;
