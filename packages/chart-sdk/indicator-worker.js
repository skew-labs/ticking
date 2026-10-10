import {calculate} from './indicators.js';
// Exact inputs/config form the key. Same panes share work; LRU bounds reuse.
const cache=new Map();
self.onmessage=({data})=>{const {id,candles,studies}=data;try{const key=JSON.stringify([candles,studies]);let results=cache.get(key);if(results){cache.delete(key);cache.set(key,results)}else{results=studies.map(s=>calculate(candles,s));cache.set(key,results);while(cache.size>12)cache.delete(cache.keys().next().value)}self.postMessage({id,results})}catch(error){self.postMessage({id,error:error.message})}};
