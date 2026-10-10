const TOKENS=[]; // Metadata comes from the configured data service, never demo prices.
import {bindDataFeed} from './data-feed.js';
import {TickChart} from '../src/chart.js';
import {parseInterval,MARKET_INTERVALS} from '../src/intervals.js';
import {mountChartControls} from '../src/chart-controls.js';
const controlsCSS=new URL('../chart-tools.css',import.meta.url).href;
/** Real executed-history embed. Supply tokenProvider for private customer access. */
class TickChartElement extends HTMLElement {
 static observedAttributes=['market','mint','pool','aggregation','history','interval','theme','average','tools'];
 constructor(){super();this.attachShadow({mode:'open'});}
 connectedCallback(){this.render();}
 disconnectedCallback(){this.feedDispose?.();this.dispose?.();this.chart?.destroy();}
 attributeChangedCallback(name,oldValue,newValue){
  if(!this.isConnected||oldValue===newValue)return;
  if(this.chart&&name==='interval'){try{const interval=parseInterval(newValue||'1m').label;if(this.getAttribute('aggregation')==='token'&&(parseInterval(interval).seconds<60||parseInterval(interval).seconds%60))throw Error('Token references start at 1m.');this.chart.set({interval});this.shadowRoot.querySelector('.frame').dispatchEvent(new CustomEvent('intervalchange',{detail:interval}));}catch(e){this.shadowRoot.querySelector('header span').textContent=e.message;}return;}
  if(this.chart&&name==='average'){this.chart.set({average:this.hasAttribute('average')});return;}
  if(this.chart&&name==='theme'){const dark=newValue==='dark',frame=this.shadowRoot.querySelector('.frame');this.chart.set({theme:dark?'dark':'light'});frame.classList.toggle('dark',dark);frame.classList.toggle('light',!dark);frame.style.backgroundColor=dark?'#1a1e2d':'white';frame.style.color=dark?'#d9ddea':'#333c50';frame.style.borderColor=dark?'#333a4e':'#e4e7ef';return;}
  this.render();
 }
 set tokenProvider(value){this._tokenProvider=value;if(this.isConnected)this.render();}
 exportDrawings(){return JSON.parse(JSON.stringify(this.chart?.drawings||[]));}
 importDrawings(drawings){if(!this.chart?.drawingLayer)throw new Error('Enable the tools attribute first.');this.chart.drawingLayer.import(drawings);}
 render(){
  this.feedDispose?.();this.dispose?.();this.chart?.destroy();this.chart=null;this.dispose=null;
  const market=this.getAttribute('mint')||this.getAttribute('market')||'SOL',token=TOKENS.find(t=>t.market===market||t.symbol===market),editable=this.hasAttribute('tools');
  const theme=this.getAttribute('theme')==='dark'?'dark':'light',aggregation=this.getAttribute('aggregation')||'pool';let interval,error;
  try{interval=parseInterval(this.getAttribute('interval')||'1m').label;if(!['pool','token','exchange'].includes(aggregation)||aggregation!=='pool'&&(this.hasAttribute('pool')||(parseInterval(interval).seconds<60||parseInterval(interval).seconds%60)))throw Error('Token references require mint or market selection and 1m–1w.');}catch(e){error=e.message;}
  this.shadowRoot.innerHTML=`<link rel="stylesheet" href="${controlsCSS}"><style>:host{display:block;height:360px;min-height:180px;font-family:system-ui,sans-serif}:host([tools]){height:auto}.frame{height:${editable?'auto':'100%'};box-sizing:border-box;border:1px solid ${theme==='dark'?'#333a4e':'#e4e7ef'};border-radius:6px;overflow:hidden;background:${theme==='dark'?'#1a1e2d':'white'};color:${theme==='dark'?'#d9ddea':'#333c50'}}header{height:45px;box-sizing:border-box;padding:14px 16px;font-size:12px;display:flex;justify-content:space-between}header span{font-size:10px;color:#8d95a6}.sdk-chart{height:${editable?'440px':'calc(100% - 45px)'};position:relative}.chart-tooltip{position:absolute;background:#252b40;color:white;padding:6px 9px;font-size:10px;border-radius:4px;pointer-events:none}.error{padding:22px;font-size:13px}button{font-family:inherit}*{box-sizing:border-box}</style><div class="frame ${theme}"><header><b></b><span>Recorded executions · ticking</span></header><div class="sdk-chart"></div></div>`;
  this.shadowRoot.querySelector('b').textContent=token?token.symbol:market.slice(0,12);
  if(error){const area=this.shadowRoot.querySelector('.sdk-chart');area.className='error';area.textContent=error;return;}
  this.chart=new TickChart(this.shadowRoot.querySelector('.sdk-chart'),{symbol:token?.symbol||market.slice(0,12),market:this.getAttribute('pool')||market},{theme,interval,editable,storageScope:'sdk',series:[],emptyMessage:'Loading recorded executions…',average:this.hasAttribute('average')});
  this.feedDispose=bindDataFeed(this.chart,{market:this.getAttribute('market')||'SOL',mint:this.getAttribute('mint')||undefined,pool:this.getAttribute('pool')||undefined,aggregation,history:this.getAttribute('history')||'full',tokenProvider:this._tokenProvider,apiBase:this.getAttribute('api-base')||(this._tokenProvider?'https://api.ticking.dev':'https://ticking.dev'),onStatus:data=>{const status=this.shadowRoot.querySelector('header span'),quote=data.candles?.at(-1)?.quoteCurrency||data.quote,unit=quote?.length>12?quote.slice(0,6)+'…':quote;status.textContent=data.loading?`Loading · ${data.displayedInterval||'recorded history'} still displayed`:data.error?(data.retained?`Retained ${data.displayedInterval} · retrying`:data.error):`${data.interval} · ${unit} · ${data.loadingHistory?'Loading history':data.identity?.network==='exchange'?data.venue:data.history?(data.history.sourceLabel||'Binance')+' history + Solana':data.stale?'Older records':data.aggregation==='token'?'Token reference · provisional':data.observationAvailable?'Observer executions · provisional':'Recorded executions'}${data.carriedBars?' · carried closes':''} · ticking`;if(quote)this.shadowRoot.querySelector('b').textContent=`${data.symbol||token?.symbol||market.slice(0,12)} / ${unit}`;this.dispatchEvent(new CustomEvent('tick-data',{detail:data}));}});
  if(editable){const root=this.shadowRoot.querySelector('.frame');this.dispose=mountChartControls(this.chart,root,aggregation!=='pool'?{intervals:MARKET_INTERVALS,customIntervals:false}:{});root.addEventListener('intervalchange',e=>{this.dispatchEvent(new CustomEvent('tick-interval-change',{detail:e.detail}));});}
 }
}
if(!customElements.get('tick-chart'))customElements.define('tick-chart',TickChartElement);
