// One chart contract, a store per network. Solana stays the default; Arbitrum One and Robinhood Chain
// rows carry an explicit network and an exact lowercase 0x token address. No ticker matching.
export const EVM_NETWORKS=['arbitrum','robinhood'];
export const NETWORK_NAMES={solana:'Solana',arbitrum:'Arbitrum One',robinhood:'Robinhood Chain'};
export const EXPLORERS={solana:'https://explorer.solana.com',arbitrum:'https://arbiscan.io',robinhood:'https://robinhoodchain.blockscout.com'};
export const NETWORK_LOGOS={solana:'/assets/ecosystem/solana.svg',arbitrum:'/assets/ecosystem/arbitrum.svg',robinhood:'/assets/ecosystem/robinhood-chain.jpg'};
export const evmAddress=/^0x[0-9a-f]{40}$/,evmPool=/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/,evmTx=/^0x[0-9a-f]{64}$/;
export const chainOf=t=>EVM_NETWORKS.includes(t?.network)?t.network:'solana';
export const networkParams=t=>chainOf(t)==='solana'?{}:{network:chainOf(t)};
export const networkName=t=>NETWORK_NAMES[chainOf(t)];
// type: address | tx | token. EVM pool ids (32-byte V4 keys) are not addresses and get no link.
export function explorerUrl(network,type,value){
 const base=EXPLORERS[network];if(!base||typeof value!=='string')return null;
 if(network==='solana')return /^[1-9A-HJ-NP-Za-km-z]{32,90}$/.test(value)?base+'/'+(type==='token'?'address':type)+'/'+value:null;
 if(type==='tx')return evmTx.test(value)?base+'/tx/'+value:null;
 return evmAddress.test(value)?base+'/'+(type==='token'?'token':'address')+'/'+value:null;
}
// One badge for every surface (lists, headers, search, assistant). Exchange instruments keep their venue label.
export function networkBadge(t,compact=false){
 if(t?.network==='exchange')return '';const n=chainOf(t),name=NETWORK_NAMES[n];injectNetworkStyles();
 return `<span class="network-badge network-${n}${compact?' network-compact':''}" title="${name}"><img src="${NETWORK_LOGOS[n]}" alt="" width="12" height="12" loading="lazy" decoding="async">${compact?'':`<span>${name}</span>`}</span>`;
}
let networkStyled=false;
export function injectNetworkStyles(){if(networkStyled||typeof document==='undefined'||document.getElementById('network-badge-styles'))return;networkStyled=true;const s=document.createElement('style');s.id='network-badge-styles';s.textContent=`
.network-badge{display:inline-flex;align-items:center;gap:4px;margin-left:6px;padding:1px 7px 1px 3px;border-radius:999px;font:500 11px/1.5 var(--font,"DM Sans"),sans-serif;vertical-align:1px;white-space:nowrap;background:color-mix(in srgb,var(--nb,#6b7088) 11%,transparent);color:color-mix(in srgb,var(--nb,#6b7088) 85%,currentColor);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--nb,#6b7088) 22%,transparent)}
.network-badge img{width:12px;height:12px;border-radius:50%;object-fit:cover}.network-compact{padding:1px;margin-left:5px}
.network-solana{--nb:#7a5af8}.network-arbitrum{--nb:#2d74da}.network-robinhood{--nb:#3f8f2a}
.market-network-chips{display:flex;gap:6px;flex-wrap:wrap}.market-network-chips button{font:500 12px/1 var(--font,"DM Sans"),sans-serif;padding:8px 11px;border-radius:999px;border:1px solid var(--line,#e7e5f3);background:var(--paper,#fff);color:inherit;cursor:pointer;min-height:32px}
.market-network-chips button[aria-pressed=true]{background:var(--accent,#5658e8);border-color:var(--accent,#5658e8);color:#fff}.market-network-chips button:focus-visible{outline:2px solid var(--accent,#5658e8);outline-offset:2px}
.home-network-chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}.home-network-chips a{display:inline-flex;align-items:center;gap:6px;padding:8px 13px;border-radius:999px;border:1px solid var(--line,#e7e5f3);background:var(--paper,#fff);color:inherit;text-decoration:none;font:500 13px/1.2 var(--font,"DM Sans"),sans-serif}
.home-network-chips a:hover{border-color:var(--accent,#5658e8)}.home-network-chips a b{font-weight:600}.home-network-chips img{width:16px;height:16px;border-radius:50%}
@media (min-width:900px){.market-exploration .public-token-table .public-token-name b,.public-token-table .public-token-name b{white-space:normal;overflow:visible;text-overflow:clip;max-width:none;overflow-wrap:anywhere;line-height:1.3}}
`;document.head.append(s);}
