"""Deterministic fair pool admission within the existing 10,000-pool budget."""
import json
MAX_SELECTED_POOLS=10000
MAX_POOLS_PER_MINT=50

def verified_market_pools(connection,mint,priority=()):
    preferred=[]
    for pool in dict.fromkeys(priority[:10]):
        row=connection.execute('SELECT pool,config FROM pools WHERE pool=? AND (base=? OR quote=?)',(pool,mint,mint)).fetchone()
        if row:preferred.append((row[0],json.loads(row[1])))
    seen={pool for pool,_ in preferred}
    regular=[(pool,json.loads(encoded)) for pool,encoded in connection.execute('SELECT pool,config FROM pools WHERE base=? OR quote=? ORDER BY (SELECT count(*) FROM bars WHERE bars.pool=pools.pool AND interval=60) DESC,pool LIMIT ?',(mint,mint,MAX_POOLS_PER_MINT)) if pool not in seen]
    return (preferred+regular)[:MAX_POOLS_PER_MINT]

def select_pools(candidates,budget=MAX_SELECTED_POOLS,priority_indices=(),managed_indices=()):
    if not 1<=budget<=MAX_SELECTED_POOLS:raise ValueError('Invalid pool budget')
    priority_indices=tuple(priority_indices)
    if len(priority_indices)>100 or len(set(priority_indices))!=len(priority_indices) or any(type(i)!=int or not 0<=i<len(candidates) for i in priority_indices):
        raise ValueError('Invalid protected market set')
    managed_indices=tuple(managed_indices)
    if len(managed_indices)>80 or len(set(managed_indices))!=len(managed_indices) or any(type(i)!=int or not 0<=i<len(candidates) for i in managed_indices):raise ValueError('Invalid managed market set')
    selected={}
    # Keep the original 100 markets' verified pools when discovery widens.
    # At most 100 * 50 slots are reserved; all remaining admission stays fair.
    for i in priority_indices:
        for pool,config in candidates[i][:MAX_POOLS_PER_MINT]:
            if pool not in selected:
                if len(selected)==budget:return selected
                selected[pool]=config
    # Preserve newly admitted customer curves and migration pools before the
    # broad catalog consumes the remaining capacity. No extra stream is opened.
    for i in managed_indices:
        for pool,config in candidates[i][:10]:
            if pool not in selected:
                if len(selected)==budget:return selected
                selected[pool]=config
    for rank in range(MAX_POOLS_PER_MINT):
        for rows in candidates:
            if rank>=len(rows):continue
            pool,config=rows[rank]
            if pool not in selected:
                if len(selected)==budget:return selected
                selected[pool]=config
    return selected
