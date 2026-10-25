package rpcsrc

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/core/types"

	"github.com/em-mapping/extractor/internal/chaindb"
)

// EndpointSpec configures one RPC endpoint. On the command line:
// URL[#batch=B&workers=W] (the fragment is never sent).
type EndpointSpec struct {
	URL     string
	Batch   int     // blocks per JSON-RPC batch request (2 calls each); 0 = no batching
	Workers int     // concurrent HTTP requests to this endpoint
	RPS     float64 // initial and maximum requests/s (0 = unlimited, adaptive after the first 429)
}

// ParseEndpoint parses URL[#batch=B&workers=W] with defaults.
func ParseEndpoint(s string, defBatch, defWorkers int) (EndpointSpec, error) {
	e := EndpointSpec{Batch: defBatch, Workers: defWorkers}
	raw, frag, _ := strings.Cut(strings.TrimSpace(s), "#")
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return e, fmt.Errorf("bad rpc url %q", s)
	}
	e.URL = raw
	if frag != "" {
		q, err := url.ParseQuery(frag)
		if err != nil {
			return e, fmt.Errorf("bad endpoint options %q: %v", frag, err)
		}
		for k, v := range q {
			if k == "rps" {
				f, err := strconv.ParseFloat(v[0], 64)
				if err != nil || f < 0 {
					return e, fmt.Errorf("bad endpoint option %s=%s", k, v[0])
				}
				e.RPS = f
				continue
			}
			x, err := strconv.Atoi(v[0])
			if err != nil || x < 0 {
				return e, fmt.Errorf("bad endpoint option %s=%s", k, v[0])
			}
			switch k {
			case "batch":
				e.Batch = x
			case "workers":
				e.Workers = x
			default:
				return e, fmt.Errorf("unknown endpoint option %q (batch, workers, rps)", k)
			}
		}
	}
	if e.Workers < 1 {
		e.Workers = 1
	}
	return e, nil
}

// Redact returns scheme://host (API keys in paths/queries are never logged).
func Redact(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "?"
	}
	return u.Scheme + "://" + u.Host
}

// PoolOptions configure a Pool.
type PoolOptions struct {
	ChainID     uint64
	Retries     int           // attempts per block for real errors (rate limiting is counted separately)
	RateRetries int           // attempts per block that were rate limited
	HTTPTimeout time.Duration // per HTTP request
	MaxBackoff  time.Duration
	MaxCalls    uint64 // per endpoint; 0 = unlimited (an endpoint stops taking work when reached)
}

// EndpointStats are cumulative counters of one endpoint.
type EndpointStats struct {
	Host        string  `json:"host"`
	Batch       int     `json:"batch"`
	Workers     int     `json:"workers"`
	RPS         float64 `json:"final_rps"`
	Requests    uint64  `json:"http_requests"`
	Calls       uint64  `json:"rpc_calls"`
	Blocks      uint64  `json:"blocks_ok"`
	RateLimited uint64  `json:"rate_limited"`
	Errors      uint64  `json:"errors"`
	Bytes       uint64  `json:"bytes"`
	MeanMs      float64 `json:"mean_ms"`
	P50Ms       float64 `json:"p50_ms"`
	P90Ms       float64 `json:"p90_ms"`
	MaxMs       float64 `json:"max_ms"`
	LastError   string  `json:"last_error,omitempty"`
	LastRate    string  `json:"last_rate_limit,omitempty"`
}

type endpoint struct {
	spec   EndpointSpec
	host   string
	client *http.Client
	batch  atomic.Int64 // current batch size (may be reduced on "batch too large" errors)
	pause  atomic.Int64 // unix nanos until which no request is sent (429 / Retry-After)
	lim    *limiter

	requests, calls, blocks, rateLimited, errs, bytes atomic.Uint64
	mu                                                sync.Mutex
	lat                                               []float64 // ms, reservoir
	latN                                              uint64
	latSum, latMax                                    float64
	lastErr, lastRate                                 string
}

func (e *endpoint) observe(ms float64) {
	e.mu.Lock()
	e.latN++
	e.latSum += ms
	if ms > e.latMax {
		e.latMax = ms
	}
	if len(e.lat) < 4096 {
		e.lat = append(e.lat, ms)
	} else if j := rand.Uint64N(e.latN); j < 4096 {
		e.lat[j] = ms
	}
	e.mu.Unlock()
}

func (e *endpoint) setErr(s string) {
	e.errs.Add(1)
	e.mu.Lock()
	if len(s) > 300 {
		s = s[:300]
	}
	e.lastErr = s
	e.mu.Unlock()
}

func (e *endpoint) setRate(s string) {
	e.rateLimited.Add(1)
	e.mu.Lock()
	if len(s) > 300 {
		s = s[:300]
	}
	e.lastRate = s
	e.mu.Unlock()
}

func (e *endpoint) stats() EndpointStats {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := EndpointStats{Host: e.host, Batch: int(e.batch.Load()), Workers: e.spec.Workers, RPS: e.lim.current(), Requests: e.requests.Load(), Calls: e.calls.Load(), Blocks: e.blocks.Load(),
		RateLimited: e.rateLimited.Load(), Errors: e.errs.Load(), Bytes: e.bytes.Load(), MaxMs: e.latMax, LastError: e.lastErr, LastRate: e.lastRate}
	if e.latN > 0 {
		s.MeanMs = e.latSum / float64(e.latN)
		l := append([]float64(nil), e.lat...)
		sort.Float64s(l)
		s.P50Ms = l[len(l)*50/100]
		s.P90Ms = l[len(l)*90/100]
	}
	return s
}

// task is one block to fetch.
type task struct {
	n        uint64
	req      *rangeReq
	slot     int
	attempts int // real errors
	rate     int // rate-limited attempts
}

type rangeReq struct {
	out   []chaindb.RawBlock
	bytes atomic.Uint64
	left  atomic.Int64
	done  chan struct{}
	once  sync.Once
	mu    sync.Mutex
	err   error
}

func (r *rangeReq) fail(err error) {
	r.mu.Lock()
	if r.err == nil {
		r.err = err
	}
	r.mu.Unlock()
	r.once.Do(func() { close(r.done) })
}

func (r *rangeReq) failed() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.err != nil
}

func (r *rangeReq) complete() {
	if r.left.Add(-1) == 0 {
		r.once.Do(func() { close(r.done) })
	}
}

// queue is an unbounded FIFO with priority re-insertion for retries.
type queue struct {
	mu     sync.Mutex
	cond   *sync.Cond
	items  []*task
	closed bool
}

func newQueue() *queue {
	q := &queue{}
	q.cond = sync.NewCond(&q.mu)
	return q
}

func (q *queue) push(front bool, ts ...*task) {
	q.mu.Lock()
	if front {
		q.items = append(append(make([]*task, 0, len(ts)+len(q.items)), ts...), q.items...)
	} else {
		q.items = append(q.items, ts...)
	}
	q.mu.Unlock()
	q.cond.Broadcast()
}

// pop blocks until at least one task is available; returns up to max tasks.
func (q *queue) pop(max int) []*task {
	q.mu.Lock()
	defer q.mu.Unlock()
	for len(q.items) == 0 && !q.closed {
		q.cond.Wait()
	}
	if q.closed {
		return nil
	}
	if max > len(q.items) {
		max = len(q.items)
	}
	out := append([]*task(nil), q.items[:max]...)
	q.items = q.items[max:]
	return out
}

func (q *queue) close() {
	q.mu.Lock()
	q.closed = true
	q.mu.Unlock()
	q.cond.Broadcast()
}

// Pool fetches blocks from one or more endpoints. Every endpoint runs its own
// workers that pull blocks from a shared queue (so faster endpoints take more
// work); failed blocks are re-queued at the front and may be served by
// another endpoint. A block that keeps failing fails its whole range.
type Pool struct {
	o      PoolOptions
	eps    []*endpoint
	q      *queue
	signer types.Signer
	rr     atomic.Uint64
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
	active atomic.Int64
	reason atomic.Value // string: why all workers stopped
}

// NewPool starts the endpoint workers.
func NewPool(ctx context.Context, specs []EndpointSpec, o PoolOptions) (*Pool, error) {
	if len(specs) == 0 {
		return nil, errors.New("no rpc endpoints")
	}
	if o.Retries <= 0 {
		o.Retries = 8
	}
	if o.RateRetries <= 0 {
		o.RateRetries = 200
	}
	if o.HTTPTimeout <= 0 {
		o.HTTPTimeout = 60 * time.Second
	}
	if o.MaxBackoff <= 0 {
		o.MaxBackoff = 60 * time.Second
	}
	ctx, cancel := context.WithCancel(ctx)
	p := &Pool{o: o, q: newQueue(), signer: types.LatestSignerForChainID(newBig(o.ChainID)), ctx: ctx, cancel: cancel}
	for _, s := range specs {
		tr := &http.Transport{
			Proxy:               http.ProxyFromEnvironment,
			DialContext:         (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			ForceAttemptHTTP2:   true,
			MaxIdleConns:        s.Workers * 2,
			MaxIdleConnsPerHost: s.Workers * 2,
			IdleConnTimeout:     90 * time.Second,
			TLSHandshakeTimeout: 15 * time.Second,
		}
		e := &endpoint{spec: s, host: Redact(s.URL), client: &http.Client{Transport: tr, Timeout: o.HTTPTimeout}, lim: newLimiter(s.RPS)}
		e.batch.Store(int64(s.Batch))
		p.eps = append(p.eps, e)
		for w := 0; w < s.Workers; w++ {
			p.wg.Add(1)
			p.active.Add(1)
			go p.worker(e)
		}
	}
	go func() {
		<-ctx.Done()
		p.q.close()
	}()
	return p, nil
}

// Close stops all workers.
func (p *Pool) Close() {
	p.cancel()
	p.q.close()
	p.wg.Wait()
}

// Stats returns per-endpoint counters.
func (p *Pool) Stats() []EndpointStats {
	out := make([]EndpointStats, len(p.eps))
	for i, e := range p.eps {
		out[i] = e.stats()
	}
	return out
}

// Hosts returns the redacted endpoint hosts.
func (p *Pool) Hosts() []string {
	var h []string
	for _, e := range p.eps {
		h = append(h, e.host)
	}
	return h
}

// Fetch returns raw blocks [from, from+count) in order, or the first
// persistent error.
func (p *Pool) Fetch(ctx context.Context, from, count uint64) ([]chaindb.RawBlock, uint64, error) {
	if count == 0 {
		return nil, 0, nil
	}
	r := &rangeReq{out: make([]chaindb.RawBlock, count), done: make(chan struct{})}
	r.left.Store(int64(count))
	ts := make([]*task, count)
	for i := range ts {
		ts[i] = &task{n: from + uint64(i), req: r, slot: i}
	}
	p.q.push(false, ts...)
	select {
	case <-r.done:
	case <-ctx.Done():
		r.fail(ctx.Err())
	case <-p.ctx.Done():
		if why, ok := p.reason.Load().(string); ok {
			r.fail(errors.New(why))
		} else {
			r.fail(p.ctx.Err())
		}
	}
	r.mu.Lock()
	err := r.err
	r.mu.Unlock()
	if err != nil {
		return nil, 0, err
	}
	return r.out, r.bytes.Load(), nil
}

func (p *Pool) sleep(d time.Duration) bool {
	if d <= 0 {
		return true
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-p.ctx.Done():
		return false
	}
}

func (p *Pool) worker(e *endpoint) {
	defer p.wg.Done()
	defer func() {
		if p.active.Add(-1) == 0 && p.ctx.Err() == nil {
			p.reason.Store("all endpoints reached --max-calls")
			p.cancel()
		}
	}()
	for {
		if p.o.MaxCalls > 0 && e.calls.Load() >= p.o.MaxCalls {
			return
		}
		// Honour a pause (429 / Retry-After) before taking work, so that other
		// endpoints pick the blocks up meanwhile.
		if until := e.pause.Load(); until > 0 {
			if !p.sleep(time.Until(time.Unix(0, until))) {
				return
			}
		}
		// Pace before taking work so throttled endpoints do not sit on blocks.
		if w := e.lim.reserve(time.Now()); w > 0 && !p.sleep(w) {
			return
		}
		b := int(e.batch.Load())
		max := b
		if max < 1 {
			max = 1
		}
		ts := p.q.pop(max)
		if ts == nil {
			return
		}
		// Drop tasks whose range already failed / was cancelled.
		live := ts[:0]
		for _, t := range ts {
			if !t.req.failed() {
				live = append(live, t)
			}
		}
		if len(live) == 0 {
			continue
		}
		p.serve(e, live, b)
	}
}

// failure classes
type fetchErr struct {
	rate    bool          // rate limited: endpoint is paused, block retried
	after   time.Duration // server-requested delay
	batchTo int           // >=0: reduce endpoint batch size to this
	msg     string
}

func (f *fetchErr) Error() string { return f.msg }

type rpcMsg struct {
	ID     json.RawMessage `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int             `json:"code"`
		Message string          `json:"message"`
		Data    json.RawMessage `json:"data"`
	} `json:"error"`
}

type rpcReq struct {
	JSONRPC string `json:"jsonrpc"`
	ID      uint64 `json:"id"`
	Method  string `json:"method"`
	Params  []any  `json:"params"`
}

func isRateMsg(code int, msg string) bool {
	m := strings.ToLower(msg)
	return code == 429 || code == -32005 || code == -32029 || code == -32090 || strings.Contains(m, "rate limit") || strings.Contains(m, "too many") ||
		strings.Contains(m, "limit exceeded") || strings.Contains(m, "exceeded") && strings.Contains(m, "limit") || strings.Contains(m, "throttl") || strings.Contains(m, "capacity")
}

// isBatchMsg: errors that mean "send smaller batches" (batch-size limits,
// Nitro's -32003 "response too large").
func isBatchMsg(msg string) bool {
	m := strings.ToLower(msg)
	return strings.Contains(m, "batch") || strings.Contains(m, "too large") || strings.Contains(m, "response size")
}

// serve fetches one group of blocks from e and delivers / re-queues them.
func (p *Pool) serve(e *endpoint, ts []*task, batch int) {
	results := make([][2]json.RawMessage, len(ts))
	var ferr *fetchErr
	var nbytes int
	if batch >= 1 {
		reqs := make([]rpcReq, 0, 2*len(ts))
		for i, t := range ts {
			h := hexutil.EncodeUint64(t.n)
			reqs = append(reqs, rpcReq{"2.0", uint64(2 * i), "eth_getBlockByNumber", []any{h, true}}, rpcReq{"2.0", uint64(2*i + 1), "eth_getBlockReceipts", []any{h}})
		}
		msgs, n, err := p.post(e, reqs, true, true)
		nbytes = n
		if err != nil {
			ferr = err
		} else {
			for _, m := range msgs {
				id, ok := parseID(m.ID)
				if !ok || id >= uint64(2*len(ts)) {
					continue
				}
				if m.Error != nil {
					fe := &fetchErr{batchTo: -1, msg: fmt.Sprintf("rpc error %d: %s", m.Error.Code, m.Error.Message)}
					if isRateMsg(m.Error.Code, m.Error.Message) {
						fe.rate = true
					} else if isBatchMsg(m.Error.Message) {
						fe.batchTo = batch / 2
					}
					if ferr == nil || fe.rate || fe.batchTo >= 0 {
						ferr = fe
					}
					continue
				}
				results[id/2][id%2] = m.Result
			}
		}
	} else {
		// No batching: two plain requests per block.
		for i, t := range ts {
			h := hexutil.EncodeUint64(t.n)
			for k, rq := range []rpcReq{{"2.0", 1, "eth_getBlockByNumber", []any{h, true}}, {"2.0", 2, "eth_getBlockReceipts", []any{h}}} {
				msgs, n, err := p.post(e, []rpcReq{rq}, false, k == 0)
				nbytes += n
				if err != nil {
					ferr = err
					break
				}
				if len(msgs) == 1 && msgs[0].Error != nil {
					fe := &fetchErr{batchTo: -1, msg: fmt.Sprintf("rpc error %d: %s", msgs[0].Error.Code, msgs[0].Error.Message)}
					fe.rate = isRateMsg(msgs[0].Error.Code, msgs[0].Error.Message)
					ferr = fe
					break
				}
				if len(msgs) == 1 {
					results[i][k] = msgs[0].Result
				}
			}
			if ferr != nil && ferr.rate {
				break
			}
		}
	}
	if ferr != nil {
		if ferr.rate {
			e.setRate(ferr.msg)
			p.backoff(e, ferr.after)
		} else {
			e.setErr(ferr.msg)
		}
		if ferr.batchTo >= 0 && int64(ferr.batchTo) < e.batch.Load() {
			e.batch.Store(int64(ferr.batchTo))
		}
	} else {
		e.lim.ok(time.Now())
	}
	per := uint64(0)
	if len(ts) > 0 {
		per = uint64(nbytes / len(ts))
	}
	var retry []*task
	for i, t := range ts {
		res := results[i]
		if res[0] != nil && res[1] != nil {
			h := hexutil.EncodeUint64(t.n)
			raw, err := ConvertWith(p.signer, t.n, res[0], res[1], func(i int) ([]byte, error) {
				msgs, _, ferr := p.post(e, []rpcReq{{"2.0", 1, "eth_getRawTransactionByBlockNumberAndIndex", []any{h, hexutil.EncodeUint64(uint64(i))}}}, false, false)
				if ferr != nil {
					return nil, fmt.Errorf("%s", ferr.msg)
				}
				if len(msgs) != 1 || msgs[0].Error != nil {
					return nil, fmt.Errorf("eth_getRawTransactionByBlockNumberAndIndex: %v", msgs)
				}
				var rb hexutil.Bytes
				if err := json.Unmarshal(msgs[0].Result, &rb); err != nil {
					return nil, err
				}
				return rb, nil
			})
			if err == nil {
				t.req.out[t.slot] = *raw
				t.req.bytes.Add(per)
				e.blocks.Add(1)
				t.req.complete()
				continue
			}
			e.setErr(err.Error())
			var ce *ConvertError
			if errors.As(err, &ce) && ce.Reason == ReasonRPCNotFound {
				// The endpoint does not have the block (yet): soft retry.
				t.rate++
				p.pauseFor(e, 500*time.Millisecond)
				if t.rate >= p.o.RateRetries {
					t.req.fail(err)
					continue
				}
				retry = append(retry, t)
				continue
			}
			t.attempts++
			if t.attempts >= p.o.Retries {
				t.req.fail(err)
				continue
			}
			retry = append(retry, t)
			continue
		}
		// Not delivered by this endpoint.
		switch {
		case ferr != nil && ferr.rate:
			t.rate++
			if t.rate >= p.o.RateRetries {
				t.req.fail(fmt.Errorf("block %d: RPC_RATE_LIMITED after %d attempts (%s: %s)", t.n, t.rate, e.host, ferr.msg))
				continue
			}
		case ferr != nil && ferr.batchTo >= 0:
			// configuration problem, not the block's fault: retry for free
		default:
			t.attempts++
			msg := "missing result"
			if ferr != nil {
				msg = ferr.msg
			}
			if t.attempts >= p.o.Retries {
				t.req.fail(fmt.Errorf("block %d: RPC_ERROR after %d attempts (last %s: %s)", t.n, t.attempts, e.host, msg))
				continue
			}
		}
		retry = append(retry, t)
	}
	if len(retry) > 0 {
		// Back off a little on real errors so a broken endpoint does not spin.
		if ferr == nil || !ferr.rate {
			a := retry[0].attempts
			p.sleep(time.Duration(200*(1<<min(a, 6))) * time.Millisecond)
		}
		p.q.push(true, retry...)
	}
}

// pauseFor stops e from taking work for d (without touching its rate).
func (p *Pool) pauseFor(e *endpoint, d time.Duration) {
	until := time.Now().Add(d).UnixNano()
	for {
		cur := e.pause.Load()
		if cur >= until || e.pause.CompareAndSwap(cur, until) {
			return
		}
	}
}

// backoff handles a rate-limit signal. With Retry-After the endpoint is
// paused for exactly that long (the server said when to come back); without
// it the pacing rate is cut once per episode and the endpoint pauses 1 s.
func (p *Pool) backoff(e *endpoint, after time.Duration) {
	now := time.Now()
	d := after
	if d <= 0 {
		if !e.lim.cut(now) {
			return
		}
		d = time.Second
	}
	if d > p.o.MaxBackoff {
		d = p.o.MaxBackoff
	}
	d += time.Duration(rand.Int64N(int64(d/10) + 1))
	p.pauseFor(e, d)
}

func parseID(raw json.RawMessage) (uint64, bool) {
	s := strings.Trim(string(raw), `"`)
	v, err := strconv.ParseUint(s, 10, 64)
	return v, err == nil
}

func retryAfter(h string) time.Duration {
	if h == "" {
		return 0
	}
	if s, err := strconv.ParseFloat(strings.TrimSpace(h), 64); err == nil && s >= 0 {
		return time.Duration(s * float64(time.Second))
	}
	if t, err := http.ParseTime(h); err == nil {
		return time.Until(t)
	}
	return 0
}

// post sends a request (array when batch) and decodes the response messages.
func (p *Pool) post(e *endpoint, reqs []rpcReq, batch, paced bool) ([]rpcMsg, int, *fetchErr) {
	var body []byte
	var err error
	if batch {
		body, err = json.Marshal(reqs)
	} else {
		body, err = json.Marshal(reqs[0])
	}
	if err != nil {
		return nil, 0, &fetchErr{batchTo: -1, msg: err.Error()}
	}
	req, err := http.NewRequestWithContext(p.ctx, http.MethodPost, e.spec.URL, bytes.NewReader(body))
	if err != nil {
		return nil, 0, &fetchErr{batchTo: -1, msg: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "em-rpc-extract/0.1")
	if !paced {
		if w := e.lim.reserve(time.Now()); w > 0 && !p.sleep(w) {
			return nil, 0, &fetchErr{batchTo: -1, msg: "cancelled"}
		}
	}
	t0 := time.Now()
	e.requests.Add(1)
	e.calls.Add(uint64(len(reqs)))
	resp, err := e.client.Do(req)
	if err != nil {
		return nil, 0, &fetchErr{batchTo: -1, msg: "transport: " + err.Error()}
	}
	data, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	e.observe(float64(time.Since(t0).Microseconds()) / 1000)
	e.bytes.Add(uint64(len(data)))
	if err != nil {
		return nil, len(data), &fetchErr{batchTo: -1, msg: "read: " + err.Error()}
	}
	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusServiceUnavailable && isRateMsg(0, string(data)) {
		return nil, len(data), &fetchErr{rate: true, after: retryAfter(resp.Header.Get("Retry-After")), batchTo: -1, msg: fmt.Sprintf("http %d: %.200s", resp.StatusCode, data)}
	}
	trim := bytes.TrimSpace(data)
	if resp.StatusCode/100 != 2 {
		fe := &fetchErr{batchTo: -1, msg: fmt.Sprintf("http %d: %.200s", resp.StatusCode, data)}
		if batch && isBatchMsg(string(data)) {
			fe.batchTo = len(reqs) / 4 // blocks: halve the number of calls
		} else if isRateMsg(0, string(data)) {
			fe.rate = true
			fe.after = retryAfter(resp.Header.Get("Retry-After"))
		}
		return nil, len(data), fe
	}
	var msgs []rpcMsg
	if len(trim) > 0 && trim[0] == '[' {
		if err := json.Unmarshal(trim, &msgs); err != nil {
			return nil, len(data), &fetchErr{batchTo: -1, msg: "decode: " + err.Error()}
		}
	} else {
		var m rpcMsg
		if err := json.Unmarshal(trim, &m); err != nil {
			return nil, len(data), &fetchErr{batchTo: -1, msg: fmt.Sprintf("decode: %v: %.120s", err, trim)}
		}
		if batch && m.Error != nil {
			// whole batch rejected with a single error object
			fe := &fetchErr{batchTo: -1, msg: fmt.Sprintf("rpc error %d: %s", m.Error.Code, m.Error.Message)}
			if isRateMsg(m.Error.Code, m.Error.Message) {
				fe.rate = true
			} else if isBatchMsg(m.Error.Message) {
				fe.batchTo = len(reqs) / 4
			}
			return nil, len(data), fe
		}
		msgs = []rpcMsg{m}
	}
	return msgs, len(data), nil
}

// Call performs one JSON-RPC call (round-robin over endpoints, retried).
func (p *Pool) Call(ctx context.Context, method string, params ...any) (json.RawMessage, error) {
	if params == nil {
		params = []any{}
	}
	var last error
	for attempt := 0; attempt < p.o.Retries*len(p.eps); attempt++ {
		e := p.eps[p.rr.Add(1)%uint64(len(p.eps))]
		if until := e.pause.Load(); until > time.Now().UnixNano() {
			d := time.Until(time.Unix(0, until))
			if d > 5*time.Second {
				d = 5 * time.Second
			}
			if !p.sleep(d) {
				return nil, p.ctx.Err()
			}
		}
		msgs, _, ferr := p.post(e, []rpcReq{{"2.0", 1, method, params}}, false, false)
		if ferr == nil && len(msgs) == 1 && msgs[0].Error == nil {
			return msgs[0].Result, nil
		}
		if ferr != nil {
			last = fmt.Errorf("%s: %s", e.host, ferr.msg)
			if ferr.rate {
				e.setRate(ferr.msg)
				p.backoff(e, ferr.after)
			}
		} else if len(msgs) == 1 {
			last = fmt.Errorf("%s: rpc error %d: %s", e.host, msgs[0].Error.Code, msgs[0].Error.Message)
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		p.sleep(time.Duration(250*(1<<min(attempt, 5))) * time.Millisecond)
	}
	return nil, fmt.Errorf("%s failed: %v", method, last)
}

// CallEach performs a call on every endpoint (for chain id / head checks).
func (p *Pool) CallEach(ctx context.Context, method string, params ...any) ([]json.RawMessage, []error) {
	if params == nil {
		params = []any{}
	}
	res := make([]json.RawMessage, len(p.eps))
	errs := make([]error, len(p.eps))
	var wg sync.WaitGroup
	for i, e := range p.eps {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for attempt := 0; attempt < 4; attempt++ {
				msgs, _, ferr := p.post(e, []rpcReq{{"2.0", 1, method, params}}, false, false)
				if ferr == nil && len(msgs) == 1 && msgs[0].Error == nil {
					res[i], errs[i] = msgs[0].Result, nil
					return
				}
				if ferr != nil {
					errs[i] = errors.New(ferr.msg)
				} else if len(msgs) == 1 {
					errs[i] = fmt.Errorf("rpc error %d: %s", msgs[0].Error.Code, msgs[0].Error.Message)
				} else {
					errs[i] = errors.New("empty response")
				}
				if !p.sleep(time.Duration(500*(1<<attempt)) * time.Millisecond) {
					return
				}
			}
		}()
	}
	wg.Wait()
	return res, errs
}
