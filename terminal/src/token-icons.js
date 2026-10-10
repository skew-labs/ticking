// Same-origin normalized files only. Artwork never establishes token identity.
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const staticIcons=new Set(typeof __TICKING_STATIC_ICONS__==='undefined'?[]:__TICKING_STATIC_ICONS__);
const liveIcon=/^\/api\/public\/token-icons\/(?:[1-9A-HJ-NP-Za-km-z]{32,44}|exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30})-[a-f0-9]{16}\.webp$/;
export function tokenImage(t,cls='public-token-icon',size=36){
 const valid=/^\/assets\/tokens\/catalog\/[a-z0-9-]+\.webp$/.test(t.icon||'')||liveIcon.test(t.icon||'');
 const initials=esc((t.symbol||'?').slice(0,2));
 const file=String(t.icon||'').split('/').at(-1),src=valid&&staticIcons.has(file)?'/assets/tokens/live/'+file:t.icon;
 return `<span class="${cls} token-artwork" aria-hidden="true">${valid?`<img data-token-artwork data-token-initials="${initials}" data-token-source="${esc(t.icon)}" src="${esc(src)}" width="${size}" height="${size}" alt="" loading="lazy" decoding="async">`:`<span>${initials}</span>`}</span>`;
}
document.addEventListener('error',event=>{
 const image=event.target;if(!image.matches?.('img[data-token-artwork]'))return;
 const source=image.dataset.tokenSource;
 if(liveIcon.test(source||'')&&image.getAttribute('src')!==source){image.src=source;return;}
 const fallback=document.createElement('span');fallback.textContent=image.dataset.tokenInitials||'?';
 image.replaceWith(fallback);
},true);
