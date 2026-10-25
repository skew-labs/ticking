// Package rpcsrc is a block source backed by Nitro JSON-RPC
// (eth_getBlockByNumber(full) + eth_getBlockReceipts). Each block is rebuilt
// into the exact raw database representation (header RLP, body RLP, storage
// receipts RLP) with the OffchainLabs fork types, so the production decoder
// verifies block hash, tx root, receipt root and bloom and writes the same
// rows as em-extract does from a snapshot.
package rpcsrc

import (
	"encoding/json"
	"fmt"
	"math/big"

	"github.com/ethereum/go-ethereum/arbitrum/multigas"
	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/common/hexutil"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/em-mapping/extractor/internal/chaindb"
)

// ConvertError is a block-level conversion failure (bad or inconsistent RPC
// data). It is retried on another endpoint and fails the chunk if persistent.
type ConvertError struct {
	Block  uint64
	Reason string
	Detail string
}

func (e *ConvertError) Error() string {
	return fmt.Sprintf("block %d: %s: %s", e.Block, e.Reason, e.Detail)
}

// Conversion failure reasons.
const (
	ReasonRPCShape    = "RPC_SHAPE"     // JSON does not decode / missing fields
	ReasonRPCTxHash   = "RPC_TX_HASH"   // rebuilt tx hash != reported hash
	ReasonRPCMismatch = "RPC_MISMATCH"  // receipts do not belong to the block
	ReasonRPCUncles   = "RPC_UNCLES"    // uncle headers are not served by these calls
	ReasonRPCNotFound = "RPC_NOT_FOUND" // null result (endpoint does not have the block yet)
)

func cerr(n uint64, reason, f string, a ...any) *ConvertError {
	return &ConvertError{Block: n, Reason: reason, Detail: fmt.Sprintf(f, a...)}
}

type rpcBlock struct {
	Hash         common.Hash       `json:"hash"`
	Number       hexutil.Uint64    `json:"number"`
	Transactions []json.RawMessage `json:"transactions"`
	Uncles       []common.Hash     `json:"uncles"`
}

// txHead is decoded for every tx (cheap); the full legacy shape only for
// Classic (type 120) txs.
type txHead struct {
	Hash common.Hash    `json:"hash"`
	Type hexutil.Uint64 `json:"type"`
}

type rpcLegacyTx struct {
	Hash     common.Hash     `json:"hash"`
	From     common.Address  `json:"from"`
	To       *common.Address `json:"to"`
	Nonce    hexutil.Uint64  `json:"nonce"`
	Gas      hexutil.Uint64  `json:"gas"`
	GasPrice *hexutil.Big    `json:"gasPrice"`
	Value    *hexutil.Big    `json:"value"`
	Input    hexutil.Bytes   `json:"input"`
	V        *hexutil.Big    `json:"v"`
	R        *hexutil.Big    `json:"r"`
	S        *hexutil.Big    `json:"s"`
}

type rpcLog struct {
	Address common.Address `json:"address"`
	Topics  []common.Hash  `json:"topics"`
	Data    hexutil.Bytes  `json:"data"`
}

type rpcReceipt struct {
	Type              hexutil.Uint64     `json:"type"`
	Status            *hexutil.Uint64    `json:"status"`
	Root              hexutil.Bytes      `json:"root"`
	CumulativeGasUsed hexutil.Uint64     `json:"cumulativeGasUsed"`
	GasUsed           hexutil.Uint64     `json:"gasUsed"`
	GasUsedForL1      hexutil.Uint64     `json:"gasUsedForL1"`
	EffectiveGasPrice *hexutil.Big       `json:"effectiveGasPrice"`
	ContractAddress   *common.Address    `json:"contractAddress"`
	TxHash            common.Hash        `json:"transactionHash"`
	TxIndex           hexutil.Uint64     `json:"transactionIndex"`
	BlockHash         common.Hash        `json:"blockHash"`
	L1BlockNumber     *hexutil.Uint64    `json:"l1BlockNumber"`
	Logs              []rpcLog           `json:"logs"`
	MultiGasUsed      *multigas.MultiGas `json:"multiGasUsed"`
}

func isNull(b []byte) bool {
	return len(b) == 0 || string(b) == "null"
}

// Convert rebuilds the raw database representation of block n from the
// eth_getBlockByNumber(n, true) and eth_getBlockReceipts(n) results. It
// checks only structural consistency (number, per-tx hash, receipt <-> tx
// pairing); the cryptographic verification (keccak(header) == hash, tx root,
// receipt root, bloom, parent link) is done by the decoder.
func Convert(signer types.Signer, n uint64, blockJSON, receiptsJSON []byte) (*chaindb.RawBlock, error) {
	return ConvertWith(signer, n, blockJSON, receiptsJSON, nil)
}

// borStateSyncTxType is Polygon bor's PIP-74 state-sync tx (in block bodies from ~80,084,800). Its RPC JSON
// (zero from/to/sig, input 0x, no stateSyncData) cannot rebuild the tx, so the canonical bytes come from
// eth_getRawTransactionByBlockNumberAndIndex via fetchRaw. The decoder (internal/decode/bor.go) turns the raw RLP
// string in the body into the state-sync row and covers it by TX_ROOT and the receipt root.
const borStateSyncTxType = 0x7f

// ConvertWith is Convert with fetchRaw, a fetcher for raw tx bytes (needed only for type 0x7f; nil = unsupported).
func ConvertWith(signer types.Signer, n uint64, blockJSON, receiptsJSON []byte, fetchRaw func(i int) ([]byte, error)) (*chaindb.RawBlock, error) {
	if isNull(blockJSON) || isNull(receiptsJSON) {
		return nil, cerr(n, ReasonRPCNotFound, "block=%v receipts=%v null", isNull(blockJSON), isNull(receiptsJSON))
	}
	var hdr types.Header
	if err := json.Unmarshal(blockJSON, &hdr); err != nil {
		return nil, cerr(n, ReasonRPCShape, "header: %v", err)
	}
	var blk rpcBlock
	if err := json.Unmarshal(blockJSON, &blk); err != nil {
		return nil, cerr(n, ReasonRPCShape, "block: %v", err)
	}
	if uint64(blk.Number) != n || hdr.Number == nil || !hdr.Number.IsUint64() || hdr.Number.Uint64() != n {
		return nil, cerr(n, ReasonRPCMismatch, "endpoint returned block %d", uint64(blk.Number))
	}
	if len(blk.Uncles) > 0 {
		return nil, cerr(n, ReasonRPCUncles, "%d uncles", len(blk.Uncles))
	}
	var rcs []rpcReceipt
	if err := json.Unmarshal(receiptsJSON, &rcs); err != nil {
		return nil, cerr(n, ReasonRPCShape, "receipts: %v", err)
	}
	if len(rcs) != len(blk.Transactions) {
		return nil, cerr(n, ReasonRPCMismatch, "%d txs vs %d receipts", len(blk.Transactions), len(rcs))
	}
	txs := make([]*types.Transaction, len(blk.Transactions))
	var raws map[int][]byte // index -> canonical 2718 bytes of 0x7f txs
	for i, rawTx := range blk.Transactions {
		rc := &rcs[i]
		if rc.BlockHash != blk.Hash || uint64(rc.TxIndex) != uint64(i) {
			return nil, cerr(n, ReasonRPCMismatch, "receipt %d: blockHash %s index %d (block %s)", i, rc.BlockHash, uint64(rc.TxIndex), blk.Hash)
		}
		var th txHead
		if err := json.Unmarshal(rawTx, &th); err != nil {
			return nil, cerr(n, ReasonRPCShape, "tx %d: %v", i, err)
		}
		if rc.TxHash != th.Hash || uint64(rc.Type) != uint64(th.Type) {
			return nil, cerr(n, ReasonRPCMismatch, "receipt %d: tx %s type %d vs receipt %s type %d", i, th.Hash, uint64(th.Type), rc.TxHash, uint64(rc.Type))
		}
		if uint8(th.Type) == borStateSyncTxType {
			if fetchRaw == nil {
				return nil, cerr(n, ReasonRPCShape, "tx %d (type 0x7f): state-sync tx needs raw bytes", i)
			}
			rb, err := fetchRaw(i)
			if err != nil {
				return nil, cerr(n, ReasonRPCShape, "tx %d (type 0x7f) raw: %v", i, err)
			}
			if len(rb) == 0 || rb[0] != borStateSyncTxType || crypto.Keccak256Hash(rb) != th.Hash {
				return nil, cerr(n, ReasonRPCTxHash, "tx %d (type 0x7f): raw bytes do not hash to %s", i, th.Hash)
			}
			if raws == nil {
				raws = map[int][]byte{}
			}
			raws[i] = rb
			continue
		}
		if uint8(th.Type) == types.ArbitrumLegacyTxType {
			// Classic tx JSON omits EffectiveGasPrice / L1BlockNumber; Nitro's
			// receipt carries them (sourced from ArbitrumLegacyTxData).
			var rt rpcLegacyTx
			if err := json.Unmarshal(rawTx, &rt); err != nil {
				return nil, cerr(n, ReasonRPCShape, "classic tx %d: %v", i, err)
			}
			inner := types.LegacyTx{Nonce: uint64(rt.Nonce), GasPrice: (*big.Int)(rt.GasPrice), Gas: uint64(rt.Gas), To: rt.To, Value: (*big.Int)(rt.Value), Data: rt.Input, V: (*big.Int)(rt.V), R: (*big.Int)(rt.R), S: (*big.Int)(rt.S)}
			if inner.GasPrice == nil || inner.Value == nil || inner.V == nil || inner.R == nil || inner.S == nil {
				return nil, cerr(n, ReasonRPCShape, "classic tx %d: missing legacy fields", i)
			}
			l1 := uint64(0)
			if rc.L1BlockNumber != nil {
				l1 = uint64(*rc.L1BlockNumber)
			}
			egp := uint64(0)
			if rc.EffectiveGasPrice != nil {
				if !rc.EffectiveGasPrice.ToInt().IsUint64() {
					return nil, cerr(n, ReasonRPCShape, "classic tx %d: effectiveGasPrice overflow", i)
				}
				egp = rc.EffectiveGasPrice.ToInt().Uint64()
			}
			var sender *common.Address
			if got, err := types.Sender(signer, types.NewTx(&inner)); err != nil || got != rt.From {
				s := rt.From
				sender = &s // unsigned Classic tx (e.g. L1->L2): sender override
			}
			tx, err := types.NewArbitrumLegacyTx(types.NewTx(&inner), rt.Hash, egp, l1, sender)
			if err != nil {
				return nil, cerr(n, ReasonRPCShape, "classic tx %d: %v", i, err)
			}
			txs[i] = tx
		} else {
			tx := new(types.Transaction)
			if err := tx.UnmarshalJSON(rawTx); err != nil {
				return nil, cerr(n, ReasonRPCShape, "tx %d (type %d): %v", i, uint64(th.Type), err)
			}
			txs[i] = tx
		}
		if h := txs[i].Hash(); h != th.Hash {
			return nil, cerr(n, ReasonRPCTxHash, "tx %d (type %d): rebuilt %s, reported %s", i, uint64(th.Type), h, th.Hash)
		}
	}
	stored := make([]*types.ReceiptForStorage, len(rcs))
	for i := range rcs {
		rc := &rcs[i]
		r := &types.Receipt{Type: uint8(rc.Type), CumulativeGasUsed: uint64(rc.CumulativeGasUsed), GasUsedForL1: uint64(rc.GasUsedForL1)}
		switch {
		case rc.Status != nil:
			r.Status = uint64(*rc.Status)
		case len(rc.Root) > 0:
			r.PostState = rc.Root
		default:
			return nil, cerr(n, ReasonRPCShape, "receipt %d: neither status nor root", i)
		}
		if len(rc.Logs) > 0 {
			r.Logs = make([]*types.Log, len(rc.Logs))
			for k := range rc.Logs {
				l := &rc.Logs[k]
				r.Logs[k] = &types.Log{Address: l.Address, Topics: l.Topics, Data: l.Data}
			}
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
		if rc.MultiGasUsed != nil {
			r.MultiGasUsed = *rc.MultiGasUsed
		}
		stored[i] = (*types.ReceiptForStorage)(r)
	}
	hraw, err := rlp.EncodeToBytes(&hdr)
	if err != nil {
		return nil, cerr(n, ReasonRPCShape, "header rlp: %v", err)
	}
	var braw []byte
	if len(raws) == 0 {
		braw, err = rlp.EncodeToBytes(&types.Body{Transactions: txs})
	} else {
		// same layout as types.Body ([txs, uncles], no withdrawals); a 0x7f tx is an RLP string of its raw bytes
		elems := make([]rlp.RawValue, len(txs))
		for i := range txs {
			var e []byte
			if rb, ok := raws[i]; ok {
				e, err = rlp.EncodeToBytes(rb)
			} else {
				e, err = rlp.EncodeToBytes(txs[i])
			}
			if err != nil {
				break
			}
			elems[i] = e
		}
		if err == nil {
			braw, err = rlp.EncodeToBytes([]any{elems, []rlp.RawValue{}})
		}
	}
	if err != nil {
		return nil, cerr(n, ReasonRPCShape, "body rlp: %v", err)
	}
	rraw, err := rlp.EncodeToBytes(stored)
	if err != nil {
		return nil, cerr(n, ReasonRPCShape, "receipts rlp: %v", err)
	}
	return &chaindb.RawBlock{Number: n, Hash: blk.Hash, Header: hraw, Body: braw, Receipts: rraw}, nil
}
