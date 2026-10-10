import copy,math,pytest
from candle_quality import assess_candles,attach_quality,CandleIntegrityError,recovery_assets,recovery_lane,update_incident

def bar(t=60):return dict(time=t,open=10,high=11,low=9,close=10,volume=2,count=3,sources=['solana_reference'])

@pytest.mark.parametrize('change',[{'high':9},{'low':11},{'close':float('nan')},{'open':float('inf')},{'volume':-1},{'count':-1},{'time':61},{'time':True},{'volume':'0'},{'referenceOnly':True},{'synthetic':True}])
def test_rejects_invalid_output_without_mutating_original(change):
 b=bar();b.update(change);before=repr(b)
 with pytest.raises(CandleIntegrityError):attach_quality({'candles':[b]},60,120)
 assert repr(b)==before

def test_duplicate_reversal_future_and_week_alignment():
 assert assess_candles([bar(),bar()],60)['invalidBars']==1
 assert assess_candles([bar(120),bar(60)],60)['invalidBars']==1
 assert assess_candles([bar(180)],60,120)['invalidBars']==1
 assert assess_candles([bar(1728259200)],604800,1728864000)['integrityPassed']

def test_hyperliquid_week_uses_source_calendar_only_for_exact_identity():
 native={'identity':{'network':'exchange','venue':'hyperliquid_public','historyPolicy':'hyperliquid_retained_v1'},'candles':[bar(1791417600)]}
 assert attach_quality(native,604800,1791663900)['coverage']['chartQuality']['integrityPassed']
 for identity in ({},{'network':'solana','venue':'hyperliquid_public','historyPolicy':'hyperliquid_retained_v1'},{'network':'exchange','venue':'other','historyPolicy':'hyperliquid_retained_v1'}):
  with pytest.raises(CandleIntegrityError):attach_quality({**native,'identity':identity},604800,1791663900)

def test_flat_carries_counted_as_unresolved_coverage_not_recovered_prices():
 rows=[dict(bar(60*i),open=10,high=10,low=10,volume=0,count=0,synthetic=True) for i in range(40)]
 before=copy.deepcopy(rows);q=attach_quality({'candles':rows},60)['coverage']['chartQuality']
 assert q['invalidBars']==0 and q['flatBars']==q['carriedBars']==40
 assert q['state']=='needs_review' and 'carried_history' in q['flags'] and rows==before

def test_holes_and_references_are_distinct_and_wicks_not_clipped():
 a=bar();a.update(volume=0,count=0,referenceOnly=True)
 b=bar(180);b.update(high=50,low=1)
 rows=[a,b]+[bar(240+60*i) for i in range(35)];before=copy.deepcopy(rows)
 q=assess_candles(rows,60)
 assert q['timestampHoles']==1 and q['referenceBars']==1 and q['wickCandidates']==1 and rows==before
 assert q['sourceCompleteness']=='not_proven'

def test_empty_page_not_invented_and_mixed_aggregate_volume_valid():
 assert attach_quality({'candles':[]},60)['coverage']['chartQuality']['state']=='unavailable'
 assert assess_candles([dict(bar(300),estimated=True,referenceOnly=False)],300)['integrityPassed']

def test_new_asset_gets_only_exact_quote_routes_preserving_primary_and_old_targets():
 catalog=[{'id':'NEW','mint':'N'}];routes=[dict(mint='N',quoteMint='Q',pools=[dict(mint='N',quoteMint='Q',pool='P'),dict(mint='WRONG',quoteMint='Q',pool='X'),dict(mint='N',quoteMint='Z',pool='Y')])]
 old=[dict(id='OLD',mint='O',quote='Q',pools=['OP'])]
 got=recovery_assets({'N'},catalog,routes,{'N':('Q','PIN')},old)
 assert got[0]==old[0] and got[1]['pools']==['PIN','P'] and got[1]['mint']=='N'
 assert recovery_assets({'OTHER'},catalog,routes,{},old)==old
 assert recovery_assets({'N':3},catalog,routes,{},old)[1]['repairPriority']==3

def test_cross_venue_median_is_not_missing_history_reconstruction():
 rows=[dict(bar(i*60),volume=0,count=0,referenceOnly=True,estimated=True,estimateMethod='composite_reference') for i in range(40)]
 q=assess_candles(rows,60)
 assert q['estimatedBars']==40 and q['reconstructedBars']==0 and not q['flags']

def test_history_cannot_starve_when_full_asset_round_exceeds_refresh_ttl():
 prior={'latestAt':100,'cursor':60,'lastLane':'latest'}
 assert recovery_lane(prior,10000,0)=='history'
 prior['lastLane']='history';assert recovery_lane(prior,10000,0)=='latest'
 prior['historyBoundary']=True;assert recovery_lane(prior,10000,0)=='latest'
 assert recovery_lane(prior,150,0) is None

def test_current_clear_does_not_resolve_historical_incident():
 old=dict(before=1000,openedAt=1100,state='unresolved')
 healthy=dict(before=2000,quality={'flags':[]})
 got,receipt=update_incident(old,healthy,None,2001)
 assert got==old and receipt is None
 got,receipt=update_incident(old,healthy,dict(before=999,quality={'flags':[]}),2001)
 assert got['state']=='unresolved' and receipt is None

def test_historical_recovery_keeps_new_fault_pending_and_records_resolution():
 old=dict(before=1000,openedAt=1100,state='unresolved')
 got,receipt=update_incident(old,dict(before=2000,quality={'flags':['carried_history']}),dict(before=1000,quality={'flags':[]}),2001)
 assert receipt['before']==1000 and receipt['state']=='resolved'
 assert got['before']==2000 and got['state']=='unresolved'
 got,receipt=update_incident(old,dict(before=2000,quality={'flags':[]}),dict(before=1000,error='provider'),2001)
 assert got['state']=='unresolved' and receipt is None
