// em-rpc-extract fetches blocks over Nitro JSON-RPC (eth_getBlockByNumber
// full + eth_getBlockReceipts, batched, from one or more endpoints), rebuilds
// the raw header / body / storage receipts with the OffchainLabs fork types,
// verifies block hash, parent link, tx root, receipt root and bloom with the
// em-extract decoder, and writes the same chunked Parquet tables + manifests
// as em-extract (interchangeable chunks). --follow keeps tailing the head.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ethereum/go-ethereum/common"

	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/extract"
	"github.com/em-mapping/extractor/internal/rpcsrc"
	"github.com/em-mapping/extractor/internal/sysutil"
)

var known = map[uint64]struct {
	nitroGenesis uint64
	weth         []string
}{
	42161: {22207817, []string{"0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"}},
	4663:  {0, nil},
}

type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	for _, s := range strings.Split(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			*m = append(*m, s)
		}
	}
	return nil
}

func main() {
	var rpcs, linkDirs multiFlag
	flag.Var(&linkDirs, "link-dir", "other output dir(s) (e.g. the em-extract snapshot output) whose OK manifests anchor the parent-hash link of the first chunk and are included in the chunk link check")
	flag.Var(&rpcs, "rpc", "RPC endpoint URL[#batch=B&workers=W]; repeat the flag or comma-separate for several (work is shared across all)")
	var (
		chainID     = flag.Uint64("chain", 42161, "chain id (42161 or 4663); every endpoint must report it")
		from        = flag.Int64("from", -1, "first block (inclusive, required)")
		to          = flag.Int64("to", -1, "last block (inclusive); -1 = head - lag")
		workers     = flag.Int("workers", 8, "concurrent HTTP requests per endpoint (override per endpoint with #workers=)")
		batch       = flag.Int("batch", 10, "blocks per JSON-RPC batch request (2 calls per block); 0 = no batching (override per endpoint with #batch=)")
		chunkSize   = flag.Uint64("chunk", 100000, "blocks per chunk (aligned); e.g. 10000 for tails")
		parallel    = flag.Int("parallel", 4, "chunks fetched+decoded concurrently")
		window      = flag.Uint64("window", 0, "blocks per chunk read window (0 = auto from endpoint capacity)")
		out         = flag.String("out", "", "output directory (same layout as em-extract)")
		follow      = flag.Bool("follow", false, "after --to (or the head) keep polling the head and write rolling --follow-chunk chunks")
		lag         = flag.Uint64("lag", 20, "safety lag: only blocks <= head - lag are extracted")
		followChunk = flag.Uint64("follow-chunk", 1000, "chunk size in --follow mode (aligned)")
		poll        = flag.Duration("poll", 2*time.Second, "head polling interval in --follow mode")
		retries     = flag.Int("retries", 8, "attempts per block for real errors (across endpoints) before the chunk fails")
		rateRetries = flag.Int("rate-retries", 200, "attempts per block that were rate limited (429 / Retry-After honoured) before the chunk fails")
		httpTimeout = flag.Duration("http-timeout", 60*time.Second, "per HTTP request timeout")
		verify      = flag.Bool("verify", true, "verify header hash, parent link, tx/receipt roots, uncle hash")
		verifyBloom = flag.Bool("verify-bloom", true, "also verify the header logs bloom")
		resumeCheck = flag.String("resume-check", "sha256", "how to validate existing chunks: sha256 | size")
		force       = flag.Bool("force", false, "re-extract chunks even if a valid manifest exists")
		nitroGen    = flag.Int64("nitro-genesis", -1, "override first Nitro block (blocks below are Classic)")
		wethFlag    = flag.String("weth", "", "WETH allowlist for Deposit/Withdrawal (same as em-extract)")
		senderErr   = flag.String("on-sender-error", "fail", "fail | zero (as em-extract)")
		bench       = flag.Uint64("bench", 0, "benchmark: extract N blocks from --from into a temp dir, print blocks/s and endpoint stats, delete output")
		benchKeep   = flag.Bool("bench-keep", false, "keep benchmark output")
		benchTime   = flag.Duration("bench-time", 0, "benchmark: stop after this long and report per-endpoint blocks/s (0 = run to completion)")
		maxCalls    = flag.Uint64("max-calls", 0, "per-endpoint cap on JSON-RPC calls (an endpoint stops taking work when reached; 0 = unlimited)")
		procs       = flag.Int("procs", 0, "GOMAXPROCS (0 = all cores)")
		quiet       = flag.Bool("quiet", false, "less logging")
	)
	flag.Parse()
	if *procs > 0 {
		runtime.GOMAXPROCS(*procs)
	}
	if len(rpcs) == 0 {
		fatalf("--rpc is required")
	}
	if *from < 0 {
		fatalf("--from is required")
	}
	k, ok := known[*chainID]
	if !ok && *nitroGen < 0 {
		fatalf("unknown chain %d: pass --nitro-genesis", *chainID)
	}
	ng := k.nitroGenesis
	if *nitroGen >= 0 {
		ng = uint64(*nitroGen)
	}
	if *senderErr != "fail" && *senderErr != "zero" {
		fatalf("--on-sender-error must be fail or zero")
	}
	weth := &decode.WETHFilter{Allow: map[common.Address]bool{}}
	switch strings.ToLower(strings.TrimSpace(*wethFlag)) {
	case "":
		for _, a := range k.weth {
			weth.Allow[common.HexToAddress(a)] = true
		}
	case "any":
		weth.Any = true
	case "none":
	default:
		for _, a := range strings.Split(*wethFlag, ",") {
			a = strings.TrimSpace(a)
			if !common.IsHexAddress(a) {
				fatalf("bad --weth address %q", a)
			}
			weth.Allow[common.HexToAddress(a)] = true
		}
	}
	var specs []rpcsrc.EndpointSpec
	capacity := 0
	for _, r := range rpcs {
		s, err := rpcsrc.ParseEndpoint(r, *batch, *workers)
		if err != nil {
			fatalf("%v", err)
		}
		specs = append(specs, s)
		capacity += s.Workers * max(s.Batch, 1)
	}
	sysutil.RaiseFileLimit()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	pool, err := rpcsrc.NewPool(ctx, specs, rpcsrc.PoolOptions{ChainID: *chainID, Retries: *retries, RateRetries: *rateRetries, HTTPTimeout: *httpTimeout, MaxCalls: *maxCalls})
	if err != nil {
		fatalf("%v", err)
	}
	defer pool.Close()
	if err := pool.CheckChainID(ctx, *chainID); err != nil {
		fatalf("%v", err)
	}
	head, heads, err := pool.Head(ctx)
	if err != nil {
		fatalf("%v", err)
	}
	logf("endpoints: %s", strings.Join(heads, " "))
	for _, s := range specs {
		logf("endpoint %s batch=%d workers=%d", rpcsrc.Redact(s.URL), s.Batch, s.Workers)
	}
	safe := int64(head) - int64(*lag)
	if safe < 0 {
		safe = 0
	}

	win := *window
	if win == 0 {
		// Enough outstanding blocks to keep every endpoint busy while the
		// other chunks decode.
		win = uint64((4*capacity + *parallel - 1) / max(*parallel, 1))
		if win < 256 {
			win = 256
		}
	}
	opts := extract.Options{
		ChainID: *chainID, NitroGenesis: ng, Workers: *parallel, Chunk: *chunkSize, Batch: win, Out: *out,
		Verify: *verify, VerifyBloom: *verify && *verifyBloom, ResumeCheck: *resumeCheck, Force: *force, WETH: weth, Quiet: *quiet, SenderZero: *senderErr == "zero",
	}

	if *bench > 0 {
		runBench(ctx, pool, opts, uint64(*from), *bench, uint64(safe), *benchKeep, *benchTime)
		return
	}
	if *out == "" {
		fatalf("--out is required")
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		fatalf("%v", err)
	}
	src := rpcsrc.NewSource(ctx, pool, *out)
	for _, d := range linkDirs {
		logf("link-dir %s: %d OK manifests indexed", d, src.LoadManifests(d))
	}
	linkRoots := append([]string{*out}, linkDirs...)
	hosts := pool.Hosts()
	opts.ChunkDone = func(r chunk.Range, m *chunk.Manifest) {
		if m.Extra == nil {
			m.Extra = map[string]any{}
		}
		m.Extra["source"] = "rpc"
		m.Extra["source_tool"] = "em-rpc-extract"
		m.Extra["rpc_endpoints"] = hosts
		if m.Status == chunk.StatusOK {
			if h, ok := m.Extra["last_block_hash"].(string); ok {
				src.Remember(r.To, common.HexToHash(h))
			}
		}
	}

	exit := 0
	next := uint64(*from)
	last := uint64(safe)
	if *to >= 0 {
		last = uint64(*to)
		if last > uint64(safe) && !*follow {
			fatalf("--to %d beyond safe head %d (head %d - lag %d)", last, safe, head, *lag)
		}
	} else if *follow && *followChunk > 0 {
		// End the bulk phase on a follow-chunk boundary so rolling chunks stay aligned.
		if e := (last+1) / *followChunk * *followChunk; e > next {
			last = e - 1
		}
	}
	if last > uint64(safe) {
		last = uint64(safe) // --follow with a future --to: bulk up to the safe head, follow the rest
	}
	if next <= last {
		logf("bulk: blocks [%d,%d] chunk=%d parallel=%d window=%d (head %d, lag %d)", next, last, *chunkSize, *parallel, win, head, *lag)
		o := opts
		o.From, o.To = next, last
		sum, err := extract.Run(ctx, src, o)
		if sum != nil {
			printSummary(sum)
		}
		printStats(pool)
		if err != nil {
			fatalf("run: %v", err)
		}
		if sum.Failed > 0 {
			exit = 2
		}
		if bad := checkLinks(linkRoots, *chainID, next, last); bad > 0 {
			exit = 2
		}
		next = last + 1
		if exit != 0 && *follow {
			fatalf("bulk phase had failed chunks; rerun (resumes) before following")
		}
	}
	if !*follow {
		os.Exit(exit)
	}
	stopAt := uint64(1<<63 - 1)
	if *to >= 0 {
		stopAt = uint64(*to) // follow until --to is reached (e.g. a local node catching up)
	}
	fc := *followChunk
	if fc == 0 {
		fc = 1000
	}
	logf("follow: from %d chunk=%d lag=%d poll=%s", next, fc, *lag, *poll)
	var lastStats atomic.Int64
	lastStats.Store(time.Now().Unix())
	for ctx.Err() == nil && next <= stopAt {
		h, _, err := pool.Head(ctx)
		if err != nil {
			logf("follow: head: %v", err)
			sleepCtx(ctx, *poll)
			continue
		}
		if h < *lag {
			sleepCtx(ctx, *poll)
			continue
		}
		safe := h - *lag
		if (safe+1)/fc*fc == 0 {
			sleepCtx(ctx, *poll)
			continue
		}
		end := (safe+1)/fc*fc - 1 // last full aligned chunk end <= safe
		if stopAt <= safe && end < stopAt {
			end = stopAt // final partial chunk up to --to
		}
		if end > stopAt {
			end = stopAt
		}
		if end < next {
			if time.Now().Unix()-lastStats.Load() > 300 {
				printStats(pool)
				lastStats.Store(time.Now().Unix())
			}
			sleepCtx(ctx, *poll)
			continue
		}
		o := opts
		o.From, o.To, o.Chunk = next, end, fc
		o.Quiet = true
		o.Log = os.Stderr
		t0 := time.Now()
		sum, err := extract.Run(ctx, src, o)
		if err != nil {
			if ctx.Err() != nil {
				break
			}
			logf("follow: run [%d,%d]: %v", next, end, err)
			sleepCtx(ctx, *poll)
			continue
		}
		if sum.Failed > 0 {
			for _, f := range sum.Failures {
				logf("follow: FAILED %s (will retry)", f)
			}
			sleepCtx(ctx, *poll)
			continue // resume re-runs only the failed chunks; nothing is skipped
		}
		if bad := checkLinks(linkRoots, *chainID, next, end); bad > 0 {
			fatalf("follow: chunk link mismatch in [%d,%d] (reorg?); inspect and rerun with --force for the affected chunks", next, end)
		}
		logf("follow: [%d,%d] done (%d chunks, %d blocks, %.1fs) head=%d safe=%d behind=%d", next, end, sum.Chunks, sum.Blocks, time.Since(t0).Seconds(), h, safe, safe-end)
		next = end + 1
	}
	printStats(pool)
}

func sleepCtx(ctx context.Context, d time.Duration) {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
	case <-ctx.Done():
	}
}

// checkLinks verifies first_parent_hash == previous chunk's last_block_hash
// for every adjacent pair of OK manifests touching [from, to] (including an
// em-extract chunk ending at from-1 if it is in the same directory).
func checkLinks(roots []string, chainID, from, to uint64) int {
	var files []string
	for _, out := range roots {
		f, _ := filepath.Glob(filepath.Join(out, "manifests", "*.json"))
		files = append(files, f...)
	}
	var ms []*chunk.Manifest
	for _, f := range files {
		if strings.Contains(filepath.Base(f), ".tmp-") {
			continue
		}
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		var m chunk.Manifest
		if json.Unmarshal(b, &m) != nil || m.Status != chunk.StatusOK || m.ChainID != chainID {
			continue
		}
		if m.Range.To+1 < from || m.Range.From > to+1 {
			continue
		}
		ms = append(ms, &m)
	}
	sort.Slice(ms, func(i, j int) bool { return ms[i].Range.From < ms[j].Range.From })
	checked, bad, gaps := 0, 0, 0
	for i := 1; i < len(ms); i++ {
		a, b := ms[i-1], ms[i]
		if a.Range.To+1 != b.Range.From {
			if b.Range.From > a.Range.To+1 && b.Range.From <= to {
				gaps++
				logf("links: gap between %s and %s", a.Range.Name(), b.Range.Name())
			}
			continue
		}
		lh, _ := a.Extra["last_block_hash"].(string)
		fp, _ := b.Extra["first_parent_hash"].(string)
		if lh == "" || fp == "" {
			continue
		}
		checked++
		if !strings.EqualFold(lh, fp) {
			bad++
			logf("links: MISMATCH %s last=%s vs %s parent=%s", a.Range.Name(), lh, b.Range.Name(), fp)
		}
	}
	if checked > 0 || bad > 0 || gaps > 0 {
		logf("links: checked=%d mismatches=%d gaps=%d", checked, bad, gaps)
	}
	return bad
}

func printSummary(s *extract.Summary) {
	logf("summary: chunks=%d done=%d skipped=%d failed=%d blocks=%d elapsed=%s rate=%.0f blocks/s out=%.3f GB",
		s.Chunks, s.Done, s.Skipped, s.Failed, s.Blocks, s.Elapsed.Round(time.Second), float64(s.Blocks)/s.Elapsed.Seconds(), float64(s.OutBytes)/1e9)
	for _, f := range s.Failures {
		logf("FAILED %s", f)
	}
}

func printStats(p *rpcsrc.Pool) {
	for _, s := range p.Stats() {
		logf("rpc %s: batch=%d workers=%d requests=%d calls=%d blocks=%d rate_limited=%d errors=%d MB=%.1f latency mean=%.0fms p50=%.0fms p90=%.0fms max=%.0fms%s",
			s.Host, s.Batch, s.Workers, s.Requests, s.Calls, s.Blocks, s.RateLimited, s.Errors, float64(s.Bytes)/1e6, s.MeanMs, s.P50Ms, s.P90Ms, s.MaxMs, lastErr(s.LastError))
	}
}

func lastErr(s string) string {
	if s == "" {
		return ""
	}
	return " last_error=" + strings.ReplaceAll(s, "\n", " ")
}

func runBench(ctx context.Context, pool *rpcsrc.Pool, o extract.Options, from, n, safe uint64, keep bool, limit time.Duration) {
	o.From = from
	o.To = from + n - 1
	if o.To > safe {
		o.To = safe
		n = o.To - from + 1
	}
	per := (n + uint64(o.Workers) - 1) / uint64(o.Workers)
	if per < 50 {
		per = 50
	}
	if o.Chunk > per {
		o.Chunk = per
	}
	if o.Batch > o.Chunk {
		o.Batch = o.Chunk
	}
	base := o.Out
	if base != "" {
		os.MkdirAll(base, 0o755)
	}
	dir, err := os.MkdirTemp(base, "em-rpc-bench-")
	if err != nil {
		fatalf("bench dir: %v", err)
	}
	o.Out = dir
	o.Force = true
	o.Quiet = true
	rctx := ctx
	if limit > 0 {
		var c context.CancelFunc
		rctx, c = context.WithTimeout(ctx, limit)
		defer c()
	}
	src := rpcsrc.NewSource(rctx, pool, "")
	logf("bench: blocks [%d,%d] parallel=%d chunk=%d window=%d verify=%v time_limit=%s out=%s", o.From, o.To, o.Workers, o.Chunk, o.Batch, o.Verify, limit, dir)
	s, err := extract.Run(rctx, src, o)
	if err != nil && rctx.Err() == nil {
		fatalf("bench: %v", err)
	}
	el := s.Elapsed.Seconds()
	var okBlocks, failed uint64
	failedChunks := 0
	for _, f := range s.Failures {
		if isCancel(f) {
			continue
		}
		failedChunks++
	}
	okBlocks = s.Blocks
	_ = failed
	var fetched uint64
	for _, st := range pool.Stats() {
		fetched += st.Blocks
	}
	fmt.Printf("bench_range=%d verified_blocks_written=%d blocks_fetched=%d elapsed_sec=%.2f written_blocks_per_sec=%.1f fetched_blocks_per_sec=%.1f chunks=%d done=%d failed=%d cancelled=%d verify_ok=%v\n",
		n, okBlocks, fetched, el, float64(okBlocks)/el, float64(fetched)/el, s.Chunks, s.Done, failedChunks, s.Failed-failedChunks, o.Verify && failedChunks == 0)
	n = max(okBlocks, 1)
	fmt.Printf("bytes_per_block: json=%.0f rlp=%.0f parquet=%.0f\n", float64(s.DiskBytes)/float64(n), float64(s.RLPBytes)/float64(n), float64(s.OutBytes)/float64(n))
	tot := float64(s.ReadNs + s.DecodeNs + s.WriteNs)
	if tot > 0 {
		fmt.Printf("worker_time_split: fetch=%.0f%% decode+verify=%.0f%% parquet=%.0f%%\n", 100*float64(s.ReadNs)/tot, 100*float64(s.DecodeNs)/tot, 100*float64(s.WriteNs)/tot)
	}
	for _, f := range s.Failures {
		if !isCancel(f) {
			fmt.Printf("FAILED %s\n", f)
		}
	}
	for _, st := range pool.Stats() {
		b, _ := json.Marshal(st)
		fmt.Printf("endpoint %s blocks_per_sec=%.1f\n", b, float64(st.Blocks)/el)
	}
	if !keep {
		os.RemoveAll(dir)
	} else {
		fmt.Printf("output kept at %s\n", dir)
	}
	if failedChunks > 0 {
		os.Exit(2)
	}
}

// isCancel reports failures caused by the benchmark's own time / call limits.
func isCancel(f string) bool {
	return strings.Contains(f, "CANCELLED") || strings.Contains(f, "context deadline exceeded") || strings.Contains(f, "context canceled") || strings.Contains(f, "--max-calls")
}

func logf(f string, a ...any) {
	fmt.Fprintf(os.Stderr, time.Now().UTC().Format("15:04:05")+" "+f+"\n", a...)
}

func fatalf(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "em-rpc-extract: "+f+"\n", a...)
	os.Exit(1)
}
