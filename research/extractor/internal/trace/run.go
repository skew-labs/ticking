package trace

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"sort"
	"sync"
	"time"

	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/pq"
	"github.com/em-mapping/extractor/internal/schema"
)

// Table names.
const (
	TNative    = "native_internal"
	TCreations = "internal_creations"
)

// Tables lists all em-trace tables.
var Tables = []string{TNative, TCreations}

// Version of the em-trace output format.
const Version = "0.1.0"

// ReasonClassic marks blocks that cannot be traced (pre-Nitro / Nitro genesis).
const ReasonClassic = "CLASSIC_PRE_NITRO"

// Options configure Run.
type Options struct {
	ChainID    uint64
	TraceStart uint64 // first traceable block; lower blocks are TRACE_UNAVAILABLE
	From, To   uint64
	Workers    int
	Chunk      uint64
	Out        string
	Client     *Client
	Force      bool
	CheckSHA   bool
	Log        io.Writer
	Quiet      bool
}

// Latency summarises per-chunk RPC latency.
type Latency struct {
	Calls   uint64  `json:"calls"`
	Retries uint64  `json:"retries"`
	Bytes   int64   `json:"bytes"`
	MeanMs  float64 `json:"mean_ms"`
	P50Ms   float64 `json:"p50_ms"`
	P90Ms   float64 `json:"p90_ms"`
	P99Ms   float64 `json:"p99_ms"`
	MaxMs   float64 `json:"max_ms"`
}

func latencyOf(ds []time.Duration, retries uint64, bytes int64) Latency {
	l := Latency{Calls: uint64(len(ds)), Retries: retries, Bytes: bytes}
	if len(ds) == 0 {
		return l
	}
	sort.Slice(ds, func(i, j int) bool { return ds[i] < ds[j] })
	var sum time.Duration
	for _, d := range ds {
		sum += d
	}
	q := func(p float64) float64 {
		i := int(math.Ceil(p*float64(len(ds)))) - 1
		if i < 0 {
			i = 0
		}
		return float64(ds[i].Microseconds()) / 1000
	}
	l.MeanMs = float64(sum.Microseconds()) / 1000 / float64(len(ds))
	l.P50Ms, l.P90Ms, l.P99Ms = q(0.50), q(0.90), q(0.99)
	l.MaxMs = float64(ds[len(ds)-1].Microseconds()) / 1000
	return l
}

type chunkStats struct {
	Stats
	Latency Latency `json:"rpc_latency"`
	Blocks  uint64  `json:"blocks_traced"`
}

// Summary of a run.
type Summary struct {
	Chunks, Done, Skipped, Failed, Unavailable int
	Blocks                                     uint64
	Rows                                       uint64
	Elapsed                                    time.Duration
	Failures                                   []string
}

// Run executes chunked, resumable tracing.
func Run(ctx context.Context, o Options) (*Summary, error) {
	if o.Workers <= 0 {
		o.Workers = 8
	}
	if o.Chunk == 0 {
		o.Chunk = 10000
	}
	if o.Log == nil {
		o.Log = os.Stderr
	}
	if o.To < o.From {
		return nil, fmt.Errorf("--to < --from")
	}
	if err := os.MkdirAll(o.Out, 0o755); err != nil {
		return nil, err
	}
	plan := chunk.Plan(o.From, o.To, o.Chunk)
	sum := &Summary{Chunks: len(plan)}
	start := time.Now()
	jobs := make(chan chunk.Range)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for w := 0; w < o.Workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for r := range jobs {
				if ctx.Err() != nil {
					continue
				}
				if !o.Force {
					if _, err := chunk.Validate(o.Out, r, chunk.Expect{Tool: "em-trace", ChainID: o.ChainID, Statuses: []string{chunk.StatusOK, chunk.StatusTraceUnavailable}, RequireFiles: Tables, CheckSHA: o.CheckSHA}); err == nil {
						mu.Lock()
						sum.Skipped++
						mu.Unlock()
						if !o.Quiet {
							fmt.Fprintf(o.Log, "chunk %s: SKIP (valid manifest)\n", r.Name())
						}
						continue
					}
				}
				m := processChunk(ctx, o, r)
				if err := chunk.Write(o.Out, m); err != nil {
					m.Status = chunk.StatusFailed
					m.Failure = &chunk.Failure{Block: r.From, Reason: "MANIFEST_WRITE", Detail: err.Error()}
				}
				mu.Lock()
				switch m.Status {
				case chunk.StatusOK:
					sum.Done++
					sum.Blocks += r.Len()
					sum.Rows += m.Counts[TNative]
				case chunk.StatusTraceUnavailable:
					sum.Unavailable++
				default:
					sum.Failed++
					sum.Failures = append(sum.Failures, fmt.Sprintf("%s: block %d %s %s", r.Name(), m.Failure.Block, m.Failure.Reason, m.Failure.Detail))
				}
				mu.Unlock()
				if !o.Quiet {
					if m.Failure != nil {
						fmt.Fprintf(o.Log, "chunk %s: FAILED block %d %s: %s\n", r.Name(), m.Failure.Block, m.Failure.Reason, m.Failure.Detail)
					} else {
						fmt.Fprintf(o.Log, "chunk %s: %s native_internal=%d creations=%d %.1f blocks/s\n", r.Name(), m.Status, m.Counts[TNative], m.Counts[TCreations], m.BlocksPerS)
					}
				}
			}
		}()
	}
	for _, r := range plan {
		if ctx.Err() != nil {
			break
		}
		select {
		case jobs <- r:
		case <-ctx.Done():
		}
	}
	close(jobs)
	wg.Wait()
	sum.Elapsed = time.Since(start)
	sort.Strings(sum.Failures)
	return sum, ctx.Err()
}

func processChunk(ctx context.Context, o Options, r chunk.Range) *chunk.Manifest {
	t0 := time.Now()
	cs := &chunkStats{}
	m := &chunk.Manifest{Tool: "em-trace", Version: Version, ChainID: o.ChainID, Range: r, Counts: map[string]uint64{TNative: 0, TCreations: 0}, Files: map[string]pq.FileInfo{},
		Extra: map[string]any{"tracer": map[string]any{"tracer": "callTracer", "tracerConfig": map[string]any{"onlyTopCall": false}, "timeout": o.Client.TracerTimeout}, "rpc": "redacted-host"}}
	finish := func(status string, f *chunk.Failure) *chunk.Manifest {
		m.Status = status
		m.Failure = f
		m.ElapsedSec = time.Since(t0).Seconds()
		if m.ElapsedSec > 0 {
			m.BlocksPerS = float64(cs.Blocks) / m.ElapsedSec
		}
		m.Stats = cs
		return m
	}
	chunk.RemoveFiles(o.Out, Tables, r)
	meta := map[string]string{"em.chain_id": fmt.Sprint(o.ChainID), "em.range": r.Name(), "em.amount_encoding": "uint256 big-endian FIXED_LEN_BYTE_ARRAY(32)"}
	wn, err := pq.Create[schema.NativeInternal](o.Out, chunk.FileRel(TNative, r), 1<<18, meta)
	if err != nil {
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.From, Reason: "OUTPUT", Detail: err.Error()})
	}
	wc, err := pq.Create[schema.InternalCreation](o.Out, chunk.FileRel(TCreations, r), 1<<18, meta)
	if err != nil {
		wn.Abort()
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.From, Reason: "OUTPUT", Detail: err.Error()})
	}
	fail := func(f *chunk.Failure) *chunk.Manifest {
		wn.Abort()
		wc.Abort()
		chunk.RemoveFiles(o.Out, Tables, r)
		return finish(chunk.StatusFailed, f)
	}
	first := r.From
	if first < o.TraceStart {
		last := r.To
		if last >= o.TraceStart {
			last = o.TraceStart - 1
		}
		m.Unavailable = &chunk.Unavailable{From: r.From, To: last, Count: last - r.From + 1, Reason: ReasonClassic}
		first = last + 1
	}
	var lat []time.Duration
	var retries uint64
	var bytes int64
	var nat []schema.NativeInternal
	var cre []schema.InternalCreation
	for n := first; n <= r.To && n >= first; n++ {
		if ctx.Err() != nil {
			return fail(&chunk.Failure{Block: n, Reason: "CANCELLED", Detail: ctx.Err().Error()})
		}
		traces, d, b, rt, err := o.Client.TraceBlock(ctx, n)
		bytes += b
		retries += uint64(rt)
		if err != nil {
			reason := "RPC_ERROR"
			var re *RPCError
			if !errors.As(err, &re) {
				reason = "RPC_TRANSPORT"
			}
			cs.Latency = latencyOf(lat, retries, bytes)
			return fail(&chunk.Failure{Block: n, Reason: reason, Detail: err.Error()})
		}
		lat = append(lat, d)
		nat, cre, err = Extract(n, traces, nat[:0], cre[:0], &cs.Stats)
		if err != nil {
			cs.Latency = latencyOf(lat, retries, bytes)
			return fail(&chunk.Failure{Block: n, Reason: "TRACE_DECODE", Detail: err.Error()})
		}
		if err := errors.Join(wn.Write(nat), wc.Write(cre)); err != nil {
			return fail(&chunk.Failure{Block: n, Reason: "OUTPUT", Detail: err.Error()})
		}
		m.Counts[TNative] += uint64(len(nat))
		m.Counts[TCreations] += uint64(len(cre))
		cs.Blocks++
		if n == math.MaxUint64 {
			break
		}
	}
	cs.Latency = latencyOf(lat, retries, bytes)
	fn, err := wn.Commit()
	if err != nil {
		wc.Abort()
		chunk.RemoveFiles(o.Out, Tables, r)
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.To, Reason: "OUTPUT", Detail: err.Error()})
	}
	fc, err := wc.Commit()
	if err != nil {
		chunk.RemoveFiles(o.Out, Tables, r)
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.To, Reason: "OUTPUT", Detail: err.Error()})
	}
	m.Files[TNative] = fn
	m.Files[TCreations] = fc
	if m.Unavailable != nil && m.Unavailable.Count == r.Len() {
		return finish(chunk.StatusTraceUnavailable, nil)
	}
	return finish(chunk.StatusOK, nil)
}
