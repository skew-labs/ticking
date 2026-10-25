// Package extract runs the chunked, parallel, resumable extraction.
package extract

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/pq"
	"github.com/em-mapping/extractor/internal/schema"
)

// Version of the extractor output format.
const Version = "0.1.0"

// Dependency records the go-ethereum fork used (Nitro v3.12.1 submodule).
const Dependency = "github.com/OffchainLabs/go-ethereum@5eee8f546e7602b7deeef7909f291bbebc77e233 (nitro v3.12.1)"

// Table names (= output sub-directories).
const (
	TBlocks    = "blocks"
	TTxs       = "transactions"
	TLogs      = "logs"
	TTransfers = "token_transfers"
	TCreations = "contract_creations"
)

// Tables lists all em-extract tables.
var Tables = []string{TBlocks, TTxs, TLogs, TTransfers, TCreations}

// Source supplies canonical raw blocks. *chaindb.DB (on-disk snapshot) and
// the JSON-RPC source (internal/rpcsrc) implement it, so both tools write
// byte-identical tables through the same decoder and writers.
type Source interface {
	// ReadRange returns blocks [from, from+count) in order (all parts present)
	// or an error; the second value is the number of source bytes read.
	ReadRange(from, count uint64) ([]chaindb.RawBlock, uint64, error)
	// CanonicalHash returns the canonical hash of block n (zero if unknown).
	CanonicalHash(n uint64) common.Hash
}

// warner is optionally implemented by sources that carry warnings which
// belong in every manifest.
type warner interface{ SourceWarnings() []string }

// Options configure Run.
type Options struct {
	Family string // decode family: "" / "arbitrum" (Nitro) or "geth"
	ChainID      uint64
	NitroGenesis uint64
	From, To     uint64
	Workers      int
	Chunk        uint64
	Batch        uint64 // blocks per read batch
	Out          string
	Verify       bool
	VerifyBloom  bool
	ResumeCheck  string // sha256 | size
	Force        bool   // ignore existing manifests
	WETH         *decode.WETHFilter
	SenderZero   bool // on signature-recovery failure record 0x0 instead of failing
	Log          io.Writer
	Quiet        bool
	// Tool is the manifest tool name (default "em-extract"). Keeping the
	// default makes chunks from other sources interchangeable on resume.
	Tool string
	// ChunkDone, if set, is called for every processed chunk before its
	// manifest is written (e.g. to add source details to m.Extra).
	ChunkDone func(r chunk.Range, m *chunk.Manifest)
}

func (o *Options) tool() string {
	if o.Tool == "" {
		return "em-extract"
	}
	return o.Tool
}

// Summary of a run.
type Summary struct {
	Chunks     int
	Skipped    int
	Done       int
	Failed     int
	Blocks     uint64
	Elapsed    time.Duration
	OutBytes   int64
	RLPBytes   uint64
	DiskBytes  uint64
	ReadNs     int64
	DecodeNs   int64
	WriteNs    int64
	Failures   []string
	TableBytes map[string]int64
}

type chunkStats struct {
	decode.Stats
	ReadSec   float64 `json:"read_sec"`
	DecodeSec float64 `json:"decode_sec"`
	WriteSec  float64 `json:"write_sec"`
}

// Run executes the extraction. It returns an error only for setup problems;
// chunk failures are reported in the summary (and their manifests).
func Run(ctx context.Context, db Source, o Options) (*Summary, error) {
	if o.Workers <= 0 {
		o.Workers = runtime.NumCPU()
	}
	if o.Chunk == 0 {
		o.Chunk = 100000
	}
	if o.Batch == 0 {
		o.Batch = 512
	}
	if o.Log == nil {
		o.Log = os.Stderr
	}
	if o.To < o.From {
		return nil, fmt.Errorf("--to %d < --from %d", o.To, o.From)
	}
	if err := os.MkdirAll(o.Out, 0o755); err != nil {
		return nil, err
	}
	plan := chunk.Plan(o.From, o.To, o.Chunk)
	sum := &Summary{Chunks: len(plan), TableBytes: map[string]int64{}}
	start := time.Now()
	jobs := make(chan chunk.Range)
	var mu sync.Mutex
	var doneBlocks atomic.Uint64
	var wg sync.WaitGroup
	totalBlocks := o.To - o.From + 1

	stop := make(chan struct{})
	if !o.Quiet {
		go func() {
			t := time.NewTicker(15 * time.Second)
			defer t.Stop()
			for {
				select {
				case <-stop:
					return
				case <-t.C:
					el := time.Since(start).Seconds()
					b := doneBlocks.Load()
					rate := float64(b) / el
					eta := "?"
					if rate > 0 {
						eta = (time.Duration(float64(totalBlocks-b)/rate) * time.Second).String()
					}
					fmt.Fprintf(o.Log, "progress: %d/%d blocks (%.1f%%) %.0f blocks/s eta %s\n", b, totalBlocks, 100*float64(b)/float64(totalBlocks), rate, eta)
				}
			}
		}()
	}

	for w := 0; w < o.Workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for r := range jobs {
				if ctx.Err() != nil {
					continue
				}
				if !o.Force {
					m, err := chunk.Validate(o.Out, r, chunk.Expect{Tool: o.tool(), ChainID: o.ChainID, Statuses: []string{chunk.StatusOK}, RequireFiles: Tables, NeedVerify: o.Verify, CheckSHA: o.ResumeCheck != "size"})
					if err == nil {
						mu.Lock()
						sum.Skipped++
						sum.Blocks += r.Len()
						mu.Unlock()
						doneBlocks.Add(r.Len())
						if !o.Quiet {
							fmt.Fprintf(o.Log, "chunk %s: SKIP (valid manifest, %d blocks)\n", r.Name(), m.Counts[TBlocks])
						}
						continue
					} else if m != nil && !o.Quiet {
						fmt.Fprintf(o.Log, "chunk %s: redo (%v)\n", r.Name(), err)
					}
				}
				m, cs := processChunk(ctx, db, o, r)
				if o.ChunkDone != nil {
					o.ChunkDone(r, m)
				}
				if err := chunk.Write(o.Out, m); err != nil {
					m.Status = chunk.StatusFailed
					m.Failure = &chunk.Failure{Block: r.From, Reason: "MANIFEST_WRITE", Detail: err.Error()}
				}
				doneBlocks.Add(r.Len())
				mu.Lock()
				sum.ReadNs += int64(cs.ReadSec * 1e9)
				sum.DecodeNs += int64(cs.DecodeSec * 1e9)
				sum.WriteNs += int64(cs.WriteSec * 1e9)
				sum.RLPBytes += cs.RLPBytes
				sum.DiskBytes += cs.DiskBytes
				if m.Status == chunk.StatusOK {
					sum.Done++
					sum.Blocks += r.Len()
					for t, fi := range m.Files {
						sum.OutBytes += fi.Bytes
						sum.TableBytes[t] += fi.Bytes
					}
				} else {
					sum.Failed++
					sum.Failures = append(sum.Failures, fmt.Sprintf("%s: block %d %s %s", r.Name(), m.Failure.Block, m.Failure.Reason, m.Failure.Detail))
				}
				mu.Unlock()
				if !o.Quiet {
					if m.Status == chunk.StatusOK {
						fmt.Fprintf(o.Log, "chunk %s: OK blocks=%d txs=%d logs=%d transfers=%d %.0f blocks/s\n", r.Name(), m.Counts[TBlocks], m.Counts[TTxs], m.Counts[TLogs], m.Counts[TTransfers], m.BlocksPerS)
					} else {
						fmt.Fprintf(o.Log, "chunk %s: FAILED block %d %s: %s\n", r.Name(), m.Failure.Block, m.Failure.Reason, m.Failure.Detail)
					}
				}
			}
		}()
	}
	for _, r := range plan {
		select {
		case jobs <- r:
		case <-ctx.Done():
		}
		if ctx.Err() != nil {
			break
		}
	}
	close(jobs)
	wg.Wait()
	close(stop)
	sum.Elapsed = time.Since(start)
	sort.Strings(sum.Failures)
	if ctx.Err() != nil {
		return sum, ctx.Err()
	}
	return sum, nil
}

type writers struct {
	blocks    *pq.Writer[schema.Block]
	txs       *pq.Writer[schema.Tx]
	logs      *pq.Writer[schema.Log]
	transfers *pq.Writer[schema.TokenTransfer]
	creations *pq.Writer[schema.ContractCreation]
}

func openWriters(root string, r chunk.Range, meta map[string]string) (*writers, error) {
	w := &writers{}
	var err error
	if w.blocks, err = pq.Create[schema.Block](root, chunk.FileRel(TBlocks, r), 1<<17, meta); err != nil {
		return nil, err
	}
	if w.txs, err = pq.Create[schema.Tx](root, chunk.FileRel(TTxs, r), 1<<18, meta); err != nil {
		w.abort()
		return nil, err
	}
	if w.logs, err = pq.Create[schema.Log](root, chunk.FileRel(TLogs, r), 1<<17, meta); err != nil {
		w.abort()
		return nil, err
	}
	if w.transfers, err = pq.Create[schema.TokenTransfer](root, chunk.FileRel(TTransfers, r), 1<<18, meta); err != nil {
		w.abort()
		return nil, err
	}
	if w.creations, err = pq.Create[schema.ContractCreation](root, chunk.FileRel(TCreations, r), 1<<18, meta); err != nil {
		w.abort()
		return nil, err
	}
	return w, nil
}

func (w *writers) abort() {
	if w.blocks != nil {
		w.blocks.Abort()
	}
	if w.txs != nil {
		w.txs.Abort()
	}
	if w.logs != nil {
		w.logs.Abort()
	}
	if w.transfers != nil {
		w.transfers.Abort()
	}
	if w.creations != nil {
		w.creations.Abort()
	}
}

func (w *writers) write(rows *decode.Rows) error {
	return errors.Join(w.blocks.Write(rows.Blocks), w.txs.Write(rows.Txs), w.logs.Write(rows.Logs), w.transfers.Write(rows.Transfers), w.creations.Write(rows.Creations))
}

func (w *writers) commit() (map[string]pq.FileInfo, error) {
	files := map[string]pq.FileInfo{}
	type c struct {
		name string
		fn   func() (pq.FileInfo, error)
	}
	for _, x := range []c{{TBlocks, w.blocks.Commit}, {TTxs, w.txs.Commit}, {TLogs, w.logs.Commit}, {TTransfers, w.transfers.Commit}, {TCreations, w.creations.Commit}} {
		fi, err := x.fn()
		if err != nil {
			w.abort()
			return nil, fmt.Errorf("%s: %w", x.name, err)
		}
		files[x.name] = fi
	}
	return files, nil
}

// seedClassicL1 mirrors Nitro's RPC walk-back (<=50 blocks) for a chunk that
// starts on Classic blocks without transactions.
func seedClassicL1(db Source, from uint64) *uint64 {
	for i := uint64(1); i <= 50 && i <= from; i++ {
		raws, _, err := db.ReadRange(from-i, 1)
		if err != nil || len(raws) != 1 {
			return nil
		}
		var body types.Body
		if rlp.DecodeBytes(raws[0].Body, &body) != nil {
			return nil
		}
		if l1, ok := decode.ClassicL1(body.Transactions); ok {
			return &l1
		}
	}
	return nil
}

func processChunk(ctx context.Context, db Source, o Options, r chunk.Range) (*chunk.Manifest, *chunkStats) {
	t0 := time.Now()
	cs := &chunkStats{}
	m := &chunk.Manifest{Tool: o.tool(), Version: Version, Dependency: Dependency, ChainID: o.ChainID, Range: r, Counts: map[string]uint64{}, Files: map[string]pq.FileInfo{}}
	checks := []string{"header_number"}
	if o.Verify {
		checks = append(checks, "header_hash==canonical_hash", "parent_hash_continuity", "tx_root", "uncle_hash", "receipt_root", "receipt_count")
		if o.VerifyBloom {
			checks = append(checks, "log_bloom")
		}
	}
	vi := &chunk.VerifyInfo{Enabled: o.Verify, Checks: checks, PrevLink: "n/a"}
	m.Verify = vi
	finish := func(status string, f *chunk.Failure) (*chunk.Manifest, *chunkStats) {
		m.Status = status
		m.Failure = f
		el := time.Since(t0).Seconds()
		m.ElapsedSec = el
		if el > 0 {
			m.BlocksPerS = float64(cs.Blocks) / el
		}
		m.Stats = cs
		return m, cs
	}
	chunk.RemoveFiles(o.Out, Tables, r)
	meta := map[string]string{"em.chain_id": fmt.Sprint(o.ChainID), "em.range": r.Name(), "em.dependency": Dependency, "em.amount_encoding": "uint256 big-endian FIXED_LEN_BYTE_ARRAY(32)"}
	w, err := openWriters(o.Out, r, meta)
	if err != nil {
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.From, Reason: "OUTPUT", Detail: err.Error()})
	}
	d := decode.New(decode.Config{ChainID: o.ChainID, NitroGenesis: o.NitroGenesis, Verify: o.Verify, VerifyBloom: o.VerifyBloom, WETH: o.WETH, SenderErrorZero: o.SenderZero, Family: o.Family})
	if r.From < o.NitroGenesis && r.From > 0 {
		d.LastClassicL1 = seedClassicL1(db, r.From)
	}
	var prev *common.Hash
	if o.Verify && r.From > 0 {
		if h := db.CanonicalHash(r.From - 1); h != (common.Hash{}) {
			prev = &h
			vi.PrevLink = "verified"
		} else {
			vi.PrevLink = "unavailable"
			m.Warnings = append(m.Warnings, fmt.Sprintf("canonical hash of block %d unavailable; first block's parent link not checked", r.From-1))
		}
	}
	var rows decode.Rows
	var firstHash, firstParent, lastHash common.Hash
	failAt := func(be *chunk.Failure) (*chunk.Manifest, *chunkStats) {
		w.abort()
		chunk.RemoveFiles(o.Out, Tables, r)
		m.Files = map[string]pq.FileInfo{}
		return finish(chunk.StatusFailed, be)
	}
	for n := r.From; n <= r.To; {
		if ctx.Err() != nil {
			return failAt(&chunk.Failure{Block: n, Reason: "CANCELLED", Detail: ctx.Err().Error()})
		}
		cnt := o.Batch
		if rem := r.To - n + 1; rem < cnt {
			cnt = rem
		}
		tr := time.Now()
		raws, diskBytes, err := db.ReadRange(n, cnt)
		cs.ReadSec += time.Since(tr).Seconds()
		if err != nil {
			return failAt(&chunk.Failure{Block: n, Reason: decode.ReasonMissing, Detail: err.Error()})
		}
		cs.DiskBytes += diskBytes
		td := time.Now()
		for i := range raws {
			nb := len(rows.Blocks)
			h, err := d.Block(&raws[i], prev, &rows, &cs.Stats)
			if err != nil {
				var be *decode.BlockError
				if errors.As(err, &be) {
					return failAt(&chunk.Failure{Block: be.Block, Reason: be.Reason, Detail: be.Detail})
				}
				return failAt(&chunk.Failure{Block: raws[i].Number, Reason: "DECODE", Detail: err.Error()})
			}
			hh := h
			prev = &hh
			if raws[i].Number == r.From {
				firstHash, firstParent = h, rows.Blocks[nb].ParentHash
			}
			lastHash = h
		}
		cs.DecodeSec += time.Since(td).Seconds()
		tw := time.Now()
		m.Counts[TBlocks] += uint64(len(rows.Blocks))
		m.Counts[TTxs] += uint64(len(rows.Txs))
		m.Counts[TLogs] += uint64(len(rows.Logs))
		m.Counts[TTransfers] += uint64(len(rows.Transfers))
		m.Counts[TCreations] += uint64(len(rows.Creations))
		if err := w.write(&rows); err != nil {
			return failAt(&chunk.Failure{Block: n, Reason: "OUTPUT", Detail: err.Error()})
		}
		cs.WriteSec += time.Since(tw).Seconds()
		rows.Reset()
		n += cnt
	}
	if o.Verify {
		vi.BlocksVerified = cs.Blocks
	}
	tw := time.Now()
	files, err := w.commit()
	cs.WriteSec += time.Since(tw).Seconds()
	if err != nil {
		chunk.RemoveFiles(o.Out, Tables, r)
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.To, Reason: "OUTPUT", Detail: err.Error()})
	}
	for t, fi := range files {
		if fi.Rows != m.Counts[t] {
			chunk.RemoveFiles(o.Out, Tables, r)
			return finish(chunk.StatusFailed, &chunk.Failure{Block: r.To, Reason: "OUTPUT", Detail: fmt.Sprintf("%s rows %d != counted %d", t, fi.Rows, m.Counts[t])})
		}
	}
	if cs.Blocks != r.Len() {
		chunk.RemoveFiles(o.Out, Tables, r)
		return finish(chunk.StatusFailed, &chunk.Failure{Block: r.To, Reason: decode.ReasonMissing, Detail: fmt.Sprintf("decoded %d blocks, expected %d", cs.Blocks, r.Len())})
	}
	m.Files = files
	if wr, ok := db.(warner); ok {
		m.Warnings = append(m.Warnings, wr.SourceWarnings()...)
	}
	// Recorded so chunk-to-chunk continuity can be re-checked offline.
	if cs.SenderErrors > 0 {
		m.Warnings = append(m.Warnings, fmt.Sprintf("%d transactions with unrecoverable sender recorded as 0x0 (see stats.sender_error_txs)", cs.SenderErrors))
	}
	m.Extra = map[string]any{"first_block_hash": firstHash.Hex(), "first_parent_hash": firstParent.Hex(), "last_block_hash": lastHash.Hex()}
	return finish(chunk.StatusOK, nil)
}
