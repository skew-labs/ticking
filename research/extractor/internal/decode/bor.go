package decode

// Polygon PoS (bor) state-sync transactions.
//
// From bor's Madhugiri fork (PIP-74; mainnet block 80,084,800) state-sync events travel in the block body as the last
// transaction, type 0x7f: canonical bytes 0x7f || rlp([]StateSyncData{ID, Contract, Data, L1TxHash}), hash =
// keccak(canonical bytes). Its receipt is in the normal receipt list and in the receipt root (type 0x7f, status 1, the
// previous receipt's cumulative gas, gas used 0). go-ethereum (and the Nitro fork this module builds on) rejects type
// 0x7f, so a body holding one takes the tx-by-tx path below.
//
// Before the fork bor kept the state-sync receipt outside the receipt trie, on sprint-start blocks: freezer table
// "matic-bor-receipts", or kv key "matic-bor-receipt-"+num+hash (chaindb.RawBlock.BorReceipt). bor's RPC shows it as an
// extra tx appended after the block's transactions, hash keccak(that key), with log indexes continuing after the
// block's logs; it is emitted the same way.
//
// Both forms are emitted as type 127 rows, from = bor's system address, to = the state-receiver contract (the
// execution view; bor's RPC shows zero addresses, and type 0 for the pre-fork form).

import (
	"bytes"
	"fmt"
	"math/big"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"
)

// StateSyncTxType is bor's state-sync transaction type.
const StateSyncTxType = 0x7f

var (
	BorSystemAddress = common.HexToAddress("0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE")
	BorStateReceiver = common.HexToAddress("0x0000000000000000000000000000000000001001")
)

// borMadhugiri is the first block with state-sync txs in the body, per bor chain id (bor params/config.go).
var borMadhugiri = map[uint64]uint64{137: 80084800, 80002: 28899616}

// stateSync marks a state-sync tx in a block's tx list.
type stateSync struct {
	hash     common.Hash
	appended bool // pre-fork bor receipt, appended after the body's txs (outside both roots)
}

type stateSyncData struct {
	ID       uint64
	Contract common.Address
	Data     []byte
	TxHash   common.Hash
}

type rawBody struct {
	Txs    []rlp.RawValue
	Uncles []*types.Header
	Rest   []rlp.RawValue `rlp:"tail"`
}

// stateSyncPlaceholder stands in for a state-sync tx in the per-tx loop: no value, gas, nonce or input, to = the
// state receiver. Its hash, type and sender come from the stateSync entry.
func stateSyncPlaceholder() *types.Transaction {
	return types.NewTx(&types.LegacyTx{To: &BorStateReceiver, Value: new(big.Int), GasPrice: new(big.Int)})
}

// decodeBodyTxByTx decodes a body tx by tx, turning type-0x7f state-sync txs into placeholders. canon holds each
// tx's canonical bytes (what the tx trie hashes). ss is nil when the body has no state-sync tx.
func decodeBodyTxByTx(b []byte) (txs []*types.Transaction, uncles []*types.Header, canon [][]byte, ss map[int]stateSync, err error) {
	var rb rawBody
	if err = rlp.DecodeBytes(b, &rb); err != nil {
		return
	}
	txs = make([]*types.Transaction, len(rb.Txs))
	canon = make([][]byte, len(rb.Txs))
	for i, raw := range rb.Txs {
		kind, content, _, e := rlp.Split(raw)
		if e != nil {
			err = fmt.Errorf("tx %d: %w", i, e)
			return
		}
		if kind == rlp.String {
			canon[i] = content // typed: type || payload
			if len(content) > 0 && content[0] == StateSyncTxType {
				var d []stateSyncData
				if e := rlp.DecodeBytes(content[1:], &d); e != nil {
					err = fmt.Errorf("tx %d (state-sync): %w", i, e)
					return
				}
				if ss == nil {
					ss = map[int]stateSync{}
				}
				ss[i] = stateSync{hash: crypto.Keccak256Hash(content)}
				txs[i] = stateSyncPlaceholder()
				continue
			}
		} else {
			canon[i] = raw // legacy: the RLP list itself
		}
		tx := new(types.Transaction)
		if e := rlp.DecodeBytes(raw, tx); e != nil {
			err = fmt.Errorf("tx %d: %w", i, e)
			return
		}
		txs[i] = tx
	}
	uncles = rb.Uncles
	return
}

// canonList derives the tx root from canonical tx bytes.
type canonList [][]byte

func (l canonList) Len() int                            { return len(l) }
func (l canonList) EncodeIndex(i int, w *bytes.Buffer) { w.Write(l[i]) }

// borReceipts derives the receipt root, encoding type-0x7f receipts like any typed receipt (as bor does); the
// go-ethereum encoder writes only the type byte for types it doesn't know.
type borReceipts types.Receipts

type receiptRLP struct {
	PostStateOrStatus []byte
	CumulativeGasUsed uint64
	Bloom             types.Bloom
	Logs              []*types.Log
}

func (rs borReceipts) Len() int { return len(rs) }

func (rs borReceipts) EncodeIndex(i int, w *bytes.Buffer) {
	r := rs[i]
	if r.Type != StateSyncTxType {
		types.Receipts(rs).EncodeIndex(i, w)
		return
	}
	status := r.PostState
	if len(status) == 0 {
		status = []byte{}
		if r.Status == types.ReceiptStatusSuccessful {
			status = []byte{0x01}
		}
	}
	w.WriteByte(StateSyncTxType)
	rlp.Encode(w, &receiptRLP{status, r.CumulativeGasUsed, r.Bloom, r.Logs})
}

// borPreFork reports whether block n of this chain may carry a pre-fork bor receipt.
func (d *Decoder) borPreFork(n uint64) bool {
	f, ok := borMadhugiri[d.cfg.ChainID]
	return ok && n < f
}

// gethStoredReceipt is the receipt storage encoding of go-ethereum >= 1.9 and its forks (bor, linea-geth):
// [status or post-state root, cumulative gas, logs]. Nitro's storage encoding adds a required L1 gas field after the
// cumulative gas, so its decoder rejects these.
type gethStoredReceipt struct {
	PostStateOrStatus []byte
	CumulativeGasUsed uint64
	Logs              []*types.Log
}

func decodeGethStoredReceipt(blob []byte) (*types.ReceiptForStorage, error) {
	var s gethStoredReceipt
	if err := rlp.DecodeBytes(blob, &s); err != nil {
		return nil, err
	}
	r := &types.Receipt{CumulativeGasUsed: s.CumulativeGasUsed, Logs: s.Logs}
	switch {
	case bytes.Equal(s.PostStateOrStatus, []byte{0x01}):
		r.Status = types.ReceiptStatusSuccessful
	case len(s.PostStateOrStatus) == 0:
		r.Status = types.ReceiptStatusFailed
	case len(s.PostStateOrStatus) == 32:
		r.PostState = s.PostStateOrStatus
	default:
		return nil, fmt.Errorf("invalid receipt status %x", s.PostStateOrStatus)
	}
	return (*types.ReceiptForStorage)(r), nil
}

// storedReceipt decodes one stored receipt: on geth-family chains geth's layout first, then (receipts rebuilt from
// RPC by rpcsrc.Convert, and every other family) Nitro's.
func (d *Decoder) storedReceipt(blob []byte) (*types.ReceiptForStorage, error) {
	if d.cfg.Family == "geth" {
		if r, err := decodeGethStoredReceipt(blob); err == nil {
			return r, nil
		}
	}
	var r types.ReceiptForStorage
	if err := rlp.DecodeBytes(blob, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

// storedReceipts decodes a block's stored receipt list.
func (d *Decoder) storedReceipts(b []byte) ([]*types.ReceiptForStorage, error) {
	if d.cfg.Family != "geth" {
		var stored []*types.ReceiptForStorage
		err := rlp.DecodeBytes(b, &stored)
		return stored, err
	}
	var blobs []rlp.RawValue
	if err := rlp.DecodeBytes(b, &blobs); err != nil {
		return nil, err
	}
	stored := make([]*types.ReceiptForStorage, len(blobs))
	for i, blob := range blobs {
		r, err := d.storedReceipt(blob)
		if err != nil {
			return nil, fmt.Errorf("receipt %d: %w", i, err)
		}
		stored[i] = r
	}
	return stored, nil
}
