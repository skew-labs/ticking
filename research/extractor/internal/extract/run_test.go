package extract

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/parquet-go/parquet-go"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/schema"
	"github.com/em-mapping/extractor/internal/testchain"
)

const (
	nBlocks  = 300
	nFrozen  = 200
	nitroGen = 50
)

func buildDB(t *testing.T, spec testchain.Spec) (string, *testchain.Expect) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "nitro", "l2chaindata")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	exp, err := testchain.Build(dir, spec)
	if err != nil {
		t.Fatal(err)
	}
	return dir, exp
}

func openRO(t *testing.T, path string) *chaindb.DB {
	t.Helper()
	db, err := chaindb.Open(chaindb.Options{DataDir: path, CacheMB: 8, Handles: 64})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

func opts(out string) Options {
	return Options{ChainID: testchain.ChainID, NitroGenesis: nitroGen, From: 0, To: nBlocks - 1, Workers: 4, Chunk: 64, Batch: 17, Out: out,
		Verify: true, VerifyBloom: true, ResumeCheck: "sha256", WETH: &decode.WETHFilter{}, Log: io.Discard}
}

func readAll[T any](t *testing.T, out, table string) []T {
	t.Helper()
	files, _ := filepath.Glob(filepath.Join(out, table, "*.parquet"))
	var all []T
	for _, f := range files {
		rows, err := parquet.ReadFile[T](f)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		all = append(all, rows...)
	}
	return all
}

func TestExtractFreezerAndKVThenResume(t *testing.T) {
	dir, exp := buildDB(t, testchain.Spec{Blocks: nBlocks, Frozen: nFrozen, NitroGenesis: nitroGen})
	// Auto-detection from the snapshot root (two levels up).
	db := openRO(t, filepath.Dir(filepath.Dir(dir)))
	if db.Frozen != nFrozen {
		t.Fatalf("frozen=%d", db.Frozen)
	}
	if head, ok := db.Head(); !ok || head != nBlocks-1 {
		t.Fatalf("head=%d %v", head, ok)
	}
	if cfg := db.ChainConfig(); cfg == nil || cfg.ArbitrumChainParams.GenesisBlockNum != nitroGen {
		t.Fatalf("chain config %+v", cfg)
	}
	out := t.TempDir()
	s, err := Run(context.Background(), db, opts(out))
	if err != nil {
		t.Fatal(err)
	}
	if s.Failed != 0 || s.Done != 5 || s.Skipped != 0 {
		t.Fatalf("summary %+v", s)
	}
	blocks := readAll[schema.Block](t, out, TBlocks)
	txs := readAll[schema.Tx](t, out, TTxs)
	logs := readAll[schema.Log](t, out, TLogs)
	tts := readAll[schema.TokenTransfer](t, out, TTransfers)
	ccs := readAll[schema.ContractCreation](t, out, TCreations)
	if len(blocks) != nBlocks || uint64(len(txs)) != exp.Txs || uint64(len(logs)) != exp.Logs || uint64(len(tts)) != exp.Transfers || uint64(len(ccs)) != exp.Creations {
		t.Fatalf("counts blocks=%d txs=%d/%d logs=%d/%d transfers=%d/%d creations=%d/%d", len(blocks), len(txs), exp.Txs, len(logs), exp.Logs, len(tts), exp.Transfers, len(ccs), exp.Creations)
	}
	for _, b := range blocks {
		if common.Hash(b.Hash) != exp.Hashes[b.Number] {
			t.Fatalf("block %d hash", b.Number)
		}
		if b.IsClassic != (b.Number < nitroGen) {
			t.Fatalf("block %d classic flag", b.Number)
		}
	}
	senders := map[uint8]map[common.Address]int{}
	for _, tx := range txs {
		if senders[tx.Type] == nil {
			senders[tx.Type] = map[common.Address]int{}
		}
		senders[tx.Type][common.Address(tx.From)]++
	}
	if senders[106][common.HexToAddress("0x00000000000000000000000000000000000A4B05")] == 0 {
		t.Errorf("internal tx sender not ArbOS: %v", senders[106])
	}
	if senders[100][common.HexToAddress("0x1111000000000000000000000000000000001111")] == 0 {
		t.Errorf("deposit sender: %v", senders[100])
	}
	if senders[120][common.HexToAddress("0x3333333333333333333333333333333333333333")] == 0 || senders[120][exp.Creator] == 0 {
		t.Errorf("classic senders: %v", senders[120])
	}
	for _, c := range ccs {
		if common.Address(c.Creator) != exp.Creator {
			t.Errorf("creator")
		}
	}

	// Second run: everything valid -> all skipped.
	s, err = Run(context.Background(), db, opts(out))
	if err != nil || s.Skipped != 5 || s.Done != 0 {
		t.Fatalf("resume: %+v %v", s, err)
	}
	// Tamper one file (same size) and delete another chunk's manifest -> exactly those two redone.
	r1 := chunk.Range{From: 64, To: 127}
	p := filepath.Join(out, chunk.FileRel(TLogs, r1))
	b, _ := os.ReadFile(p)
	b[len(b)/2] ^= 0xff
	os.WriteFile(p, b, 0o644)
	r2 := chunk.Range{From: 256, To: 299}
	os.Remove(chunk.Path(out, r2))
	s, err = Run(context.Background(), db, opts(out))
	if err != nil || s.Skipped != 3 || s.Done != 2 {
		t.Fatalf("partial resume: %+v %v", s, err)
	}
	if _, err := chunk.Validate(out, r1, chunk.Expect{Tool: "em-extract", ChainID: testchain.ChainID, Statuses: []string{chunk.StatusOK}, RequireFiles: Tables, CheckSHA: true}); err != nil {
		t.Fatal(err)
	}
	// A manifest produced without --verify does not satisfy a verifying run.
	o := opts(out)
	o.Verify = false
	o.Force = true
	o.From, o.To = 0, 63
	if s, _ := Run(context.Background(), db, o); s.Done != 1 {
		t.Fatalf("noverify run %+v", s)
	}
	o = opts(out)
	o.From, o.To = 0, 63
	if s, _ := Run(context.Background(), db, o); s.Done != 1 || s.Skipped != 0 {
		t.Fatalf("verify after noverify should redo: %+v", s)
	}
}

func TestExtractReceiptRootMismatchFailsChunk(t *testing.T) {
	dir, _ := buildDB(t, testchain.Spec{Blocks: nBlocks, Frozen: nFrozen, NitroGenesis: nitroGen, CorruptReceiptAt: 250})
	db := openRO(t, dir)
	out := t.TempDir()
	s, err := Run(context.Background(), db, opts(out))
	if err != nil {
		t.Fatal(err)
	}
	if s.Failed != 1 || s.Done != 4 {
		t.Fatalf("summary %+v", s)
	}
	bad := chunk.Range{From: 192, To: 255}
	m, err := chunk.Read(out, bad)
	if err != nil || m.Status != chunk.StatusFailed || m.Failure == nil || m.Failure.Block != 250 || m.Failure.Reason != decode.ReasonReceiptRoot {
		t.Fatalf("manifest %+v %v", m, err)
	}
	for _, tb := range Tables {
		if _, err := os.Stat(filepath.Join(out, chunk.FileRel(tb, bad))); !os.IsNotExist(err) {
			t.Fatalf("%s output left behind for failed chunk", tb)
		}
	}
	// Resume retries the failed chunk (and fails again), never skips it.
	s, _ = Run(context.Background(), db, opts(out))
	if s.Failed != 1 || s.Skipped != 4 {
		t.Fatalf("resume after failure %+v", s)
	}
	// Without verification the same data extracts (documented trade-off).
	o := opts(out)
	o.Verify = false
	o.From, o.To = 192, 255
	if s, _ := Run(context.Background(), db, o); s.Done != 1 {
		t.Fatalf("noverify %+v", s)
	}
}

func TestPlanAligned(t *testing.T) {
	got := chunk.Plan(50, 250, 100)
	want := []chunk.Range{{From: 50, To: 99}, {From: 100, To: 199}, {From: 200, To: 250}}
	if len(got) != len(want) {
		t.Fatal(got)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatal(got)
		}
	}
}
