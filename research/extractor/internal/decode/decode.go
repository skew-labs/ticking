// Package decode turns raw canonical block RLP (header, body, storage
// receipts) into Parquet rows, verifying header hash, parent link, tx root,
// receipt root, uncle hash and log bloom on the way.
package decode

import (
	"bytes"
	"fmt"
	"math/big"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/schema"
)

// Failure reasons (stable strings, used in manifests).
const (
	ReasonMissing      = "MISSING_DATA"
	ReasonHeaderRLP    = "HEADER_RLP"
	ReasonHeaderNumber = "HEADER_NUMBER"
	ReasonHeaderHash   = "HEADER_HASH"
	ReasonParent       = "PARENT_HASH"
	ReasonBodyRLP      = "BODY_RLP"
	ReasonReceiptRLP   = "RECEIPT_RLP"
	ReasonReceiptCount = "RECEIPT_COUNT"
	ReasonTxRoot       = "TX_ROOT"
	ReasonReceiptRoot  = "RECEIPT_ROOT"
	ReasonUncleHash    = "UNCLE_HASH"
	ReasonLogBloom     = "LOG_BLOOM"
	ReasonSender       = "SENDER"
	ReasonGas          = "RECEIPT_GAS"
	ReasonOverflow     = "UINT64_OVERFLOW"
	ReasonLogTopics    = "LOG_TOPICS"
)

// BlockError describes why a block failed decoding or verification.
type BlockError struct {
	Block  uint64 `json:"block"`
	Reason string `json:"reason"`
	Detail string `json:"detail,omitempty"`
}

func (e *BlockError) Error() string {
	return fmt.Sprintf("block %d: %s: %s", e.Block, e.Reason, e.Detail)
}

func fail(n uint64, reason, format string, a ...any) *BlockError {
	return &BlockError{Block: n, Reason: reason, Detail: fmt.Sprintf(format, a...)}
}

// Config controls decoding.
type Config struct {
	ChainID      uint64
	NitroGenesis uint64 // first Nitro block; lower numbers are Classic (legacy-imported)
	Verify       bool
	VerifyBloom  bool
	WETH         *WETHFilter
	// SenderErrorZero records the zero address (like Nitro's RPC does) instead
	// of failing the chunk when signature recovery fails; such txs are counted
	// in Stats.SenderErrors and listed in Stats.SenderErrorTxs (capped).
	SenderErrorZero bool
	// Family "geth" (Ethereum-style L1/L2 such as Linea or Polygon bor): no Arbitrum header info,
	// effective gas price = standard RPC semantics. Empty or "arbitrum" keeps the Nitro behaviour.
	Family string
}

// Stats are per-chunk counters.
type Stats struct {
	Blocks        uint64            `json:"blocks"`
	ClassicBlocks uint64            `json:"classic_blocks"`
	KVBlocks      uint64            `json:"kv_blocks"`
	Txs           uint64            `json:"transactions"`
	FailedTxs     uint64            `json:"failed_transactions"`
	Logs          uint64            `json:"logs"`
	TxTypes       map[string]uint64 `json:"tx_types"`
	Token         TokenStats        `json:"token_decode"`
	RLPBytes      uint64            `json:"rlp_bytes"`
	DiskBytes     uint64            `json:"disk_bytes"`
	SenderErrors  uint64            `json:"sender_errors"`
	SenderErrTxs  []string          `json:"sender_error_txs,omitempty"`
}

// Merge adds o into s.
func (s *Stats) Merge(o *Stats) {
	s.Blocks += o.Blocks
	s.ClassicBlocks += o.ClassicBlocks
	s.KVBlocks += o.KVBlocks
	s.Txs += o.Txs
	s.FailedTxs += o.FailedTxs
	s.Logs += o.Logs
	s.RLPBytes += o.RLPBytes
	s.SenderErrors += o.SenderErrors
	s.SenderErrTxs = append(s.SenderErrTxs, o.SenderErrTxs...)
	s.DiskBytes += o.DiskBytes
	s.Token.Malformed += o.Token.Malformed
	s.Token.Unrecognized += o.Token.Unrecognized
	s.Token.SkippedWETH += o.Token.SkippedWETH
	if s.TxTypes == nil {
		s.TxTypes = map[string]uint64{}
	}
	for k, v := range o.TxTypes {
		s.TxTypes[k] += v
	}
}

// Rows accumulates output rows.
type Rows struct {
	Blocks    []schema.Block
	Txs       []schema.Tx
	Logs      []schema.Log
	Transfers []schema.TokenTransfer
	Creations []schema.ContractCreation
}

// Reset truncates all slices, keeping capacity.
func (r *Rows) Reset() {
	r.Blocks = r.Blocks[:0]
	r.Txs = r.Txs[:0]
	r.Logs = r.Logs[:0]
	r.Transfers = r.Transfers[:0]
	r.Creations = r.Creations[:0]
}

// Decoder is NOT safe for concurrent use; use one per worker.
type Decoder struct {
	cfg    Config
	signer types.Signer
	hasher *trie.StackTrie
	// LastClassicL1 carries the most recent Classic L1 block number, used for
	// Classic blocks without transactions (mirrors Nitro's RPC walk-back).
	LastClassicL1 *uint64
}

// New creates a decoder.
func New(cfg Config) *Decoder {
	return &Decoder{
		cfg:    cfg,
		signer: types.LatestSignerForChainID(new(big.Int).SetUint64(cfg.ChainID)),
		hasher: trie.NewStackTrie(nil),
	}
}

// Signer exposes the signer (arbitrumSigner wrapping the latest signer).
func (d *Decoder) Signer() types.Signer { return d.signer }

func u64ptr(v uint64) *uint64 { return &v }

func bigU64(n uint64, field string, v *big.Int) (*uint64, error) {
	if v == nil {
		return nil, nil
	}
	if v.Sign() < 0 || !v.IsUint64() {
		return nil, fail(n, ReasonOverflow, "%s=%s does not fit uint64", field, v)
	}
	return u64ptr(v.Uint64()), nil
}

func be32(v *big.Int) (o [32]byte) {
	if v != nil && v.Sign() > 0 {
		v.FillBytes(o[:])
	}
	return
}

// ClassicL1 returns the L1 block number carried by the first Classic tx.
func ClassicL1(txs []*types.Transaction) (uint64, bool) {
	if len(txs) == 0 {
		return 0, false
	}
	if lt, ok := txs[0].GetInner().(*types.ArbitrumLegacyTxData); ok {
		return lt.L1BlockNumber, true
	}
	return 0, false
}

// SenderOf returns the sender using the Arbitrum-aware signer (explicit From
// for Arbitrum system types, Sender override for Classic legacy txs, ECDSA
// recovery otherwise).
func SenderOf(signer types.Signer, tx *types.Transaction) (common.Address, error) {
	return types.Sender(signer, tx)
}

// Block decodes one block. prevHash, when non-nil, is the expected parent hash.
// It returns the block hash.
func (d *Decoder) Block(raw *chaindb.RawBlock, prevHash *common.Hash, rows *Rows, st *Stats) (common.Hash, error) {
	n := raw.Number
	if len(raw.Header) == 0 || len(raw.Body) == 0 || len(raw.Receipts) == 0 {
		return common.Hash{}, fail(n, ReasonMissing, "header=%d body=%d receipts=%d bytes", len(raw.Header), len(raw.Body), len(raw.Receipts))
	}
	var h types.Header
	if err := rlp.DecodeBytes(raw.Header, &h); err != nil {
		return common.Hash{}, fail(n, ReasonHeaderRLP, "%v", err)
	}
	if h.Number == nil || !h.Number.IsUint64() || h.Number.Uint64() != n {
		return common.Hash{}, fail(n, ReasonHeaderNumber, "header number %v", h.Number)
	}
	hash := raw.Hash
	if d.cfg.Verify {
		got := crypto.Keccak256Hash(raw.Header)
		if got != raw.Hash {
			return common.Hash{}, fail(n, ReasonHeaderHash, "keccak(header)=%s canonical=%s", got, raw.Hash)
		}
		if prevHash != nil && h.ParentHash != *prevHash {
			return common.Hash{}, fail(n, ReasonParent, "parent=%s expected=%s", h.ParentHash, *prevHash)
		}
	}
	var body types.Body
	var canon [][]byte       // canonical tx bytes, only on the tx-by-tx path
	var ss map[int]stateSync // bor state-sync txs by index (bor.go)
	if err := rlp.DecodeBytes(raw.Body, &body); err != nil {
		// go-ethereum rejects bor's state-sync tx type (0x7f): decode tx by tx
		txs, uncles, c, s, err2 := decodeBodyTxByTx(raw.Body)
		if err2 != nil || s == nil {
			return common.Hash{}, fail(n, ReasonBodyRLP, "%v", err)
		}
		body.Transactions, body.Uncles, canon, ss = txs, uncles, c, s
	}
	stored, err := d.storedReceipts(raw.Receipts)
	if err != nil {
		return common.Hash{}, fail(n, ReasonReceiptRLP, "%v", err)
	}
	txs := body.Transactions
	if len(stored) != len(txs) {
		return common.Hash{}, fail(n, ReasonReceiptCount, "txs=%d receipts=%d", len(txs), len(stored))
	}
	if d.cfg.Verify {
		var got common.Hash
		if canon != nil {
			got = types.DeriveSha(canonList(canon), d.hasher)
		} else {
			got = types.DeriveSha(types.Transactions(txs), d.hasher)
		}
		if got != h.TxHash {
			return common.Hash{}, fail(n, ReasonTxRoot, "derived=%s header=%s", got, h.TxHash)
		}
		if got := types.CalcUncleHash(body.Uncles); got != h.UncleHash {
			return common.Hash{}, fail(n, ReasonUncleHash, "derived=%s header=%s", got, h.UncleHash)
		}
	}
	nRoot := len(txs) // txs and receipts in the roots; a pre-fork bor receipt is appended after them
	if ss == nil && len(raw.BorReceipt) > 0 && d.borPreFork(n) {
		br, err := d.storedReceipt(raw.BorReceipt)
		if err != nil {
			return common.Hash{}, fail(n, ReasonReceiptRLP, "bor receipt: %v", err)
		}
		txs = append(txs, stateSyncPlaceholder())
		stored = append(stored, br)
		ss = map[int]stateSync{nRoot: {hash: crypto.Keccak256Hash(chaindb.BorReceiptKey(n, hash)), appended: true}}
	}
	isClassic := n < d.cfg.NitroGenesis
	receipts := make(types.Receipts, len(txs))
	for i := range stored {
		receipts[i] = (*types.Receipt)(stored[i])
	}

	// Header-level Arbitrum info.
	var collectTips bool
	blk := schema.Block{
		Number:     n,
		Hash:       hash,
		ParentHash: h.ParentHash,
		Timestamp:  h.Time,
		TxCount:    uint32(len(txs)),
		GasUsed:    h.GasUsed,
		GasLimit:   h.GasLimit,
		Miner:      h.Coinbase,
		IsClassic:  isClassic,
		Size:       uint32(raw.Size()),
	}
	if blk.BaseFee, err = bigU64(n, "base_fee", h.BaseFee); err != nil {
		return common.Hash{}, err
	}
	if isClassic {
		if l1, ok := ClassicL1(txs); ok {
			d.LastClassicL1 = u64ptr(l1)
		}
		if d.LastClassicL1 != nil {
			blk.L1BlockNumber = u64ptr(*d.LastClassicL1)
		}
	} else if d.cfg.Family == "geth" {
		collectTips = true
	} else {
		info := types.DeserializeHeaderExtraInformation(&h)
		blk.L1BlockNumber = u64ptr(info.L1BlockNumber)
		blk.ArbOSVersion = u64ptr(info.ArbOSFormatVersion)
		blk.SendCount = u64ptr(info.SendCount)
		collectTips = info.CollectTips
	}

	if st.TxTypes == nil {
		st.TxTypes = map[string]uint64{}
	}
	var prevCum uint64
	logIndex := uint32(0)
	for i, tx := range txs {
		r := receipts[i]
		sync, isSync := ss[i]
		typ := tx.Type()
		if isSync {
			typ = StateSyncTxType
		}
		r.Type = typ
		if r.Status > 1 {
			return common.Hash{}, fail(n, ReasonReceiptRLP, "tx %d status %d", i, r.Status)
		}
		var from common.Address
		var err error
		if isSync {
			from = BorSystemAddress
		} else if from, err = SenderOf(d.signer, tx); err != nil {
			if !d.cfg.SenderErrorZero {
				return common.Hash{}, fail(n, ReasonSender, "tx %d (%s type %d): %v", i, tx.Hash(), tx.Type(), err)
			}
			from = common.Address{}
			st.SenderErrors++
			if len(st.SenderErrTxs) < 100 {
				st.SenderErrTxs = append(st.SenderErrTxs, fmt.Sprintf("%d:%d:%s", n, i, tx.Hash()))
			}
		}
		legacy, isArbLegacy := tx.GetInner().(*types.ArbitrumLegacyTxData)
		var gasUsed uint64
		if isArbLegacy {
			gasUsed = r.GasUsed
		} else if sync.appended {
			gasUsed = 0 // the pre-fork bor receipt repeats the block's cumulative gas
		} else {
			if r.CumulativeGasUsed < prevCum {
				return common.Hash{}, fail(n, ReasonGas, "tx %d cumulative gas %d < previous %d", i, r.CumulativeGasUsed, prevCum)
			}
			gasUsed = r.CumulativeGasUsed - prevCum
		}
		prevCum = r.CumulativeGasUsed

		// Effective gas price, mirroring Nitro's RPC receipt marshalling.
		var egp *uint64
		switch {
		case isArbLegacy:
			egp = u64ptr(legacy.EffectiveGasPrice)
		case h.BaseFee == nil && d.cfg.Family == "geth":
			if egp, err = bigU64(n, "effective_gas_price", tx.GasPrice()); err != nil {
				return common.Hash{}, err
			}
		case h.BaseFee == nil:
			egp = nil
		case collectTips:
			if egp, err = bigU64(n, "effective_gas_price", tx.EffectiveGasPrice(h.BaseFee)); err != nil {
				return common.Hash{}, err
			}
		default:
			egp = blk.BaseFee
		}

		// Contract address, mirroring Receipt.DeriveFields.
		var contract common.Address
		if isArbLegacy {
			contract = r.ContractAddress
		} else if tx.To() == nil {
			contract = r.ContractAddress
			if contract == (common.Address{}) {
				contract = crypto.CreateAddress(from, tx.Nonce())
			}
		}
		txHash := tx.Hash()
		if isSync {
			txHash = sync.hash
		}
		row := schema.Tx{
			BlockNumber:       n,
			TxIndex:           uint32(i),
			Hash:              txHash,
			Type:              typ,
			From:              from,
			Value:             be32(tx.Value()),
			Nonce:             tx.Nonce(),
			GasLimit:          tx.Gas(),
			GasUsed:           gasUsed,
			Status:            uint8(r.Status),
			EffectiveGasPrice: egp,
			GasUsedForL1:      r.GasUsedForL1,
			LogCount:          uint32(len(r.Logs)),
		}
		if to := tx.To(); to != nil {
			a := [20]byte(*to)
			row.To = &a
		}
		if contract != (common.Address{}) {
			a := [20]byte(contract)
			row.ContractAddress = &a
		}
		if data := tx.Data(); len(data) > 0 {
			row.InputSize = uint32(len(data))
			if len(data) >= 4 {
				m := [4]byte(data[:4])
				row.MethodID = &m
			}
		}
		if srt, ok := tx.GetInner().(*types.ArbitrumSubmitRetryableTx); ok && srt.DepositValue != nil {
			// tx.Value() is 0 for submit-retryable; the L1 deposit is DepositValue.
			dv := be32(srt.DepositValue)
			row.DepositValue = &dv
		}
		rows.Txs = append(rows.Txs, row)
		st.TxTypes[fmt.Sprint(typ)]++
		st.Txs++
		if r.Status != types.ReceiptStatusSuccessful {
			st.FailedTxs++
		}
		if r.Status == types.ReceiptStatusSuccessful && contract != (common.Address{}) {
			rows.Creations = append(rows.Creations, schema.ContractCreation{BlockNumber: n, TxIndex: uint32(i), Address: contract, Creator: from, TxType: typ})
		}
		for _, l := range r.Logs {
			if len(l.Topics) > 4 {
				return common.Hash{}, fail(n, ReasonLogTopics, "tx %d log %d has %d topics", i, logIndex, len(l.Topics))
			}
			lr := schema.Log{BlockNumber: n, TxIndex: uint32(i), LogIndex: logIndex, Address: l.Address, TopicCount: uint8(len(l.Topics)), Data: l.Data}
			for k, t := range l.Topics {
				tv := [32]byte(t)
				switch k {
				case 0:
					lr.Topic0 = &tv
				case 1:
					lr.Topic1 = &tv
				case 2:
					lr.Topic2 = &tv
				case 3:
					lr.Topic3 = &tv
				}
			}
			rows.Logs = append(rows.Logs, lr)
			if r.Status == types.ReceiptStatusSuccessful {
				rows.Transfers = DecodeTokenLog(rows.Transfers, n, uint32(i), logIndex, l.Address, l.Topics, l.Data, d.cfg.WETH, &st.Token)
			}
			logIndex++
		}
		st.Logs += uint64(len(r.Logs))
	}
	if d.cfg.Verify {
		for _, r := range receipts {
			r.Bloom = types.CreateBloom(r)
		}
		var got common.Hash
		if ss != nil {
			got = types.DeriveSha(borReceipts(receipts[:nRoot]), d.hasher)
		} else {
			got = types.DeriveSha(receipts, d.hasher)
		}
		if got != h.ReceiptHash {
			return common.Hash{}, fail(n, ReasonReceiptRoot, "derived=%s header=%s", got, h.ReceiptHash)
		}
		if d.cfg.VerifyBloom {
			if got := types.MergeBloom(receipts[:nRoot]); !bytes.Equal(got[:], h.Bloom[:]) {
				return common.Hash{}, fail(n, ReasonLogBloom, "derived bloom differs from header")
			}
		}
	}
	rows.Blocks = append(rows.Blocks, blk)
	st.Blocks++
	if isClassic {
		st.ClassicBlocks++
	}
	if raw.FromKV {
		st.KVBlocks++
	}
	st.RLPBytes += uint64(raw.Size())
	return hash, nil
}
