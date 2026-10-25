package decode_test

// Live check of bor state-sync (0x7f) decoding against Polygon mainnet blocks, run only when EM_POLYGON_RPC is set:
//
//	EM_POLYGON_RPC=https://polygon-bor-rpc.publicnode.com go test ./internal/decode -run TestBorStateSyncRPC -v
//
// RPC JSON omits the state-sync payload, so each block is rebuilt from JSON without its 0x7f tx (rpcsrc.Convert) and
// the canonical 0x7f bytes (eth_getRawTransactionByBlockNumberAndIndex, served for recent blocks only) and its receipt
// are spliced back in. The decoder must then pass header hash, tx root and receipt root, and emit the state-sync row and
// logs as RPC reports them.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/big"
	"net/http"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/decode"
	"github.com/em-mapping/extractor/internal/rpcsrc"
)

func borRPC(t *testing.T, url, method string, params ...any) json.RawMessage {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
	for try := 0; ; try++ {
		req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
		req.Header.Set("content-type", "application/json")
		req.Header.Set("user-agent", "curl/8.7.1")
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			var out struct {
				Result json.RawMessage
				Error  *struct{ Message string }
			}
			err = json.NewDecoder(resp.Body).Decode(&out)
			resp.Body.Close()
			if err == nil && out.Error != nil {
				err = fmt.Errorf("%s: %s", method, out.Error.Message)
			}
			if err == nil && resp.StatusCode == 200 {
				return out.Result
			}
			if err == nil {
				err = fmt.Errorf("%s: http %d", method, resp.StatusCode)
			}
		}
		if try == 6 {
			t.Fatalf("%v", err)
		}
		time.Sleep(time.Duration(1+try) * time.Second)
	}
}

// gethLayout re-encodes a stored receipt list in go-ethereum's freezer layout [status, cumulative gas, logs] (what bor
// and linea-geth databases hold); rpcsrc.Convert writes Nitro's layout.
func gethLayout(t *testing.T, b []byte) []byte {
	t.Helper()
	var rs []*types.ReceiptForStorage
	if err := rlp.DecodeBytes(b, &rs); err != nil {
		t.Fatal(err)
	}
	type stored struct {
		PostStateOrStatus []byte
		CumulativeGasUsed uint64
		Logs              []*types.Log
	}
	out := make([]stored, len(rs))
	for i, r := range rs {
		st := []byte{}
		if r.Status == types.ReceiptStatusSuccessful {
			st = []byte{0x01}
		}
		out[i] = stored{st, r.CumulativeGasUsed, r.Logs}
	}
	enc, _ := rlp.EncodeToBytes(out)
	return enc
}

func TestBorStateSyncRPC(t *testing.T) {
	url := os.Getenv("EM_POLYGON_RPC")
	if url == "" {
		t.Skip("EM_POLYGON_RPC not set")
	}
	want, _ := strconv.Atoi(os.Getenv("EM_POLYGON_BLOCKS"))
	if want == 0 {
		want = 4
	}
	var headHex hexutil.Uint64
	json.Unmarshal(borRPC(t, url, "eth_blockNumber"), &headHex)
	head := uint64(headHex) - 3
	signer := types.LatestSignerForChainID(big.NewInt(137))
	dec := decode.New(decode.Config{ChainID: 137, Verify: true, VerifyBloom: true, Family: "geth"})
	checked := 0
	for b := head - head%16; checked < want && b > head-4000; b -= 16 {
		blockJSON := borRPC(t, url, "eth_getBlockByNumber", hexutil.EncodeUint64(b), true)
		var blk struct {
			Transactions []map[string]any
		}
		if err := json.Unmarshal(blockJSON, &blk); err != nil || len(blk.Transactions) == 0 {
			continue
		}
		last := blk.Transactions[len(blk.Transactions)-1]
		if last["type"] != "0x7f" {
			continue
		}
		idx := len(blk.Transactions) - 1
		for i, tx := range blk.Transactions[:idx] {
			if tx["type"] == "0x7f" {
				t.Fatalf("block %d: state-sync tx at %d is not last", b, i)
			}
		}
		rawHex := borRPC(t, url, "eth_getRawTransactionByBlockNumberAndIndex", hexutil.EncodeUint64(b), hexutil.EncodeUint64(uint64(idx)))
		var canon hexutil.Bytes
		if err := json.Unmarshal(rawHex, &canon); err != nil || len(canon) == 0 || canon[0] != decode.StateSyncTxType {
			t.Fatalf("block %d: raw state-sync tx %s", b, rawHex)
		}
		receiptsJSON := borRPC(t, url, "eth_getBlockReceipts", hexutil.EncodeUint64(b))
		var rcs []json.RawMessage
		json.Unmarshal(receiptsJSON, &rcs)
		var ssr struct {
			Type              hexutil.Uint64
			Status            hexutil.Uint64
			CumulativeGasUsed hexutil.Uint64
			GasUsed           hexutil.Uint64
			Logs              []struct {
				Address  common.Address
				Topics   []common.Hash
				Data     hexutil.Bytes
				LogIndex hexutil.Uint64
			}
		}
		if len(rcs) != len(blk.Transactions) {
			t.Fatalf("block %d: %d receipts for %d txs", b, len(rcs), len(blk.Transactions))
		}
		json.Unmarshal(rcs[idx], &ssr)
		if ssr.Type != decode.StateSyncTxType {
			t.Fatalf("block %d: state-sync receipt type %d", b, ssr.Type)
		}

		// rebuild without the state-sync tx, then splice the canonical tx and its stored receipt back in
		var full map[string]json.RawMessage
		json.Unmarshal(blockJSON, &full)
		var txsJSON []json.RawMessage
		json.Unmarshal(full["transactions"], &txsJSON)
		full["transactions"], _ = json.Marshal(txsJSON[:idx])
		stripped, _ := json.Marshal(full)
		strippedRcs, _ := json.Marshal(rcs[:idx])
		raw, err := rpcsrc.Convert(signer, b, stripped, strippedRcs)
		if err != nil {
			t.Fatalf("block %d: convert: %v", b, err)
		}
		var body struct {
			Txs  []rlp.RawValue
			Rest []rlp.RawValue `rlp:"tail"`
		}
		if err := rlp.DecodeBytes(raw.Body, &body); err != nil {
			t.Fatal(err)
		}
		enc, _ := rlp.EncodeToBytes([]byte(canon))
		body.Txs = append(body.Txs, enc)
		raw.Body, _ = rlp.EncodeToBytes(&body)
		var stored []rlp.RawValue
		rlp.DecodeBytes(raw.Receipts, &stored)
		r := &types.Receipt{Status: uint64(ssr.Status), CumulativeGasUsed: uint64(ssr.CumulativeGasUsed)}
		for _, l := range ssr.Logs {
			r.Logs = append(r.Logs, &types.Log{Address: l.Address, Topics: l.Topics, Data: l.Data})
		}
		se, _ := rlp.EncodeToBytes((*types.ReceiptForStorage)(r))
		stored = append(stored, se)
		raw.Receipts, _ = rlp.EncodeToBytes(stored)

		var rows decode.Rows
		var st decode.Stats
		if _, err := dec.Block(raw, nil, &rows, &st); err != nil {
			t.Fatalf("block %d: decode: %v", b, err)
		}
		gl := *raw
		gl.Receipts = gethLayout(t, raw.Receipts)
		var rowsG decode.Rows
		if _, err := dec.Block(&gl, nil, &rowsG, &st); err != nil || len(rowsG.Logs) != len(rows.Logs) || len(rowsG.Transfers) != len(rows.Transfers) {
			t.Fatalf("block %d: geth-layout receipts: %v (%d/%d logs)", b, err, len(rowsG.Logs), len(rows.Logs))
		}
		tx := rows.Txs[idx]
		if common.Hash(tx.Hash).Hex() != last["hash"] || tx.Type != decode.StateSyncTxType || common.Address(tx.From) != decode.BorSystemAddress || tx.GasUsed != 0 || tx.Status != 1 {
			t.Fatalf("block %d: state-sync row %+v (rpc hash %v)", b, tx, last["hash"])
		}
		var got []string
		for _, l := range rows.Logs {
			if l.TxIndex == uint32(idx) {
				got = append(got, fmt.Sprintf("%d %s %x", l.LogIndex, common.Address(l.Address).Hex(), l.Data))
			}
		}
		var exp []string
		for _, l := range ssr.Logs {
			exp = append(exp, fmt.Sprintf("%d %s %x", uint64(l.LogIndex), l.Address.Hex(), []byte(l.Data)))
		}
		if strings.Join(got, "|") != strings.Join(exp, "|") {
			t.Fatalf("block %d: state-sync logs differ\n got %v\nwant %v", b, got, exp)
		}
		var ids []uint64
		var d []struct {
			ID       uint64
			Contract common.Address
			Data     []byte
			TxHash   common.Hash
		}
		rlp.DecodeBytes(canon[1:], &d)
		for _, e := range d {
			ids = append(ids, e.ID)
		}
		t.Logf("block %d: %d txs, state-sync tx %s ok (tx root, receipt root, bloom), %d events %v, %d logs, transfers %d",
			b, len(rows.Txs), common.Hash(tx.Hash).Hex(), len(d), ids, len(got), len(rows.Transfers))
		checked++
	}
	if checked < want {
		t.Fatalf("checked %d of %d blocks", checked, want)
	}
}

// Pre-fork: bor's RPC appends the state-sync "tx" (hash keccak(BorReceiptKey)) and its receipt after the block's txs.
// The block is rebuilt without them, the receipt goes in as RawBlock.BorReceipt (as the freezer's matic-bor-receipts
// table delivers it), and the decoder must pass both roots and emit the appended row and logs as RPC shows them.
//
//	EM_POLYGON_ARCHIVE_RPC=https://polygon.drpc.org go test ./internal/decode -run TestBorPreForkRPC -v
func TestBorPreForkRPC(t *testing.T) {
	url := os.Getenv("EM_POLYGON_ARCHIVE_RPC")
	if url == "" {
		t.Skip("EM_POLYGON_ARCHIVE_RPC not set")
	}
	signer := types.LatestSignerForChainID(big.NewInt(137))
	dec := decode.New(decode.Config{ChainID: 137, Verify: true, Family: "geth"})
	blocks := []uint64{80084752, 80084560, 80084368}
	for _, s := range strings.Fields(os.Getenv("EM_POLYGON_PREFORK_BLOCKS")) {
		n, _ := strconv.ParseUint(s, 10, 64)
		blocks = append(blocks, n)
	}
	checked := 0
	for _, b := range blocks {
		blockJSON := borRPC(t, url, "eth_getBlockByNumber", hexutil.EncodeUint64(b), true)
		receiptsJSON := borRPC(t, url, "eth_getBlockReceipts", hexutil.EncodeUint64(b))
		var full map[string]json.RawMessage
		json.Unmarshal(blockJSON, &full)
		var hdr struct{ Hash common.Hash }
		json.Unmarshal(blockJSON, &hdr)
		var txsJSON, rcs []json.RawMessage
		json.Unmarshal(full["transactions"], &txsJSON)
		json.Unmarshal(receiptsJSON, &rcs)
		derived := crypto.Keccak256Hash(chaindb.BorReceiptKey(b, hdr.Hash))
		idx := -1
		for i, tj := range txsJSON {
			var th struct{ Hash common.Hash }
			json.Unmarshal(tj, &th)
			if th.Hash == derived {
				idx = i
			}
		}
		if idx < 0 {
			t.Logf("block %d: no state-sync", b)
			continue
		}
		if idx != len(txsJSON)-1 {
			t.Fatalf("block %d: bor tx at %d of %d", b, idx, len(txsJSON))
		}
		var br struct {
			Status            hexutil.Uint64
			CumulativeGasUsed hexutil.Uint64
			TransactionHash   common.Hash
			Logs              []struct {
				Address  common.Address
				Topics   []common.Hash
				Data     hexutil.Bytes
				LogIndex hexutil.Uint64
			}
		}
		var brJSON json.RawMessage
		if len(rcs) == len(txsJSON) {
			brJSON, rcs = rcs[idx], rcs[:idx]
		} else {
			brJSON = borRPC(t, url, "eth_getTransactionReceipt", derived)
		}
		json.Unmarshal(brJSON, &br)
		if br.TransactionHash != derived {
			t.Fatalf("block %d: bor receipt for %s, want %s", b, br.TransactionHash, derived)
		}
		full["transactions"], _ = json.Marshal(txsJSON[:idx])
		stripped, _ := json.Marshal(full)
		strippedRcs, _ := json.Marshal(rcs)
		raw, err := rpcsrc.Convert(signer, b, stripped, strippedRcs)
		if err != nil {
			t.Fatalf("block %d: convert: %v", b, err)
		}
		r := &types.Receipt{Status: uint64(br.Status), CumulativeGasUsed: uint64(br.CumulativeGasUsed)}
		for _, l := range br.Logs {
			r.Logs = append(r.Logs, &types.Log{Address: l.Address, Topics: l.Topics, Data: l.Data})
		}
		raw.Receipts = gethLayout(t, raw.Receipts)
		one, _ := rlp.EncodeToBytes([]*types.ReceiptForStorage{(*types.ReceiptForStorage)(r)})
		var oneRaw []rlp.RawValue
		rlp.DecodeBytes(gethLayout(t, one), &oneRaw)
		raw.BorReceipt = oneRaw[0]
		var rows decode.Rows
		var st decode.Stats
		if _, err := dec.Block(raw, nil, &rows, &st); err != nil {
			t.Fatalf("block %d: decode: %v", b, err)
		}
		if len(rows.Txs) != idx+1 || rows.Blocks[0].TxCount != uint32(idx+1) {
			t.Fatalf("block %d: %d tx rows, tx count %d, want %d", b, len(rows.Txs), rows.Blocks[0].TxCount, idx+1)
		}
		tx := rows.Txs[idx]
		if common.Hash(tx.Hash) != derived || tx.Type != decode.StateSyncTxType || common.Address(tx.From) != decode.BorSystemAddress || tx.GasUsed != 0 || tx.Status != 1 {
			t.Fatalf("block %d: bor row %+v", b, tx)
		}
		var got, exp []string
		for _, l := range rows.Logs {
			if l.TxIndex == uint32(idx) {
				got = append(got, fmt.Sprintf("%d %s %x", l.LogIndex, common.Address(l.Address).Hex(), l.Data))
			}
		}
		for _, l := range br.Logs {
			exp = append(exp, fmt.Sprintf("%d %s %x", uint64(l.LogIndex), l.Address.Hex(), []byte(l.Data)))
		}
		if strings.Join(got, "|") != strings.Join(exp, "|") {
			t.Fatalf("block %d: bor logs differ\n got %v\nwant %v", b, got, exp)
		}
		// the same block without its bor receipt must decode to the body only (an empty freezer entry)
		raw.BorReceipt = nil
		var rows2 decode.Rows
		if _, err := dec.Block(raw, nil, &rows2, &st); err != nil || len(rows2.Txs) != idx {
			t.Fatalf("block %d: without bor receipt: %v, %d rows", b, err, len(rows2.Txs))
		}
		t.Logf("block %d: %d body txs + bor tx %s ok (tx root, receipt root), %d logs from index %d, %d transfers",
			b, idx, derived.Hex(), len(got), uint64(br.Logs[0].LogIndex), len(rows.Transfers))
		checked++
	}
	if checked < 3 {
		t.Fatalf("checked %d pre-fork blocks", checked)
	}
}
