package decode

import (
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/em-mapping/extractor/internal/chaindb"
)

// Real arb1 fixtures captured with eth_getBlockByNumber(full) and
// eth_getBlockReceipts from https://arb1.arbitrum.io/rpc (debug_getRaw* is
// not exposed there). The test rebuilds header / txs / storage receipts with
// the OffchainLabs fork types, runs them through the production decoder with
// verification on, and requires (a) the official block hash, tx root, receipt
// root and bloom to be reproduced, and (b) every derived field to equal the
// value Nitro's RPC reports.

var fixtureBlocks = []uint64{1000000, 22204650, 22207817, 22207818, 22209702, 515626437}

const arb1NitroGenesis = 22207817

type rpcEnvelope struct {
	Result json.RawMessage  `json:"result"`
	Error  *json.RawMessage `json:"error"`
}

type rpcLog struct {
	Address  common.Address `json:"address"`
	Topics   []common.Hash  `json:"topics"`
	Data     hexutil.Bytes  `json:"data"`
	LogIndex hexutil.Uint64 `json:"logIndex"`
	TxIndex  hexutil.Uint64 `json:"transactionIndex"`
}

type rpcReceipt struct {
	Type              hexutil.Uint64  `json:"type"`
	Status            hexutil.Uint64  `json:"status"`
	CumulativeGasUsed hexutil.Uint64  `json:"cumulativeGasUsed"`
	GasUsed           hexutil.Uint64  `json:"gasUsed"`
	GasUsedForL1      hexutil.Uint64  `json:"gasUsedForL1"`
	EffectiveGasPrice *hexutil.Big    `json:"effectiveGasPrice"`
	ContractAddress   *common.Address `json:"contractAddress"`
	From              common.Address  `json:"from"`
	To                *common.Address `json:"to"`
	TxHash            common.Hash     `json:"transactionHash"`
	L1BlockNumber     *hexutil.Uint64 `json:"l1BlockNumber"`
	Logs              []rpcLog        `json:"logs"`
	LogsBloom         types.Bloom     `json:"logsBloom"`
}

type rpcTx struct {
	Hash     common.Hash     `json:"hash"`
	From     common.Address  `json:"from"`
	To       *common.Address `json:"to"`
	Type     hexutil.Uint64  `json:"type"`
	Nonce    hexutil.Uint64  `json:"nonce"`
	Gas      hexutil.Uint64  `json:"gas"`
	GasPrice *hexutil.Big    `json:"gasPrice"`
	Value    *hexutil.Big    `json:"value"`
	Input    hexutil.Bytes   `json:"input"`
	V        *hexutil.Big    `json:"v"`
	R        *hexutil.Big    `json:"r"`
	S        *hexutil.Big    `json:"s"`
}

type rpcBlock struct {
	Hash          common.Hash       `json:"hash"`
	Number        hexutil.Uint64    `json:"number"`
	L1BlockNumber *hexutil.Uint64   `json:"l1BlockNumber"`
	SendCount     *hexutil.Uint64   `json:"sendCount"`
	Transactions  []json.RawMessage `json:"transactions"`
}

func readResult(t *testing.T, path string, v any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var env rpcEnvelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatal(err)
	}
	if env.Error != nil {
		t.Fatalf("%s: rpc error %s", path, *env.Error)
	}
	if err := json.Unmarshal(env.Result, v); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
}

// FixtureRaw rebuilds the raw DB representation of one fixture block. It is
// exported to other test packages through testdata helpers (see chunk tests).
type fixture struct {
	raw      chaindb.RawBlock
	block    rpcBlock
	header   *types.Header
	txs      []rpcTx
	receipts []rpcReceipt
}

func loadFixture(t *testing.T, dir string, n uint64) *fixture {
	t.Helper()
	f := &fixture{}
	var hdr types.Header
	readResult(t, filepath.Join(dir, fmt.Sprintf("block_%d.json", n)), &hdr)
	readResult(t, filepath.Join(dir, fmt.Sprintf("block_%d.json", n)), &f.block)
	readResult(t, filepath.Join(dir, fmt.Sprintf("receipts_%d.json", n)), &f.receipts)
	f.header = &hdr
	if hdr.Hash() != f.block.Hash {
		t.Fatalf("block %d: header JSON re-encodes to %s, official hash %s", n, hdr.Hash(), f.block.Hash)
	}
	if len(f.receipts) != len(f.block.Transactions) {
		t.Fatalf("block %d: %d txs vs %d receipts", n, len(f.block.Transactions), len(f.receipts))
	}
	signer := types.LatestSignerForChainID(big.NewInt(42161))
	txs := make([]*types.Transaction, len(f.block.Transactions))
	for i, rawTx := range f.block.Transactions {
		var rt rpcTx
		if err := json.Unmarshal(rawTx, &rt); err != nil {
			t.Fatal(err)
		}
		f.txs = append(f.txs, rt)
		if uint8(rt.Type) == types.ArbitrumLegacyTxType {
			// Classic tx JSON omits EffectiveGasPrice / L1BlockNumber; Nitro's
			// receipt carries them (sourced from ArbitrumLegacyTxData).
			rc := f.receipts[i]
			inner := types.LegacyTx{Nonce: uint64(rt.Nonce), GasPrice: (*big.Int)(rt.GasPrice), Gas: uint64(rt.Gas), To: rt.To, Value: (*big.Int)(rt.Value), Data: rt.Input, V: (*big.Int)(rt.V), R: (*big.Int)(rt.R), S: (*big.Int)(rt.S)}
			l1 := uint64(0)
			if rc.L1BlockNumber != nil {
				l1 = uint64(*rc.L1BlockNumber)
			}
			var sender *common.Address
			if got, err := types.Sender(signer, types.NewTx(&inner)); err != nil || got != rt.From {
				s := rt.From
				sender = &s // unsigned Classic tx (e.g. L1->L2): sender override
			}
			tx, err := types.NewArbitrumLegacyTx(types.NewTx(&inner), rt.Hash, rc.EffectiveGasPrice.ToInt().Uint64(), l1, sender)
			if err != nil {
				t.Fatal(err)
			}
			txs[i] = tx
		} else {
			var tx types.Transaction
			if err := json.Unmarshal(rawTx, &tx); err != nil {
				t.Fatalf("block %d tx %d (type %d): %v", n, i, rt.Type, err)
			}
			txs[i] = &tx
		}
		if txs[i].Hash() != rt.Hash {
			t.Fatalf("block %d tx %d: rebuilt hash %s, official %s", n, i, txs[i].Hash(), rt.Hash)
		}
	}
	stored := make([]*types.ReceiptForStorage, len(f.receipts))
	for i, rc := range f.receipts {
		r := &types.Receipt{Type: uint8(rc.Type), Status: uint64(rc.Status), CumulativeGasUsed: uint64(rc.CumulativeGasUsed), GasUsedForL1: uint64(rc.GasUsedForL1)}
		for _, l := range rc.Logs {
			r.Logs = append(r.Logs, &types.Log{Address: l.Address, Topics: l.Topics, Data: l.Data})
		}
		if uint8(rc.Type) == types.ArbitrumLegacyTxType {
			r.GasUsed = uint64(rc.GasUsed)
			r.PostState = []byte{0}
			if rc.ContractAddress != nil {
				r.ContractAddress = *rc.ContractAddress
			}
		} else if uint8(rc.Type) >= types.ArbitrumDepositTxType && rc.ContractAddress != nil {
			r.ContractAddress = *rc.ContractAddress
		}
		stored[i] = (*types.ReceiptForStorage)(r)
	}
	hraw, _ := rlp.EncodeToBytes(&hdr)
	braw, _ := rlp.EncodeToBytes(&types.Body{Transactions: txs})
	rraw, err := rlp.EncodeToBytes(stored)
	if err != nil {
		t.Fatal(err)
	}
	f.raw = chaindb.RawBlock{Number: n, Hash: f.block.Hash, Header: hraw, Body: braw, Receipts: rraw}
	return f
}

func fixtureDir() string { return filepath.Join("..", "..", "testdata", "rpc") }

func TestRPCFixturesRootsAndFields(t *testing.T) {
	for _, n := range fixtureBlocks {
		t.Run(fmt.Sprint(n), func(t *testing.T) {
			f := loadFixture(t, fixtureDir(), n)
			d := New(Config{ChainID: 42161, NitroGenesis: arb1NitroGenesis, Verify: true, VerifyBloom: true, WETH: &WETHFilter{Any: true}})
			var rows Rows
			var st Stats
			parent := f.header.ParentHash
			if _, err := d.Block(&f.raw, &parent, &rows, &st); err != nil {
				t.Fatalf("decode/verify: %v", err)
			}
			if len(rows.Blocks) != 1 || len(rows.Txs) != len(f.txs) {
				t.Fatalf("rows: %d blocks %d txs", len(rows.Blocks), len(rows.Txs))
			}
			b := rows.Blocks[0]
			if common.Hash(b.Hash) != f.block.Hash {
				t.Fatalf("hash")
			}
			if f.block.L1BlockNumber != nil {
				if b.L1BlockNumber == nil || *b.L1BlockNumber != uint64(*f.block.L1BlockNumber) {
					t.Errorf("l1_block_number got %v want %d", b.L1BlockNumber, *f.block.L1BlockNumber)
				}
			}
			if f.block.SendCount != nil && (b.SendCount == nil || *b.SendCount != uint64(*f.block.SendCount)) {
				t.Errorf("send_count got %v want %d", b.SendCount, *f.block.SendCount)
			}
			logi := 0
			for i, row := range rows.Txs {
				rt, rc := f.txs[i], f.receipts[i]
				if common.Hash(row.Hash) != rt.Hash || common.Address(row.From) != rt.From || common.Address(row.From) != rc.From {
					t.Errorf("tx %d hash/from mismatch: %x %x want %s %s", i, row.Hash, row.From, rt.Hash, rt.From)
				}
				if row.Type != uint8(rt.Type) || row.Nonce != uint64(rt.Nonce) || row.GasLimit != uint64(rt.Gas) {
					t.Errorf("tx %d type/nonce/gas mismatch", i)
				}
				if new(big.Int).SetBytes(row.Value[:]).Cmp(rt.Value.ToInt()) != 0 {
					t.Errorf("tx %d value %x want %s", i, row.Value, rt.Value)
				}
				if (row.To == nil) != (rt.To == nil) || (row.To != nil && common.Address(*row.To) != *rt.To) {
					t.Errorf("tx %d to mismatch", i)
				}
				if row.Status != uint8(rc.Status) || row.GasUsed != uint64(rc.GasUsed) || row.GasUsedForL1 != uint64(rc.GasUsedForL1) {
					t.Errorf("tx %d status/gasUsed/gasUsedForL1 got %d/%d/%d want %d/%d/%d", i, row.Status, row.GasUsed, row.GasUsedForL1, rc.Status, rc.GasUsed, rc.GasUsedForL1)
				}
				if rc.EffectiveGasPrice != nil {
					if row.EffectiveGasPrice == nil || *row.EffectiveGasPrice != rc.EffectiveGasPrice.ToInt().Uint64() {
						t.Errorf("tx %d effective gas price got %v want %s", i, row.EffectiveGasPrice, rc.EffectiveGasPrice)
					}
				}
				if (row.ContractAddress == nil) != (rc.ContractAddress == nil) || (row.ContractAddress != nil && common.Address(*row.ContractAddress) != *rc.ContractAddress) {
					t.Errorf("tx %d contract address got %v want %v", i, row.ContractAddress, rc.ContractAddress)
				}
				for _, l := range rc.Logs {
					lr := rows.Logs[logi]
					if uint64(lr.LogIndex) != uint64(l.LogIndex) || uint64(lr.TxIndex) != uint64(l.TxIndex) || common.Address(lr.Address) != l.Address || string(lr.Data) != string(l.Data) || int(lr.TopicCount) != len(l.Topics) {
						t.Errorf("log %d mismatch", logi)
					}
					logi++
				}
			}
			if logi != len(rows.Logs) {
				t.Errorf("log count %d vs %d", logi, len(rows.Logs))
			}
			t.Logf("block %d ok: txs=%d types=%v logs=%d transfers=%d creations=%d classic=%v l1=%v", n, len(rows.Txs), st.TxTypes, len(rows.Logs), len(rows.Transfers), len(rows.Creations), b.IsClassic, deref(b.L1BlockNumber))
		})
	}
}

func deref(p *uint64) any {
	if p == nil {
		return nil
	}
	return *p
}

// Corrupting any verified component must fail with the right reason.
func TestRPCFixtureCorruptionDetected(t *testing.T) {
	f := loadFixture(t, fixtureDir(), 22209702)
	d := New(Config{ChainID: 42161, NitroGenesis: arb1NitroGenesis, Verify: true, VerifyBloom: true})
	check := func(name string, raw chaindb.RawBlock, parent *common.Hash, want string) {
		var rows Rows
		var st Stats
		_, err := d.Block(&raw, parent, &rows, &st)
		be, ok := err.(*BlockError)
		if !ok || be.Reason != want {
			t.Errorf("%s: got %v want %s", name, err, want)
		}
	}
	bad := f.raw
	bad.Hash[0] ^= 1
	check("hash", bad, nil, ReasonHeaderHash)
	wrong := common.Hash{1}
	check("parent", f.raw, &wrong, ReasonParent)
	// Drop the last receipt's logs -> receipt root mismatch.
	var stored []*types.ReceiptForStorage
	if err := rlp.DecodeBytes(f.raw.Receipts, &stored); err != nil {
		t.Fatal(err)
	}
	for i := range stored {
		(*types.Receipt)(stored[i]).Type = uint8(f.receipts[i].Type)
	}
	stored[len(stored)-1].CumulativeGasUsed++
	rr, _ := rlp.EncodeToBytes(stored)
	bad = f.raw
	bad.Receipts = rr
	check("receipt root", bad, nil, ReasonReceiptRoot)
	// Drop a tx -> receipt count / tx root.
	var body types.Body
	rlp.DecodeBytes(f.raw.Body, &body)
	body.Transactions = body.Transactions[:len(body.Transactions)-1]
	bb, _ := rlp.EncodeToBytes(&body)
	bad = f.raw
	bad.Body = bb
	check("tx count", bad, nil, ReasonReceiptCount)
}
