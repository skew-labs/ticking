package rpcsrc

import (
	"sync"
	"time"
)

// limiter paces requests to one endpoint. It starts unlimited (or at a
// user-given rate) and adapts AIMD-style: the first rate-limit response of an
// episode cuts the rate to 70% of what was actually sent over the last
// seconds; every 2 s without rate limiting it grows by 15% + 0.5/s. Responses
// that were already in flight when the cut happened do not cut again, so N
// concurrent 429s count as one episode.
type limiter struct {
	mu      sync.Mutex
	rate    float64 // requests/s; 0 = unlimited
	max     float64 // upper bound (0 = none)
	next    time.Time
	lastCut time.Time
	lastUp  time.Time
	sec     [16]int64 // unix second of bucket
	cnt     [16]int
	hits    []time.Time // recent rate-limit episodes
}

func newLimiter(rps float64) *limiter {
	return &limiter{rate: rps, max: rps}
}

// reserve returns how long to wait before sending the next request.
func (l *limiter) reserve(now time.Time) time.Duration {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := now.Unix()
	i := s % int64(len(l.sec))
	if l.sec[i] != s {
		l.sec[i], l.cnt[i] = s, 0
	}
	l.cnt[i]++
	if l.rate <= 0 {
		return 0
	}
	slot := l.next
	if slot.Before(now) {
		slot = now
	}
	l.next = slot.Add(time.Duration(float64(time.Second) / l.rate))
	return slot.Sub(now)
}

func (l *limiter) observed(now time.Time, secs int64) float64 {
	s := now.Unix()
	n := 0
	for k := int64(1); k <= secs; k++ {
		i := (s - k) % int64(len(l.sec))
		if l.sec[i] == s-k {
			n += l.cnt[i]
		}
	}
	return float64(n) / float64(secs)
}

// cut handles a rate-limit response; it reports whether a new episode
// started (the caller then pauses the endpoint briefly). The rate itself is
// only cut when episodes repeat (>= 2 within 5 s), so a sporadic 429 does not
// collapse the throughput.
func (l *limiter) cut(now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if now.Sub(l.lastCut) < 1500*time.Millisecond {
		return false
	}
	l.lastCut = now
	keep := l.hits[:0]
	for _, h := range l.hits {
		if now.Sub(h) < 5*time.Second {
			keep = append(keep, h)
		}
	}
	l.hits = append(keep, now)
	if len(l.hits) < 2 {
		return true
	}
	obs := l.observed(now, 3)
	r := l.rate
	if r <= 0 || (obs > 0 && obs < r) {
		r = obs
	}
	r *= 0.7
	if r < 0.5 {
		r = 0.5
	}
	l.rate = r
	l.next = now
	return true
}

// ok handles a successful response.
func (l *limiter) ok(now time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.rate <= 0 || now.Sub(l.lastCut) < 3*time.Second || now.Sub(l.lastUp) < 2*time.Second {
		return
	}
	l.rate = l.rate*1.15 + 0.5
	if l.max > 0 && l.rate > l.max {
		l.rate = l.max
	}
	l.lastUp = now
}

func (l *limiter) current() float64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.rate
}
