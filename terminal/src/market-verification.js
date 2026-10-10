export const reviewedMarket=t=>t?.verification?.status==='reviewed'&&t.verification.id===t.id&&t.verification.mint===t.mint;
export const rankingEligible=t=>reviewedMarket(t)&&t.ranking_eligible===true&&!t.stale&&typeof t.price==='number'&&Number.isFinite(t.price)&&t.price>0&&typeof t.change==='number'&&Number.isFinite(t.change)&&t.change>=-100;
export function verificationBadge(t){
 if(!reviewedMarket(t))return '';
 return '<span class="token-reviewed" role="img" aria-label="Reviewed by ticking" title="Reviewed by ticking · Token identity and listing sources"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="m4.6 8 2.2 2.2 4.6-4.6" fill="none" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>';
}
