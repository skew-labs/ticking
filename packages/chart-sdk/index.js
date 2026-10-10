export {ChartEngine} from './engine.js';
export {Canvas2DRenderer} from './renderer.js';
export {STUDIES,calculate} from './indicators.js';
export {CandleBuffer,DrawingBook,validateLayout,validateSDKLayout,validateDrawing} from './model.js';
export class StudyWorker {
  constructor(){this.worker=new Worker(new URL('./indicator-worker.js',import.meta.url),{type:'module'});this.jobs=new Map();this.id=0;this.worker.onmessage=({data})=>{const task=this.jobs.get(data.id);if(!task)return;this.jobs.delete(data.id);data.error?task.reject(Error(data.error)):task.resolve(data.results)};this.worker.onerror=()=>{for(const job of this.jobs.values())job.reject(Error('Indicator worker unavailable'));this.jobs.clear()}}
  request(candles,studies){return new Promise((resolve,reject)=>{const id=++this.id;this.jobs.set(id,{resolve,reject});this.worker.postMessage({id,candles,studies})})}
  destroy(){this.worker.terminate();for(const job of this.jobs.values())job.reject(Error('Chart closed'));this.jobs.clear()}
}

export {LIGHT_THEME,DARK_THEME,validateTheme} from './theme.js';
