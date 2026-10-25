package rpcsrc_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/parquet-go/parquet-go"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/extract"
	"github.com/em-mapping/extractor/internal/rpcsrc"
	"github.com/em-mapping/extractor/internal/schema"
	"github.com/em-mapping/extractor/internal/testchain"
)

var fixtureBlocks = []uint64{1000000, 22204650, 22207817, 22207818, 22209702, 515626437}

func fixtureDir() string { return filepath.Join("..", "..", "testdata", "rpc") }

func readResult(t *testing.T, path string) json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var env struct {
		Result json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatal(err)
	}
	return env.Result
}

// The production converter reproduces the official arb1 hashes and roots for
// every fixture (Classic 120, Nitro genesis, retryables 104/105/106, recent).
func TestConvertFixtures(t *testing.T) {
	signer := types.LatestSignerForChainID(big.NewInt(42161))
	for _, n := range fixtureBlocks {
		bj := readResult(t, filepath.Join(fixtureDir(), fmt.Sprintf("block_%d.json", n)))
		rj := readResult(t, filepath.Join(fixtureDir(), fmt.Sprintf("receipts_%d.json", n)))
		raw, err := rpcsrc.Convert(signer, n, bj, rj)
		if err != nil {
			t.Fatalf("block %d: %v", n, err)
		}
		var hdr types.Header
		rlp.DecodeBytes(raw.Header, &hdr)
		d := decode.New(decode.Config{ChainID: 42161, NitroGenesis: 22207817, Verify: true, VerifyBloom: true, WETH: &decode.WETHFilter{Any: true}})
		var rows decode.Rows
		var st decode.Stats
		parent := hdr.ParentHash
		h, err := d.Block(raw, &parent, &rows, &st)
		if err != nil {
			t.Fatalf("block %d verify: %v", n, err)
		}
		if h != raw.Hash || crypto.Keccak256Hash(raw.Header) != raw.Hash {
			t.Fatalf("block %d hash", n)
		}
		t.Logf("block %d ok: txs=%d types=%v", n, len(rows.Txs), st.TxTypes)
	}
	// Corrupted JSON is caught by the decoder (receipt root) or the converter.
	bj := readResult(t, filepath.Join(fixtureDir(), "block_22209702.json"))
	rj := readResult(t, filepath.Join(fixtureDir(), "receipts_22209702.json"))
	bad := strings.Replace(string(rj), `"status":"0x1"`, `"status":"0x0"`, 1)
	raw, err := rpcsrc.Convert(signer, 22209702, bj, []byte(bad))
	if err != nil {
		t.Fatal(err)
	}
	d := decode.New(decode.Config{ChainID: 42161, NitroGenesis: 22207817, Verify: true, VerifyBloom: true})
	var rows decode.Rows
	var st decode.Stats
	if _, err := d.Block(raw, nil, &rows, &st); err == nil || !strings.Contains(err.Error(), decode.ReasonReceiptRoot) {
		t.Fatalf("tampered status not detected: %v", err)
	}
	if _, err := rpcsrc.Convert(signer, 22209703, bj, rj); err == nil {
		t.Fatal("wrong block number accepted")
	}
	if _, err := rpcsrc.Convert(signer, 22209702, []byte("null"), rj); err == nil || !strings.Contains(err.Error(), rpcsrc.ReasonRPCNotFound) {
		t.Fatalf("null block: %v", err)
	}
}

// ---- fake Nitro RPC over a chain DB ----------------------------------------

type fakeNode struct {
	db      *chaindb.DB
	signer  types.Signer
	reqs    atomic.Int64
	calls   atomic.Int64
	limited atomic.Int64
	// fault injection
	every429   int64 // every Nth request -> 429 + Retry-After
	maxBatch   int   // batches with more calls are rejected (drpc style)
	nullEvery  int64 // every Nth call returns null
	itemRate   int64 // every Nth request: per-item -32005 errors
	faultMax   int64 // stop injecting 429 / item errors after this many (0 = never stop)
	nullCalls  atomic.Int64
	itemCalls  atomic.Int64
	itemHits   atomic.Int64
	headNumber uint64
}

func (f *fakeNode) blockJSON(n uint64) (map[string]any, []map[string]any, error) {
	raws, _, err := f.db.ReadRange(n, 1)
	if err != nil {
		return nil, nil, err
	}
	raw := raws[0]
	var h types.Header
	if err := rlp.DecodeBytes(raw.Header, &h); err != nil {
		return nil, nil, err
	}
	var body types.Body
	if err := rlp.DecodeBytes(raw.Body, &body); err != nil {
		return nil, nil, err
	}
	var stored []*types.ReceiptForStorage
	if err := rlp.DecodeBytes(raw.Receipts, &stored); err != nil {
		return nil, nil, err
	}
	hj, _ := json.Marshal(&h)
	var blk map[string]any
	json.Unmarshal(hj, &blk)
	blk["hash"] = raw.Hash
	blk["uncles"] = []any{}
	var txs []any
	var rcs []map[string]any
	var prevCum uint64
	logIndex := 0
	for i, tx := range body.Transactions {
		tj, _ := tx.MarshalJSON()
		var m map[string]any
		json.Unmarshal(tj, &m)
		from, _ := types.Sender(f.signer, tx)
		m["from"] = from
		m["hash"] = tx.Hash()
		m["blockHash"] = raw.Hash
		m["blockNumber"] = hexutil.Uint64(n)
		m["transactionIndex"] = hexutil.Uint64(i)
		delete(m, "effectiveGasPrice")
		delete(m, "l1BlockNumber")
		txs = append(txs, m)
		r := (*types.Receipt)(stored[i])
		rc := map[string]any{
			"blockHash": raw.Hash, "blockNumber": hexutil.Uint64(n), "transactionHash": tx.Hash(), "transactionIndex": hexutil.Uint64(i),
			"from": from, "to": tx.To(), "type": hexutil.Uint64(tx.Type()), "status": hexutil.Uint64(r.Status),
			"cumulativeGasUsed": hexutil.Uint64(r.CumulativeGasUsed), "gasUsedForL1": hexutil.Uint64(r.GasUsedForL1), "contractAddress": nil,
		}
		gas := r.CumulativeGasUsed - prevCum
		if lt, ok := tx.GetInner().(*types.ArbitrumLegacyTxData); ok {
			gas = r.GasUsed
			rc["effectiveGasPrice"] = hexutil.Uint64(lt.EffectiveGasPrice)
			rc["l1BlockNumber"] = hexutil.Uint64(lt.L1BlockNumber)
		} else {
			rc["effectiveGasPrice"] = (*hexutil.Big)(h.BaseFee)
		}
		prevCum = r.CumulativeGasUsed
		rc["gasUsed"] = hexutil.Uint64(gas)
		ca := r.ContractAddress
		if tx.To() == nil && ca == (common.Address{}) {
			ca = crypto.CreateAddress(from, tx.Nonce())
		}
		if ca != (common.Address{}) {
			rc["contractAddress"] = ca
		}
		var logs []any
		for _, l := range r.Logs {
			logs = append(logs, map[string]any{"address": l.Address, "topics": l.Topics, "data": hexutil.Bytes(l.Data), "logIndex": hexutil.Uint64(logIndex), "transactionIndex": hexutil.Uint64(i), "blockHash": raw.Hash})
			logIndex++
		}
		if logs == nil {
			logs = []any{}
		}
		rc["logs"] = logs
		rcs = append(rcs, rc)
	}
	if txs == nil {
		txs = []any{}
	}
	blk["transactions"] = txs
	if rcs == nil {
		rcs = []map[string]any{}
	}
	return blk, rcs, nil
}

type req struct {
	ID     json.RawMessage   `json:"id"`
	Method string            `json:"method"`
	Params []json.RawMessage `json:"params"`
}

func (f *fakeNode) one(r req) map[string]any {
	resp := map[string]any{"jsonrpc": "2.0", "id": r.ID}
	if f.nullEvery > 0 && f.nullCalls.Add(1)%f.nullEvery == 0 && (r.Method == "eth_getBlockReceipts" || r.Method == "eth_getBlockByNumber") {
		resp["result"] = nil
		return resp
	}
	switch r.Method {
	case "eth_chainId":
		resp["result"] = hexutil.Uint64(testchain.ChainID)
	case "eth_blockNumber":
		resp["result"] = hexutil.Uint64(f.headNumber)
	case "eth_getBlockByNumber", "eth_getBlockReceipts":
		var hs string
		json.Unmarshal(r.Params[0], &hs)
		n, err := hexutil.DecodeUint64(hs)
		if err != nil || n > f.headNumber {
			resp["result"] = nil
			return resp
		}
		blk, rcs, err := f.blockJSON(n)
		if err != nil {
			resp["error"] = map[string]any{"code": -32000, "message": err.Error()}
			return resp
		}
		if r.Method == "eth_getBlockReceipts" {
			resp["result"] = rcs
		} else {
			full := true
			if len(r.Params) > 1 {
				json.Unmarshal(r.Params[1], &full)
			}
			if !full {
				delete(blk, "transactions")
			}
			resp["result"] = blk
		}
	default:
		resp["error"] = map[string]any{"code": -32601, "message": "method not found"}
	}
	return resp
}

func (f *fakeNode) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	k := f.reqs.Add(1)
	if f.every429 > 0 && k%f.every429 == 0 && (f.faultMax == 0 || f.limited.Load() < f.faultMax) {
		f.limited.Add(1)
		w.Header().Set("Retry-After", "1")
		w.WriteHeader(http.StatusTooManyRequests)
		w.Write([]byte(`{"jsonrpc":"2.0","error":{"code":429,"message":"Too Many Requests"}}`))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if len(body) > 0 && body[0] == '[' {
		var reqs []req
		if err := json.Unmarshal(body, &reqs); err != nil {
			w.WriteHeader(400)
			return
		}
		f.calls.Add(int64(len(reqs)))
		if f.maxBatch > 0 && len(reqs) > f.maxBatch {
			w.WriteHeader(500)
			var out []any
			for _, q := range reqs {
				out = append(out, map[string]any{"jsonrpc": "2.0", "id": q.ID, "error": map[string]any{"code": -32000, "message": fmt.Sprintf("Batch of more than %d requests are not allowed on free plan", f.maxBatch)}})
			}
			json.NewEncoder(w).Encode(out)
			return
		}
		rateItems := f.itemRate > 0 && f.itemCalls.Add(1)%f.itemRate == 0 && (f.faultMax == 0 || f.itemHits.Add(1) <= f.faultMax)
		var out []any
		for i, q := range reqs {
			if rateItems && i%2 == 1 {
				out = append(out, map[string]any{"jsonrpc": "2.0", "id": q.ID, "error": map[string]any{"code": -32005, "message": "Rate limit exceeded"}})
				continue
			}
			out = append(out, f.one(q))
		}
		json.NewEncoder(w).Encode(out)
		return
	}
	var q req
	if err := json.Unmarshal(body, &q); err != nil {
		w.WriteHeader(400)
		return
	}
	f.calls.Add(1)
	json.NewEncoder(w).Encode(f.one(q))
}

const (
	nBlocks  = 300
	nFrozen  = 200
	nitroGen = 50
)

func buildDB(t *testing.T) (*chaindb.DB, *testchain.Expect) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "nitro", "l2chaindata")
	os.MkdirAll(dir, 0o755)
	exp, err := testchain.Build(dir, testchain.Spec{Blocks: nBlocks, Frozen: nFrozen, NitroGenesis: nitroGen})
	if err != nil {
		t.Fatal(err)
	}
	db, err := chaindb.Open(chaindb.Options{DataDir: dir, CacheMB: 8, Handles: 64})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db, exp
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

func baseOpts(out string) extract.Options {
	return extract.Options{ChainID: testchain.ChainID, NitroGenesis: nitroGen, From: 0, To: nBlocks - 1, Workers: 3, Chunk: 64, Batch: 17, Out: out,
		Verify: true, VerifyBloom: true, ResumeCheck: "sha256", WETH: &decode.WETHFilter{}, Log: io.Discard}
}

// em-rpc-extract output over a (faulty) RPC equals em-extract output read
// from the same database: same rows in every table, byte-identical Parquet
// files and interchangeable manifests.
func TestRPCMatchesDatabaseExtraction(t *testing.T) {
	db, exp := buildDB(t)
	signer := types.LatestSignerForChainID(big.NewInt(testchain.ChainID))
	good := &fakeNode{db: db, signer: signer, headNumber: nBlocks - 1}
	flaky := &fakeNode{db: db, signer: signer, headNumber: nBlocks - 1, every429: 5, maxBatch: 6, nullEvery: 23, itemRate: 4, faultMax: 10}
	s1 := httptest.NewServer(good)
	defer s1.Close()
	s2 := httptest.NewServer(flaky)
	defer s2.Close()

	outDB := filepath.Join(t.TempDir(), "db")
	sumDB, err := extract.Run(context.Background(), db, baseOpts(outDB))
	if err != nil || sumDB.Failed != 0 {
		t.Fatalf("db run: %v %+v", err, sumDB)
	}

	ctx := context.Background()
	e1, _ := rpcsrc.ParseEndpoint(s1.URL+"#batch=3&workers=2", 10, 4)
	e2, _ := rpcsrc.ParseEndpoint(s2.URL+"#batch=8&workers=3", 10, 4)
	pool, err := rpcsrc.NewPool(ctx, []rpcsrc.EndpointSpec{e1, e2}, rpcsrc.PoolOptions{ChainID: testchain.ChainID, Retries: 6, HTTPTimeout: 10 * time.Second, MaxBackoff: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if err := pool.CheckChainID(ctx, testchain.ChainID); err != nil {
		t.Fatal(err)
	}
	if h, _, err := pool.Head(ctx); err != nil || h != nBlocks-1 {
		t.Fatalf("head %d %v", h, err)
	}
	outRPC := filepath.Join(t.TempDir(), "rpc")
	src := rpcsrc.NewSource(ctx, pool, outRPC)
	o := baseOpts(outRPC)
	sumRPC, err := extract.Run(ctx, src, o)
	if err != nil || sumRPC.Failed != 0 {
		t.Fatalf("rpc run: %v %+v", err, sumRPC)
	}
	if flaky.limited.Load() == 0 || flaky.nullCalls.Load() == 0 {
		t.Fatalf("fault injection did not trigger")
	}
	st := pool.Stats()
	if st[1].Batch >= 8 || st[1].RateLimited == 0 {
		t.Fatalf("flaky endpoint: batch not reduced or no rate limiting seen: %+v", st[1])
	}
	t.Logf("good: %+v", st[0])
	t.Logf("flaky: %+v", st[1])

	if got := readAll[schema.Tx](t, outRPC, extract.TTxs); uint64(len(got)) != exp.Txs {
		t.Fatalf("txs %d want %d", len(got), exp.Txs)
	}
	cmp := func(name string, a, b any) {
		if !reflect.DeepEqual(a, b) {
			t.Fatalf("%s rows differ between db and rpc extraction", name)
		}
	}
	cmp("blocks", readAll[schema.Block](t, outDB, extract.TBlocks), readAll[schema.Block](t, outRPC, extract.TBlocks))
	cmp("txs", readAll[schema.Tx](t, outDB, extract.TTxs), readAll[schema.Tx](t, outRPC, extract.TTxs))
	cmp("logs", readAll[schema.Log](t, outDB, extract.TLogs), readAll[schema.Log](t, outRPC, extract.TLogs))
	cmp("transfers", readAll[schema.TokenTransfer](t, outDB, extract.TTransfers), readAll[schema.TokenTransfer](t, outRPC, extract.TTransfers))
	cmp("creations", readAll[schema.ContractCreation](t, outDB, extract.TCreations), readAll[schema.ContractCreation](t, outRPC, extract.TCreations))

	// Byte-identical Parquet and interchangeable manifests: em-extract's
	// resume logic accepts the RPC chunks as done.
	for _, r := range chunk.Plan(0, nBlocks-1, 64) {
		ma, err := chunk.Read(outDB, r)
		if err != nil {
			t.Fatal(err)
		}
		mb, err := chunk.Read(outRPC, r)
		if err != nil {
			t.Fatal(err)
		}
		for tbl, fa := range ma.Files {
			if fb := mb.Files[tbl]; fb.SHA256 != fa.SHA256 || fb.Rows != fa.Rows {
				t.Fatalf("chunk %s table %s: db %s/%d rpc %s/%d", r.Name(), tbl, fa.SHA256, fa.Rows, fb.SHA256, fb.Rows)
			}
		}
		if ma.Extra["last_block_hash"] != mb.Extra["last_block_hash"] || ma.Extra["first_parent_hash"] != mb.Extra["first_parent_hash"] {
			t.Fatalf("chunk %s extra differs", r.Name())
		}
		if mb.Verify == nil || !mb.Verify.Enabled || mb.Verify.BlocksVerified != r.Len() {
			t.Fatalf("chunk %s not verified: %+v", r.Name(), mb.Verify)
		}
	}
	again, err := extract.Run(ctx, db, baseOpts(outRPC))
	if err != nil || again.Skipped != again.Chunks {
		t.Fatalf("em-extract resume over rpc output: %+v %v", again, err)
	}
}

// A tampered endpoint (receipt status flipped) never produces rows: the
// chunk fails with RECEIPT_ROOT after retries are exhausted.
func TestRPCTamperedEndpointFailsChunk(t *testing.T) {
	db, _ := buildDB(t)
	signer := types.LatestSignerForChainID(big.NewInt(testchain.ChainID))
	node := &fakeNode{db: db, signer: signer, headNumber: nBlocks - 1}
	tamper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := httptest.NewRecorder()
		node.ServeHTTP(rec, r)
		out := strings.ReplaceAll(rec.Body.String(), `"cumulativeGasUsed":"0xc350"`, `"cumulativeGasUsed":"0xc351"`)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(rec.Code)
		w.Write([]byte(out))
	})
	srv := httptest.NewServer(tamper)
	defer srv.Close()
	ctx := context.Background()
	e, _ := rpcsrc.ParseEndpoint(srv.URL, 4, 2)
	pool, _ := rpcsrc.NewPool(ctx, []rpcsrc.EndpointSpec{e}, rpcsrc.PoolOptions{ChainID: testchain.ChainID, Retries: 2})
	defer pool.Close()
	out := t.TempDir()
	o := baseOpts(out)
	o.From, o.To = 60, 127
	sum, err := extract.Run(ctx, rpcsrc.NewSource(ctx, pool, out), o)
	if err != nil {
		t.Fatal(err)
	}
	if sum.Failed == 0 {
		t.Fatalf("tampered receipts accepted: %+v", sum)
	}
	for _, f := range sum.Failures {
		if !strings.Contains(f, decode.ReasonReceiptRoot) && !strings.Contains(f, decode.ReasonGas) {
			t.Fatalf("unexpected failure %s", f)
		}
	}
	files, _ := filepath.Glob(filepath.Join(out, "*", "*.parquet"))
	var leftover []string
	for _, f := range files {
		m, err := chunk.Read(out, rangeOf(f))
		if err != nil || m.Status != chunk.StatusOK {
			leftover = append(leftover, f)
		}
	}
	if len(leftover) > 0 {
		t.Fatalf("files of failed chunks left: %v", leftover)
	}
}

func rangeOf(path string) chunk.Range {
	base := strings.TrimSuffix(filepath.Base(path), ".parquet")
	i := strings.LastIndex(base, "_")
	var a, b uint64
	fmt.Sscanf(base[i+1:], "%d-%d", &a, &b)
	return chunk.Range{From: a, To: b}
}

// Retry-After is honoured: with a single endpoint that rate-limits every
// other request, all blocks still arrive and the pause is respected.
func TestRetryAfterHonoured(t *testing.T) {
	db, _ := buildDB(t)
	signer := types.LatestSignerForChainID(big.NewInt(testchain.ChainID))
	node := &fakeNode{db: db, signer: signer, headNumber: nBlocks - 1, every429: 2}
	var mu sync.Mutex
	var times []time.Time
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		times = append(times, time.Now())
		mu.Unlock()
		node.ServeHTTP(w, r)
	})
	srv := httptest.NewServer(h)
	defer srv.Close()
	ctx := context.Background()
	e, _ := rpcsrc.ParseEndpoint(srv.URL, 5, 1)
	pool, _ := rpcsrc.NewPool(ctx, []rpcsrc.EndpointSpec{e}, rpcsrc.PoolOptions{ChainID: testchain.ChainID})
	defer pool.Close()
	t0 := time.Now()
	raws, _, err := pool.Fetch(ctx, 100, 20)
	if err != nil {
		t.Fatal(err)
	}
	for i, r := range raws {
		if r.Number != 100+uint64(i) || len(r.Header) == 0 {
			t.Fatalf("slot %d: %+v", i, r.Number)
		}
	}
	// 4 batches needed; each success is followed by a 429 + 1 s pause.
	if el := time.Since(t0); el < 2*time.Second {
		t.Fatalf("Retry-After not honoured: %s", el)
	}
	if node.limited.Load() == 0 {
		t.Fatal("no 429 served")
	}
}

func TestParseEndpoint(t *testing.T) {
	e, err := rpcsrc.ParseEndpoint("https://x.example/rpc/KEY#batch=1&workers=24&rps=5", 10, 8)
	if err != nil || e.Batch != 1 || e.Workers != 24 || e.RPS != 5 || e.URL != "https://x.example/rpc/KEY" {
		t.Fatalf("%+v %v", e, err)
	}
	if rpcsrc.Redact(e.URL) != "https://x.example" {
		t.Fatal(rpcsrc.Redact(e.URL))
	}
	if _, err := rpcsrc.ParseEndpoint("ftp://x", 1, 1); err == nil {
		t.Fatal("bad scheme accepted")
	}
	if _, err := rpcsrc.ParseEndpoint("http://x#foo=1", 1, 1); err == nil {
		t.Fatal("unknown option accepted")
	}
}

// Rebuilt raw blocks are byte-identical to the database (header, body and
// storage receipts RLP), even through an endpoint that limits batch size,
// returns nulls and per-item rate-limit errors.
func TestFetchBytesEqualDatabase(t *testing.T) {
	db, _ := buildDB(t)
	signer := types.LatestSignerForChainID(big.NewInt(testchain.ChainID))
	node := &fakeNode{db: db, signer: signer, headNumber: nBlocks - 1, maxBatch: 6, nullEvery: 41, itemRate: 5, faultMax: 6}
	srv := httptest.NewServer(node)
	defer srv.Close()
	ctx := context.Background()
	e, _ := rpcsrc.ParseEndpoint(srv.URL+"#batch=10&workers=4", 10, 4)
	pool, _ := rpcsrc.NewPool(ctx, []rpcsrc.EndpointSpec{e}, rpcsrc.PoolOptions{ChainID: testchain.ChainID, RateRetries: 1000})
	defer pool.Close()
	got, _, err := pool.Fetch(ctx, 0, nBlocks)
	if err != nil {
		t.Fatal(err)
	}
	want, _, err := db.ReadRange(0, nBlocks)
	if err != nil {
		t.Fatal(err)
	}
	for i := range want {
		a, b := want[i], got[i]
		if a.Number != b.Number || a.Hash != b.Hash || string(a.Header) != string(b.Header) || string(a.Body) != string(b.Body) || string(a.Receipts) != string(b.Receipts) {
			t.Fatalf("block %d differs: header %v body %v receipts %v", a.Number, string(a.Header) == string(b.Header), string(a.Body) == string(b.Body), string(a.Receipts) == string(b.Receipts))
		}
	}
	st := pool.Stats()[0]
	if st.Batch > 3 || st.RateLimited == 0 {
		t.Fatalf("batch limit / item rate limits not handled: %+v", st)
	}
	t.Logf("%+v", st)
}
