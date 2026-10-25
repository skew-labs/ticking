// em-trace calls debug_traceBlockByNumber (callTracer) on a Nitro node and
// writes value-carrying internal native transfers as chunked, resumable Parquet.
package main

import (
	"context"
	"flag"
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/em-mapping/extractor/internal/trace"
)

// First traceable block per chain. arb1: 22207817 is the Nitro genesis (its
// parent state is Classic), so tracing starts at 22207818.
var traceStart = map[uint64]uint64{42161: 22207818, 4663: 1}

func main() {
	var (
		rpc       = flag.String("rpc", "http://127.0.0.1:8547", "Nitro JSON-RPC endpoint (debug namespace enabled)")
		chainID   = flag.Uint64("chain", 42161, "chain id")
		from      = flag.Uint64("from", 0, "first block (inclusive)")
		to        = flag.Int64("to", -1, "last block (inclusive), required")
		workers   = flag.Int("workers", 16, "concurrent chunks (= concurrent RPC calls)")
		chunkSize = flag.Uint64("chunk", 10000, "blocks per chunk")
		out       = flag.String("out", "", "output directory")
		start     = flag.Int64("trace-start", -1, "override first traceable block (lower blocks are TRACE_UNAVAILABLE)")
		retries   = flag.Int("retries", 5, "retries per block (exponential backoff)")
		httpTO    = flag.Duration("http-timeout", 10*time.Minute, "HTTP timeout per call")
		tracerTO  = flag.String("tracer-timeout", "", "optional callTracer 'timeout' option (e.g. 120s); empty = node default (5s)")
		force     = flag.Bool("force", false, "redo chunks even if a valid manifest exists")
		checkSHA  = flag.Bool("resume-sha256", true, "validate sha256 of existing chunk files on resume")
		quiet     = flag.Bool("quiet", false, "less logging")
	)
	flag.Parse()
	if *out == "" || *to < 0 {
		fatalf("--out and --to are required")
	}
	ts, ok := traceStart[*chainID]
	if *start >= 0 {
		ts = uint64(*start)
	} else if !ok {
		fatalf("unknown chain %d: pass --trace-start", *chainID)
	}
	u, err := url.Parse(*rpc)
	if err != nil || u.Host == "" {
		fatalf("bad --rpc")
	}
	c := trace.NewClient(*rpc, *workers, *httpTO)
	c.Retries = *retries
	c.TracerTimeout = *tracerTO
	// Never echo credentials / query strings of the endpoint.
	fmt.Fprintf(os.Stderr, "em-trace: rpc=%s://%s chain=%d blocks=[%d,%d] trace_start=%d workers=%d chunk=%d\n", u.Scheme, u.Hostname(), *chainID, *from, *to, ts, *workers, *chunkSize)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	s, err := trace.Run(ctx, trace.Options{ChainID: *chainID, TraceStart: ts, From: *from, To: uint64(*to), Workers: *workers, Chunk: *chunkSize, Out: *out, Client: c, Force: *force, CheckSHA: *checkSHA, Quiet: *quiet})
	if s != nil {
		fmt.Fprintf(os.Stderr, "summary: chunks=%d done=%d skipped=%d unavailable=%d failed=%d traced_blocks=%d rows=%d elapsed=%s\n", s.Chunks, s.Done, s.Skipped, s.Unavailable, s.Failed, s.Blocks, s.Rows, s.Elapsed.Round(time.Second))
		for _, f := range s.Failures {
			fmt.Fprintf(os.Stderr, "FAILED %s\n", f)
		}
	}
	if err != nil {
		fatalf("%v", err)
	}
	if s.Failed > 0 {
		os.Exit(2)
	}
}

func fatalf(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "em-trace: "+f+"\n", a...)
	os.Exit(1)
}
