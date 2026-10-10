"""Bounded display projection. Never persists invented executions or volume."""
from copy import deepcopy


def continuous_window(bars, interval, limit):
    """Carry inside the latest observed window, never beyond its last record.

    A cursor is still anchored to real history: calling this on an older page
    skips an empty epoch, rather than issuing thousands of synthetic pages.
    """
    if not bars:
        return []
    real = [b for b in bars if not b.get('synthetic') or b.get('estimated')]
    if not real:
        return bars[-limit:]
    end = real[-1]['time']
    lower = max(real[0]['time'], end - (limit - 1) * interval)
    by_time = {b['time']: b for b in real}
    previous = next((b for b in reversed(real) if b['time'] <= lower), None)
    result = []
    for stamp in range(lower, end + interval, interval):
        if stamp in by_time:
            previous = by_time[stamp]
            result.append(previous)
        elif previous:
            bar = deepcopy(previous)
            bar.update(time=stamp, open=previous['close'], high=previous['close'],
                       low=previous['close'], close=previous['close'], volume=0,
                       baseVolume=0, count=0, samples=0, referenceSamples=0, synthetic=True,
                       sourceTime=previous['time'], gapKind='unverified_no_record',
                       estimated=False, referenceOnly=True)
            # A carry cites its anchor, not a new provider receipt or estimated
            # path at the missing timestamp. Never inherit its anchor's volume,
            # execution provenance or per-minute currency conversion claim.
            for key in ('receipt','conversionReceipt','sourceBaseVolume','sourceQuoteVolume',
                        'sourceReceivedAt','providerReportedVolume','conversionPrice',
                        'conversionTime','conversionSource','conversionMarket',
                        'originalQuoteMint','priceConversion','conversionEstimated',
                        'anchors','estimateMethod','closedProviderCandle','sourcePeriod',
                        'sourceSelection','sourceResolutionSeconds','coverageResolution'):
                bar.pop(key, None)
            if 'quality' in bar:
                bar['quality'] = dict(basis='carried', observedMinutes=0,
                    referenceMinutes=0, estimatedMinutes=0, carriedMinutes=interval//60)
            if 'quoteVolume' in bar:
                bar['quoteVolume'] = 0
            result.append(bar)
    return result
