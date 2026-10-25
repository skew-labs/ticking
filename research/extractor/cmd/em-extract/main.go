// em-extract reads an Arbitrum Nitro chain database (Pebble + geth ancient
// freezer) directly from disk, strictly read-only, and writes verified,
// chunked, zstd Parquet for wallet mapping.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"runtime/pprof"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/ethereum/go-ethereum/common"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/extract"
	"github.com/em-mapping/extractor/internal/sysutil"
)

// Known chains: Nitro genesis block (first Nitro block; lower = Classic) and
// the default WETH allowlist for Deposit/Withdrawal decoding.
var known = map[uint64]struct {
	nitroGenesis uint64
	weth         []string
}{
	42161: {22207817, []string{"0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"}},
	4663:  {0, nil},
}

func main() {
	var (
		datadir     = flag.String("datadir", "", "path to nitro l2chaindata (or a parent / snapshot root; auto-detected)")
		ancient     = flag.String("ancient", "", "explicit chain freezer dir (default: <l2chaindata>/ancient/chain or <l2chaindata>/ancient)")
		chainID     = flag.Uint64("chain", 42161, "chain id (42161 or 4663)")
		from        = flag.Uint64("from", 0, "first block (inclusive)")
		to          = flag.Int64("to", -1, "last block (inclusive); -1 = head")
		workers     = flag.Int("workers", runtime.NumCPU(), "parallel chunk workers")
		chunkSize   = flag.Uint64("chunk", 100000, "blocks per chunk")
		batch       = flag.Uint64("batch", 512, "blocks per freezer read batch")
		out         = flag.String("out", "", "output directory")
		verify      = flag.Bool("verify", true, "verify header hash, parent link, tx/receipt roots, uncle hash")
		verifyBloom = flag.Bool("verify-bloom", true, "also verify the header logs bloom (requires --verify)")
		resumeCheck = flag.String("resume-check", "sha256", "how to validate existing chunks: sha256 | size")
		force       = flag.Bool("force", false, "re-extract chunks even if a valid manifest exists")
		bench       = flag.Uint64("bench", 0, "benchmark mode: process N blocks from --from into a temp dir and report blocks/s and bytes/block")
		benchKeep   = flag.Bool("bench-keep", false, "keep benchmark output")
		nitroGen    = flag.Int64("nitro-genesis", -1, "override first Nitro block (blocks below are decoded as Classic)")
		wethFlag    = flag.String("weth", "", "comma-separated WETH addresses whose Deposit/Withdrawal events are decoded (default: per-chain allowlist); 'any' = every emitter; 'none' = disabled")
		cacheMB     = flag.Int("pebble-cache-mb", 1024, "pebble block cache (MB)")
		senderErr   = flag.String("on-sender-error", "fail", "signature recovery failure: fail (chunk FAILED) | zero (record 0x0 like Nitro RPC, counted in manifest)")
		info        = flag.Bool("info", false, "print database info and exit")
		noKV        = flag.Bool("no-kv", false, "freezer only, no key-value store (needs --ancient; blocks past the freezer are unreadable)")
		family      = flag.String("family", "", "chain family: arbitrum (Nitro, default for 42161/4663) | geth (Linea, Polygon bor, other geth-freezer chains)")
		quiet       = flag.Bool("quiet", false, "less logging")
		cpuProfile  = flag.String("cpuprofile", "", "write a CPU profile to this file")
		memProfile  = flag.String("memprofile", "", "write an allocation profile to this file at exit (bench mode)")
	)
	flag.Parse()
	if *cpuProfile != "" {
		f, err := os.Create(*cpuProfile)
		if err != nil {
			fatalf("cpuprofile: %v", err)
		}
		pprof.StartCPUProfile(f)
		defer pprof.StopCPUProfile()
	}
	if *datadir == "" && *ancient == "" {
		fatalf("--datadir is required")
	}
	k, ok := known[*chainID]
	if !ok && *nitroGen < 0 {
		fatalf("unknown chain %d: pass --nitro-genesis", *chainID)
	}
	ng := k.nitroGenesis
	if *nitroGen >= 0 {
		ng = uint64(*nitroGen)
	}
	sysutil.RaiseFileLimit()

	db, err := chaindb.Open(chaindb.Options{DataDir: *datadir, AncientDir: *ancient, CacheMB: *cacheMB, NoKV: *noKV})
	if err != nil {
		fatalf("open database: %v", err)
	}
	defer db.Close()
	head, hasHead := db.Head()
	fmt.Fprintf(os.Stderr, "l2chaindata=%s engine=%s ancient=%s frozen=[%d,%d) head=%d(%v)\n", db.ChainDataDir, db.Engine, db.AncientDir, db.Tail, db.Frozen, head, hasHead)
	for _, w := range db.Warnings {
		fmt.Fprintf(os.Stderr, "warning: %s\n", w)
	}
	if cfg := db.ChainConfig(); cfg != nil {
		if cfg.ChainID != nil && cfg.ChainID.Uint64() != *chainID {
			fatalf("database chain id %s != --chain %d", cfg.ChainID, *chainID)
		}
		if cfg.IsArbitrum() && *nitroGen < 0 && cfg.ArbitrumChainParams.GenesisBlockNum != ng {
			fmt.Fprintf(os.Stderr, "note: stored chain config GenesisBlockNum=%d (default %d); using stored value\n", cfg.ArbitrumChainParams.GenesisBlockNum, ng)
			ng = cfg.ArbitrumChainParams.GenesisBlockNum
		}
		fmt.Fprintf(os.Stderr, "chain config: chainId=%v arbitrum=%v nitroGenesis=%d\n", cfg.ChainID, cfg.IsArbitrum(), ng)
	} else {
		fmt.Fprintf(os.Stderr, "note: no stored chain config readable; using chain=%d nitroGenesis=%d\n", *chainID, ng)
	}
	if *info {
		g := db.CanonicalHash(0)
		fmt.Printf("genesis_hash=%s nitro_genesis=%d nitro_genesis_hash=%s frozen=%d tail=%d head=%d\n", g, ng, db.CanonicalHash(ng), db.Frozen, db.Tail, head)
		return
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

	last := uint64(0)
	switch {
	case *to >= 0:
		last = uint64(*to)
	case hasHead:
		last = head
	default:
		fatalf("cannot determine head; pass --to")
	}
	if hasHead && last > head {
		fatalf("--to %d beyond head %d", last, head)
	}
	opts := extract.Options{
		Family: *family, ChainID: *chainID, NitroGenesis: ng, From: *from, To: last, Workers: *workers, Chunk: *chunkSize, Batch: *batch,
		Out: *out, Verify: *verify, VerifyBloom: *verify && *verifyBloom, ResumeCheck: *resumeCheck, Force: *force, WETH: weth, Quiet: *quiet, SenderZero: *senderErr == "zero",
	}
	if *bench > 0 {
		runBench(db, opts, *bench, *benchKeep)
		if *memProfile != "" {
			if f, err := os.Create(*memProfile); err == nil {
				pprof.Lookup("allocs").WriteTo(f, 0)
				f.Close()
			}
		}
		return
	}
	if *out == "" {
		fatalf("--out is required")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	sum, err := extract.Run(ctx, db, opts)
	if sum != nil {
		printSummary(sum)
	}
	if err != nil {
		fatalf("run: %v", err)
	}
	if sum.Failed > 0 {
		os.Exit(2)
	}
}

func printSummary(s *extract.Summary) {
	fmt.Fprintf(os.Stderr, "summary: chunks=%d done=%d skipped=%d failed=%d blocks=%d elapsed=%s rate=%.0f blocks/s out=%.2f GB\n",
		s.Chunks, s.Done, s.Skipped, s.Failed, s.Blocks, s.Elapsed.Round(time.Second), float64(s.Blocks)/s.Elapsed.Seconds(), float64(s.OutBytes)/1e9)
	for _, f := range s.Failures {
		fmt.Fprintf(os.Stderr, "FAILED %s\n", f)
	}
}

func runBench(db *chaindb.DB, o extract.Options, n uint64, keep bool) {
	o.To = o.From + n - 1
	if head, ok := db.Head(); ok && o.To > head {
		o.To = head
		n = o.To - o.From + 1
	}
	per := (n + uint64(o.Workers) - 1) / uint64(o.Workers)
	if per < 1000 {
		per = 1000
	}
	if o.Chunk > per {
		o.Chunk = per
	}
	dir := o.Out
	var err error
	if dir == "" {
		dir, err = os.MkdirTemp("", "em-bench-")
	} else {
		dir, err = os.MkdirTemp(dir, "em-bench-")
	}
	if err != nil {
		fatalf("bench dir: %v", err)
	}
	o.Out = dir
	o.Force = true
	o.Quiet = true
	fmt.Fprintf(os.Stderr, "bench: blocks [%d,%d] workers=%d chunk=%d verify=%v out=%s\n", o.From, o.To, o.Workers, o.Chunk, o.Verify, dir)
	s, err := extract.Run(context.Background(), db, o)
	if err != nil {
		fatalf("bench: %v", err)
	}
	el := s.Elapsed.Seconds()
	fmt.Printf("bench_blocks=%d elapsed_sec=%.2f blocks_per_sec=%.0f\n", n, el, float64(n)/el)
	fmt.Printf("bytes_per_block: rlp=%.0f disk_read=%.0f parquet=%.0f\n", float64(s.RLPBytes)/float64(n), float64(s.DiskBytes)/float64(n), float64(s.OutBytes)/float64(n))
	tot := float64(s.ReadNs + s.DecodeNs + s.WriteNs)
	if tot > 0 {
		fmt.Printf("worker_time_split: read=%.0f%% decode+verify=%.0f%% parquet=%.0f%%\n", 100*float64(s.ReadNs)/tot, 100*float64(s.DecodeNs)/tot, 100*float64(s.WriteNs)/tot)
	}
	var names []string
	for t := range s.TableBytes {
		names = append(names, t)
	}
	sort.Strings(names)
	for _, t := range names {
		fmt.Printf("parquet_bytes_per_block.%s=%.1f\n", t, float64(s.TableBytes[t])/float64(n))
	}
	fmt.Printf("chunks=%d failed=%d\n", s.Chunks, s.Failed)
	for _, f := range s.Failures {
		fmt.Printf("FAILED %s\n", f)
	}
	if !keep {
		os.RemoveAll(dir)
	} else {
		fmt.Printf("output kept at %s\n", filepath.Clean(dir))
	}
	if s.Failed > 0 {
		os.Exit(2)
	}
}

func fatalf(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "em-extract: "+f+"\n", a...)
	os.Exit(1)
}
