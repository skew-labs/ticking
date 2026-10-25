package trace

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/parquet-go/parquet-go"

	"github.com/em-mapping/extractor/internal/chunk"
	"github.com/em-mapping/extractor/internal/schema"
)

const A, B, C, D, E = "0x00000000000000000000000000000000000000aa", "0x00000000000000000000000000000000000000bb", "0x00000000000000000000000000000000000000cc", "0x00000000000000000000000000000000000000dd", "0x00000000000000000000000000000000000000ee"

// A constructed callTracer block result exercising every rule.
var blockJSON = `[
 {"txHash":"0x0100000000000000000000000000000000000000000000000000000000000000","result":
  {"type":"CALL","from":"` + A + `","to":"` + B + `","value":"0x5","calls":[
    {"type":"CALL","from":"` + B + `","to":"` + C + `","value":"0x10"},
    {"type":"DELEGATECALL","from":"` + B + `","to":"` + D + `","value":"0x10","calls":[
        {"type":"CALL","from":"` + B + `","to":"` + E + `","value":"0x11"},
        {"type":"STATICCALL","from":"` + B + `","to":"` + E + `"}
    ]},
    {"type":"CALL","from":"` + B + `","to":"` + C + `","value":"0x12","error":"execution reverted","calls":[
        {"type":"CALL","from":"` + C + `","to":"` + D + `","value":"0x13"},
        {"type":"CREATE","from":"` + C + `","to":"` + E + `","value":"0x14"}
    ]},
    {"type":"CALL","from":"` + B + `","to":"` + C + `","value":"0x0","calls":[
        {"type":"CALL","from":"` + C + `","to":"` + D + `","value":"0x1","calls":[
            {"type":"CALL","from":"` + D + `","to":"` + A + `","value":"0x2","error":"out of gas"}
        ]},
        {"type":"CALLCODE","from":"` + C + `","to":"` + D + `","value":"0x99"},
        {"type":"CREATE2","from":"` + C + `","to":"` + E + `","value":"0x0"},
        {"type":"CREATE","from":"` + C + `","to":"` + D + `","value":"0x15"},
        {"type":"SELFDESTRUCT","from":"` + C + `","to":"` + A + `","value":"0x16"}
    ]}
  ]}},
 {"txHash":"0x0200000000000000000000000000000000000000000000000000000000000000","result":
  {"type":"CALL","from":"` + A + `","to":"` + B + `","value":"0x5","error":"execution reverted","calls":[
    {"type":"CALL","from":"` + B + `","to":"` + C + `","value":"0x77"}
  ]}},
 {"txHash":"0x0300000000000000000000000000000000000000000000000000000000000000","result":
  {"type":"CALL","from":"` + A + `","to":"` + B + `","value":"0xde0b6b3a7640000","beforeEVMTransfers":[{"purpose":"feePayment","from":"` + A + `","to":null,"value":"0x1"}],"afterEVMTransfers":[]}}
]`

type want struct {
	tx    uint32
	path  string
	typ   string
	from  string
	to    string
	value int64
	depth uint16
}

func TestCallTracerFilter(t *testing.T) {
	var traces []TxTrace
	if err := json.Unmarshal([]byte(blockJSON), &traces); err != nil {
		t.Fatal(err)
	}
	var st Stats
	rows, cre, err := Extract(77, traces, nil, nil, &st)
	if err != nil {
		t.Fatal(err)
	}
	exp := []want{
		{0, "0", "CALL", B, C, 0x10, 1},
		{0, "1.0", "CALL", B, E, 0x11, 2}, // child of DELEGATECALL kept; the DELEGATECALL value itself is not
		{0, "3.0", "CALL", C, D, 0x1, 2},  // its reverted child excluded
		{0, "3.3", "CREATE", C, D, 0x15, 2},
		{0, "3.4", "SELFDESTRUCT", C, A, 0x16, 2},
	}
	if len(rows) != len(exp) {
		for _, r := range rows {
			t.Logf("%d %s %s %x->%x", r.TxIndex, r.TracePath, r.CallType, r.From, r.To)
		}
		t.Fatalf("got %d rows want %d", len(rows), len(exp))
	}
	for i, w := range exp {
		r := rows[i]
		if r.BlockNumber != 77 || r.TxIndex != w.tx || r.TracePath != w.path || r.CallType != w.typ || common.Address(r.From) != common.HexToAddress(w.from) || common.Address(r.To) != common.HexToAddress(w.to) || new(big.Int).SetBytes(r.Value[:]).Int64() != w.value || r.Depth != w.depth {
			t.Errorf("row %d = %+v want %+v", i, r, w)
		}
	}
	// Creations: CREATE2 (zero value) and CREATE (value) in the successful subtree;
	// the CREATE under the reverted frame is excluded.
	if len(cre) != 2 || cre[0].TracePath != "3.2" || cre[0].CallType != "CREATE2" || cre[1].TracePath != "3.3" {
		t.Fatalf("creations %+v", cre)
	}
	if st.FailedTxs != 1 || st.RevertedSubtrees != 2 || st.DelegateValueFrames != 2 || st.ArbEVMTransfers != 1 || st.Txs != 3 {
		t.Fatalf("stats %+v", st)
	}
}

func TestTracerErrorFailsBlock(t *testing.T) {
	traces := []TxTrace{{TxHash: common.Hash{1}, Error: "execution timeout"}}
	if _, _, err := Extract(1, traces, nil, nil, &Stats{}); err == nil {
		t.Fatal("expected error")
	}
}

// fake Nitro RPC.
func fakeNode(t *testing.T, failAt uint64) (*httptest.Server, *atomic.Int64) {
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req struct {
			ID     uint64            `json:"id"`
			Method string            `json:"method"`
			Params []json.RawMessage `json:"params"`
		}
		json.Unmarshal(body, &req)
		calls.Add(1)
		if req.Method != "debug_traceBlockByNumber" || len(req.Params) != 2 || !strings.Contains(string(req.Params[1]), `"onlyTopCall":false`) || !strings.Contains(string(req.Params[1]), `"callTracer"`) {
			t.Errorf("bad request %s", body)
		}
		var hexn string
		json.Unmarshal(req.Params[0], &hexn)
		n, _ := strconv.ParseUint(strings.TrimPrefix(hexn, "0x"), 16, 64)
		if n == failAt {
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"error":{"code":-32000,"message":"missing trie node"}}`, req.ID)
			return
		}
		res := blockJSON
		if n%2 == 1 {
			res = "[]"
		}
		fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":%s}`, req.ID, res)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func TestRunClassicUnavailableResumeAndFailure(t *testing.T) {
	srv, calls := fakeNode(t, 1_000_000)
	out := t.TempDir()
	c := NewClient(srv.URL, 4, 10*time.Second)
	c.Retries = 1
	o := Options{ChainID: 42161, TraceStart: 22207818, From: 22207790, To: 22207839, Workers: 3, Chunk: 20, Out: out, Client: c, CheckSHA: true, Log: io.Discard}
	s, err := Run(context.Background(), o)
	if err != nil {
		t.Fatal(err)
	}
	// chunks: [..790-799] all classic, [800-819] partial, [820-839] full
	if s.Chunks != 3 || s.Unavailable != 1 || s.Done != 2 || s.Failed != 0 {
		t.Fatalf("summary %+v", s)
	}
	if got := calls.Load(); got != 22207839-22207818+1 {
		t.Fatalf("rpc calls %d (classic blocks must not be called)", got)
	}
	m, err := chunk.Read(out, chunk.Range{From: 22207800, To: 22207819})
	if err != nil || m.Status != chunk.StatusOK || m.Unavailable == nil || m.Unavailable.From != 22207800 || m.Unavailable.To != 22207817 || m.Unavailable.Count != 18 {
		t.Fatalf("partial manifest %+v %v", m, err)
	}
	m, _ = chunk.Read(out, chunk.Range{From: 22207790, To: 22207799})
	if m.Status != chunk.StatusTraceUnavailable || m.Unavailable.Reason != ReasonClassic {
		t.Fatalf("classic manifest %+v", m)
	}
	rows, err := parquet.ReadFile[schema.NativeInternal](filepath.Join(out, chunk.FileRel(TNative, chunk.Range{From: 22207820, To: 22207839})))
	if err != nil || len(rows) != 10*5 {
		t.Fatalf("rows %d %v", len(rows), err)
	}
	// Resume: nothing re-called.
	before := calls.Load()
	s, _ = Run(context.Background(), o)
	if s.Skipped != 3 || calls.Load() != before {
		t.Fatalf("resume %+v calls %d", s, calls.Load()-before)
	}
	// RPC error -> FAILED, files removed, latency stats recorded.
	srv2, _ := fakeNode(t, 22207825)
	o.Client = NewClient(srv2.URL, 4, 10*time.Second)
	o.Client.Retries = 1
	o.Force = true
	o.From, o.To = 22207820, 22207839
	s, _ = Run(context.Background(), o)
	if s.Failed != 1 {
		t.Fatalf("expected failure %+v", s)
	}
	m, _ = chunk.Read(out, chunk.Range{From: 22207820, To: 22207839})
	if m.Status != chunk.StatusFailed || m.Failure.Block != 22207825 || m.Failure.Reason != "RPC_ERROR" {
		t.Fatalf("failed manifest %+v", m.Failure)
	}
	raw, _ := json.Marshal(m.Stats)
	var cs struct {
		Latency Latency `json:"rpc_latency"`
	}
	json.Unmarshal(raw, &cs)
	if cs.Latency.Calls != 5 || cs.Latency.Retries != 1 || cs.Latency.P99Ms < cs.Latency.P50Ms || cs.Latency.Bytes == 0 {
		t.Fatalf("latency stats %s", raw)
	}
}
