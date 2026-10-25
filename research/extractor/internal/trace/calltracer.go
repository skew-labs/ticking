// Package trace turns debug_traceBlockByNumber(callTracer) output into
// native-value internal transfer rows.
package trace

import (
	"encoding/json"
	"fmt"
	"math/big"
	"strconv"
	"strings"

	"github.com/ethereum/go-ethereum/common"

	"github.com/em-mapping/extractor/internal/schema"
)

// Frame is one callTracer call frame.
type Frame struct {
	Type  string   `json:"type"`
	From  string   `json:"from"`
	To    string   `json:"to"`
	Value string   `json:"value"`
	Error string   `json:"error"`
	Calls []*Frame `json:"calls"`
	// Nitro adds beforeEVMTransfers/afterEVMTransfers on the top frame; they
	// are counted, not emitted (they are not EVM call frames).
	BeforeEVMTransfers []json.RawMessage `json:"beforeEVMTransfers"`
	AfterEVMTransfers  []json.RawMessage `json:"afterEVMTransfers"`
}

// TxTrace is one element of the debug_traceBlockByNumber result array.
type TxTrace struct {
	TxHash common.Hash `json:"txHash"`
	Result *Frame      `json:"result"`
	Error  string      `json:"error"`
}

// Stats counts filter decisions.
type Stats struct {
	Txs                 uint64 `json:"txs"`
	FailedTxs           uint64 `json:"failed_txs_excluded"`
	Frames              uint64 `json:"frames"`
	RevertedSubtrees    uint64 `json:"reverted_subtrees_excluded"`
	RevertedFrames      uint64 `json:"reverted_frames_excluded"`
	DelegateValueFrames uint64 `json:"delegate_static_callcode_value_frames_excluded"`
	UnknownTypes        uint64 `json:"unknown_frame_types"`
	ZeroValue           uint64 `json:"zero_value_frames"`
	Emitted             uint64 `json:"native_internal_rows"`
	Creations           uint64 `json:"internal_creation_rows"`
	ArbEVMTransfers     uint64 `json:"arb_before_after_evm_transfers_seen"`
}

func parseValue(s string) (*big.Int, error) {
	s = strings.TrimSpace(s)
	if s == "" || s == "0x" || s == "0x0" {
		return new(big.Int), nil
	}
	if !strings.HasPrefix(s, "0x") && !strings.HasPrefix(s, "0X") {
		return nil, fmt.Errorf("value %q not hex", s)
	}
	v, ok := new(big.Int).SetString(s[2:], 16)
	if !ok || v.Sign() < 0 || v.BitLen() > 256 {
		return nil, fmt.Errorf("bad value %q", s)
	}
	return v, nil
}

func parseAddr(s string) (common.Address, error) {
	if !common.IsHexAddress(s) {
		return common.Address{}, fmt.Errorf("bad address %q", s)
	}
	return common.HexToAddress(s), nil
}

func countFrames(f *Frame) uint64 {
	n := uint64(1)
	for _, c := range f.Calls {
		n += countFrames(c)
	}
	return n
}

// Extract appends rows for one block. Rules:
//   - failed txs (top-level frame has error) are skipped entirely;
//   - only frames at depth>0 are considered (depth 0 = the tx itself);
//   - a frame with error, and its whole subtree, is skipped (reverted);
//   - CALL / CREATE / CREATE2 / SELFDESTRUCT with value>0 are emitted;
//     DELEGATECALL / CALLCODE / STATICCALL never are (their children still are);
//   - successful CREATE/CREATE2 frames are also emitted to creations (any value).
func Extract(block uint64, traces []TxTrace, out []schema.NativeInternal, cre []schema.InternalCreation, st *Stats) ([]schema.NativeInternal, []schema.InternalCreation, error) {
	for i, tt := range traces {
		if tt.Error != "" {
			return out, cre, fmt.Errorf("tx %d (%s): tracer error: %s", i, tt.TxHash, tt.Error)
		}
		if tt.Result == nil {
			return out, cre, fmt.Errorf("tx %d (%s): empty trace result", i, tt.TxHash)
		}
		st.Txs++
		top := tt.Result
		st.ArbEVMTransfers += uint64(len(top.BeforeEVMTransfers) + len(top.AfterEVMTransfers))
		st.Frames += countFrames(top)
		if top.Error != "" {
			st.FailedTxs++
			continue
		}
		var err error
		for j, c := range top.Calls {
			out, cre, err = walk(block, uint32(i), tt.TxHash, c, strconv.Itoa(j), 1, out, cre, st)
			if err != nil {
				return out, cre, fmt.Errorf("tx %d (%s): %w", i, tt.TxHash, err)
			}
		}
	}
	return out, cre, nil
}

func walk(block uint64, txIndex uint32, txHash common.Hash, f *Frame, path string, depth int, out []schema.NativeInternal, cre []schema.InternalCreation, st *Stats) ([]schema.NativeInternal, []schema.InternalCreation, error) {
	if f == nil {
		return out, cre, nil
	}
	if f.Error != "" {
		st.RevertedSubtrees++
		st.RevertedFrames += countFrames(f)
		return out, cre, nil
	}
	typ := strings.ToUpper(f.Type)
	v, err := parseValue(f.Value)
	if err != nil {
		return out, cre, fmt.Errorf("frame %s: %w", path, err)
	}
	switch typ {
	case "CALL", "CREATE", "CREATE2", "SELFDESTRUCT":
		if v.Sign() > 0 || typ == "CREATE" || typ == "CREATE2" {
			from, err := parseAddr(f.From)
			if err != nil {
				return out, cre, fmt.Errorf("frame %s from: %w", path, err)
			}
			to, err := parseAddr(f.To)
			if err != nil {
				return out, cre, fmt.Errorf("frame %s to: %w", path, err)
			}
			var val [32]byte
			v.FillBytes(val[:])
			if v.Sign() > 0 {
				out = append(out, schema.NativeInternal{BlockNumber: block, TxIndex: txIndex, TxHash: txHash, TracePath: path, Depth: uint16(depth), CallType: typ, From: from, To: to, Value: val})
				st.Emitted++
			} else {
				st.ZeroValue++
			}
			if typ == "CREATE" || typ == "CREATE2" {
				cre = append(cre, schema.InternalCreation{BlockNumber: block, TxIndex: txIndex, TxHash: txHash, TracePath: path, CallType: typ, Creator: from, Address: to, Value: val})
				st.Creations++
			}
		} else {
			st.ZeroValue++
		}
	case "DELEGATECALL", "CALLCODE", "STATICCALL":
		if v.Sign() > 0 {
			st.DelegateValueFrames++
		}
	default:
		st.UnknownTypes++
	}
	for j, c := range f.Calls {
		out, cre, err = walk(block, txIndex, txHash, c, path+"."+strconv.Itoa(j), depth+1, out, cre, st)
		if err != nil {
			return out, cre, err
		}
	}
	return out, cre, nil
}
