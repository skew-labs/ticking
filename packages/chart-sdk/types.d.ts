export interface Candle {time:number;open:number;high:number;low:number;close:number;volume:number;baseVolume?:number;quoteVolume?:number|null;count?:number}
export type PriceSeries='trade'|'mark'|'index';
export type DrawingTool='cursor'|'trend'|'ray'|'extended'|'horizontal'|'vertical'|'box'|'ellipse'|'arrow'|'text'|'fib'|'measure';
export interface Point {time:number;price:number;y?:number}
export interface Drawing {id:string;type:Exclude<DrawingTool,'cursor'>;points:Point[];locked?:boolean;text?:string;color?:string}
export interface Viewport {bars:number;rightTime:number|null}
export interface PaneLayout {chartStyle?:ChartStyle;interval:1|5|15|60|240|1440;log:boolean;studies:string[];drawings:Drawing[];view:Viewport}
export interface Layout {version:1;mint:string;dataSource?:'native'|'bitquery'|'archive';linked:boolean;panes:PaneLayout[]}
export interface SDKLayout {version:1;market:string;series:PriceSeries;linked:boolean;panes:PaneLayout[]}
export interface MarketIdentity {network:string;venue:string;pool:string;baseMint:string;quoteMint:string;quoteSymbol:string;series:PriceSeries;market?:string}
export interface HistoryPage {deleted?:number[];candles:Candle[];sequence:number;hasMore:boolean;nextBefore:number|null;historyReset?:boolean}
export interface MarketAdapter {identity:MarketIdentity;history(before?:number,limit?:number,options?:{minSequence:number}):Promise<HistoryPage>;subscribe(callback:(update:{deleted?:number[];candles:Candle[];sequence:number;recovery?:boolean;historyReset?:boolean})=>void,options?:{afterSequence:number;onGap:()=>Promise<boolean>}):()=>void}
export interface TradingOverlay {price:number;label:string;color?:string;kind?:string}
export interface VerifiedPosition {entry:number;quantity:number;side:'long'|'short';pnl:number}
export interface Study {id:string;name:string;overlay:boolean;color:string;levels?:number[]}
export interface StudyResult extends Study {levels:number[];lines:{values:(number|null)[];color?:string}[]}
export class StudyWorker {request(candles:Candle[],studies:string[]):Promise<StudyResult[]>;destroy():void}
export interface ChartTheme {background:string;grid:string;axisText:string;up:string;down:string;upGuide:string;downGuide:string;crosshair:string;labelText:string;fontFamily:string}
export interface DisplayOptions {locale?:string;timeZone?:string;priceFormatter?:(value:number)=>string}
export const LIGHT_THEME:Readonly<ChartTheme>;
export const DARK_THEME:Readonly<ChartTheme>;
export function validateTheme(theme:Partial<ChartTheme>):ChartTheme;
export type ChartStyle='candles'|'bars'|'line'|'area';
export interface ChartOptions extends Partial<PaneLayout>,DisplayOptions {chartStyle?:ChartStyle;rightPadding?:number;theme?:Partial<ChartTheme>;indicatorWorker?:StudyWorker;maxCandles?:number;rendererFactory?:(layers:HTMLCanvasElement[])=>Canvas2DRenderer;loadOlder?:(before:number)=>void;recover?:()=>void;drawingColor?:string;noteText?:string;quote?:string;volumeUnit?:string}
export interface RenderDiagnostics {baseFrames:number;annotationFrames:number;crosshairFrames:number;candlesInMemory:number;visibleCandles:number;baseP95Ms:number}
export class ChartEngine {
  constructor(container:HTMLElement,options?:ChartOptions);
  readonly alive:boolean;readonly interval:number;readonly studies:string[];
  setData(candles:Candle[],quote?:string):void;removeCandles(times:number[]):void;update(candles:Candle[]):void;setIndicators(ids:string[]):void;
  setStyle(style:ChartStyle):void;setMagnet(enabled:boolean):void;setDrawingsVisible(visible:boolean):void;lockAllDrawings(locked:boolean):void;clearDrawings():void;takeScreenshot():string;
  setTheme(theme:Partial<ChartTheme>):void;setLog(value:boolean):void;setTool(tool:DrawingTool):void;setViewport(view:Viewport,emit?:boolean):void;linkedCrosshair(point:Point|null):void;
  setTradingOverlays(overlays:TradingOverlay[]):void;setPositions(positions:VerifiedPosition[]):void;
  setMarketOverlays(overlays:TradingOverlay[]):void;
  undo():void;redo():void;reset():void;deleteSelected():void;lockSelected():void;editSelected(patch:Partial<Drawing>):void;
  serialize():PaneLayout;diagnostics():RenderDiagnostics;destroy():void;
  on(event:'viewport'|'crosshair'|'price'|'drawing'|'selection'|'error'|'active'|'destroy',callback:(value:any)=>void):()=>void;
}
export interface RenderScene {candles:Candle[];[key:string]:any}
export class Canvas2DRenderer {constructor(layers:HTMLCanvasElement[],theme?:Partial<ChartTheme>,display?:DisplayOptions);setTheme(theme:Partial<ChartTheme>):void;setDisplay(display:DisplayOptions):void;readonly metrics:{baseFrames:number;annotationFrames:number;crosshairFrames:number;baseMs:number[];annotationMs:number[]};resize(width:number,height:number,dpr:number):void;scene(candles:Candle[],start:number,studies:StudyResult[],log:boolean,quote:string,bars?:number,display?:{rightPadding?:number;priceRange?:{min:number;max:number};chartStyle?:ChartStyle;volumeUnit?:string}):RenderScene;base(scene:RenderScene):void;annotations(scene:RenderScene,drawings:Drawing[],selected:string|null,preview:Drawing|null,orders:TradingOverlay[]):void;crosshair(scene:RenderScene,hover:Point|null):void;hit(scene:RenderScene,drawing:Drawing,x:number,y:number):{id:string;handle?:number}|null;destroy():void}
export class CandleBuffer {constructor(limit?:number);readonly data:Candle[];readonly revision:number;remove(times:number[]):Candle[];merge(page:Candle[],anchor?:number|null):Candle[];view(rightTime:number|null,bars:number):{candles:Candle[];start:number;end:number};upperBound(time:number):number}
export class DrawingBook {constructor(drawings?:Drawing[]);items:Drawing[];past:Drawing[][];future:Drawing[][];add(drawing:Drawing):void;edit(id:string,patch:Partial<Drawing>):void;remove(id:string):void;undo():void;redo():void}
export const STUDIES:Study[];
export function calculate(candles:Candle[],study:string):StudyResult;
export function validateDrawing(drawing:unknown):Drawing;
export function validateLayout(layout:unknown):Layout;
export function validateSDKLayout(layout:unknown):SDKLayout;
export interface PublicSigningKey {algorithm:'Ed25519';key:string;kid:string}
export interface SDKEntitlement {project:string;origin:string;plan:string;maxCharts:number;features:readonly string[];exp:number}
export interface ChartSDK {readonly entitlement:SDKEntitlement;createChart(container:HTMLElement,options?:ChartOptions):ChartEngine;linkCharts(charts:ChartEngine[]):()=>void;saveLayout(charts:ChartEngine[],market:string,series?:PriceSeries):SDKLayout;openLayout(containers:HTMLElement[],layout:SDKLayout):ChartEngine[];refresh():Promise<SDKEntitlement>;dataToken():Promise<string>;destroy():void}
