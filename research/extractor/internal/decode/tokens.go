package decode

import (
	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"

	"github.com/em-mapping/extractor/internal/schema"
)

// Event signatures (topic0).
var (
	TopicTransfer       = crypto.Keccak256Hash([]byte("Transfer(address,address,uint256)"))
	TopicTransferSingle = crypto.Keccak256Hash([]byte("TransferSingle(address,address,address,uint256,uint256)"))
	TopicTransferBatch  = crypto.Keccak256Hash([]byte("TransferBatch(address,address,address,uint256[],uint256[])"))
	TopicDeposit        = crypto.Keccak256Hash([]byte("Deposit(address,uint256)"))
	TopicWithdrawal     = crypto.Keccak256Hash([]byte("Withdrawal(address,uint256)"))
)

// MaxBatchItems bounds one ERC1155 TransferBatch decode (defensive; data size
// already bounds it, this just prevents pathological allocations).
const MaxBatchItems = 1 << 16

// TokenStats counts decode outcomes for the manifest.
type TokenStats struct {
	Unrecognized uint64 `json:"transfer_shape_unrecognized"` // topic0 matched, shape did not
	Malformed    uint64 `json:"malformed"`                   // dirty address padding / bad ABI offsets
	SkippedWETH  uint64 `json:"weth_events_not_allowlisted"`
}

// WETHFilter decides which emitters' Deposit/Withdrawal events are decoded.
type WETHFilter struct {
	Any   bool
	Allow map[common.Address]bool
}

func (w *WETHFilter) allowed(a common.Address) bool {
	if w == nil {
		return false
	}
	return w.Any || w.Allow[a]
}

// topicAddr extracts an address from a 32-byte topic/word, requiring clean
// zero padding of the upper 12 bytes.
func topicAddr(t []byte) (a [20]byte, ok bool) {
	for _, b := range t[:12] {
		if b != 0 {
			return a, false
		}
	}
	copy(a[:], t[12:32])
	return a, true
}

func word(b []byte, i int) []byte { return b[i*32 : i*32+32] }

func wordUint(b []byte) (uint64, bool) {
	for _, x := range b[:24] {
		if x != 0 {
			return 0, false
		}
	}
	var v uint64
	for _, x := range b[24:32] {
		v = v<<8 | uint64(x)
	}
	return v, true
}

var oneBE = func() (o [32]byte) { o[31] = 1; return }()

// DecodeTokenLog appends token transfer rows decoded from one log. The caller
// guarantees the log belongs to a successful transaction.
func DecodeTokenLog(dst []schema.TokenTransfer, blockNum uint64, txIndex, logIndex uint32, addr common.Address, topics []common.Hash, data []byte, weth *WETHFilter, st *TokenStats) []schema.TokenTransfer {
	if len(topics) == 0 {
		return dst
	}
	base := schema.TokenTransfer{BlockNumber: blockNum, TxIndex: txIndex, LogIndex: logIndex, Token: addr}
	t0 := topics[0]
	switch t0 {
	case TopicTransfer:
		switch {
		case len(topics) == 3 && len(data) == 32:
			from, ok1 := topicAddr(topics[1][:])
			to, ok2 := topicAddr(topics[2][:])
			if !ok1 || !ok2 {
				st.Malformed++
				return dst
			}
			r := base
			r.Standard = schema.StdERC20
			r.From, r.To = from, to
			copy(r.Amount[:], data)
			return append(dst, r)
		case len(topics) == 4 && len(data) == 0:
			from, ok1 := topicAddr(topics[1][:])
			to, ok2 := topicAddr(topics[2][:])
			if !ok1 || !ok2 {
				st.Malformed++
				return dst
			}
			r := base
			r.Standard = schema.StdERC721
			r.From, r.To = from, to
			id := [32]byte(topics[3])
			r.TokenID = &id
			r.Amount = oneBE
			return append(dst, r)
		default:
			st.Unrecognized++
			return dst
		}
	case TopicTransferSingle:
		if len(topics) != 4 || len(data) != 64 {
			st.Unrecognized++
			return dst
		}
		op, ok0 := topicAddr(topics[1][:])
		from, ok1 := topicAddr(topics[2][:])
		to, ok2 := topicAddr(topics[3][:])
		if !ok0 || !ok1 || !ok2 {
			st.Malformed++
			return dst
		}
		r := base
		r.Standard = schema.StdERC1155Single
		r.From, r.To, r.Operator = from, to, &op
		var id [32]byte
		copy(id[:], data[:32])
		r.TokenID = &id
		copy(r.Amount[:], data[32:64])
		return append(dst, r)
	case TopicTransferBatch:
		if len(topics) != 4 || len(data) < 128 || len(data)%32 != 0 {
			st.Unrecognized++
			return dst
		}
		op, ok0 := topicAddr(topics[1][:])
		from, ok1 := topicAddr(topics[2][:])
		to, ok2 := topicAddr(topics[3][:])
		if !ok0 || !ok1 || !ok2 {
			st.Malformed++
			return dst
		}
		ids, okI := abiUintArray(data, 0)
		vals, okV := abiUintArray(data, 1)
		if !okI || !okV || len(ids) != len(vals) {
			st.Malformed++
			return dst
		}
		for i := range ids {
			r := base
			r.Standard = schema.StdERC1155Batch
			r.BatchIndex = uint32(i)
			r.From, r.To = from, to
			o := op
			r.Operator = &o
			var id [32]byte
			copy(id[:], ids[i])
			r.TokenID = &id
			copy(r.Amount[:], vals[i])
			dst = append(dst, r)
		}
		return dst
	case TopicDeposit, TopicWithdrawal:
		if len(topics) != 2 || len(data) != 32 {
			return dst // too generic a signature to count as "unrecognized"
		}
		if !weth.allowed(addr) {
			st.SkippedWETH++
			return dst
		}
		who, ok := topicAddr(topics[1][:])
		if !ok {
			st.Malformed++
			return dst
		}
		r := base
		if t0 == TopicDeposit {
			r.Standard = schema.StdWETHDeposit
			r.To = who // from = zero address (mint-like)
		} else {
			r.Standard = schema.StdWETHWithdrawal
			r.From = who // to = zero address (burn-like)
		}
		copy(r.Amount[:], data)
		return append(dst, r)
	}
	return dst
}

// abiUintArray decodes the head-slot'th dynamic uint256[] argument of an
// ABI-encoded tuple. Returned slices alias data.
func abiUintArray(data []byte, slot int) ([][]byte, bool) {
	words := len(data) / 32
	if slot >= words {
		return nil, false
	}
	off, ok := wordUint(word(data, slot))
	if !ok || off%32 != 0 || off/32 >= uint64(words) {
		return nil, false
	}
	at := int(off / 32)
	n, ok := wordUint(word(data, at))
	if !ok || n > MaxBatchItems || uint64(at)+1+n > uint64(words) {
		return nil, false
	}
	out := make([][]byte, n)
	for i := range out {
		out[i] = word(data, at+1+i)
	}
	return out, true
}
