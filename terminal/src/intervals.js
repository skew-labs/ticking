export const MARKET_INTERVALS=['1m','3m','5m','15m','30m','45m','1h','2h','3h','4h','6h','8h','12h','1d','1w'];
export const INTERVAL_PRESETS=['1s','3s','5s','15s','30s','45s',...MARKET_INTERVALS];
const UNITS={s:1,m:60,h:3600,d:86400,w:604800};
export function intervalStart(time,period){const origin=period===604800000?-259200000:0;return Math.floor((time-origin)/period)*period+origin;}
export function parseInterval(input){
  const match=String(input).trim().toLowerCase().match(/^([1-9]\d*)(s|m|h|d|w)$/);
  if(!match)throw new RangeError('Use a whole number and s, m, h, d or w, such as 3m or 1w.');
  const seconds=Number(match[1])*UNITS[match[2]];
  if(!Number.isSafeInteger(seconds)||seconds>604800)throw new RangeError('Choose an interval between 1 second and 1 week.');
  const unit=seconds===604800?'w':seconds%86400===0?'d':seconds%3600===0?'h':seconds%60===0?'m':'s';
  return {seconds,milliseconds:seconds*1000,label:`${seconds/UNITS[unit]}${unit}`};
}
export function aggregateCandles(source,period){
  const out=[];
  for(const c of source){
    const time=intervalStart(c.time,period),last=out.at(-1);
    if(!last||last.time!==time)out.push({...c,time});
    else{last.close=c.close;last.high=Math.max(last.high,c.high);last.low=Math.min(last.low,c.low);last.volume+=c.volume;if(Number.isFinite(c.baseVolume))last.baseVolume=(last.baseVolume||0)+c.baseVolume;if(Number.isFinite(c.count))last.count=(last.count||0)+c.count;last.synthetic=!!last.synthetic&&!!c.synthetic;}
  }
  return out;
}
