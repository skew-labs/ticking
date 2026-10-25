package decode

import (
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/schema"
)

func at(a common.Address) common.Hash { return common.BytesToHash(a.Bytes()) }
func w(v uint64) []byte               { return common.BigToHash(new(big.Int).SetUint64(v)).Bytes() }

var (
	tok   = common.HexToAddress("0x00000000000000000000000000000000000000f0")
	alice = common.HexToAddress("0x00000000000000000000000000000000000000a1")
	bob   = common.HexToAddress("0x00000000000000000000000000000000000000b2")
	op    = common.HexToAddress("0x00000000000000000000000000000000000000c3")
)

func TestTopicConstants(t *testing.T) {
	for got, want := range map[common.Hash]string{
		TopicTransfer:       "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
		TopicTransferSingle: "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62",
		TopicTransferBatch:  "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb",
		TopicDeposit:        "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c",
		TopicWithdrawal:     "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
	} {
		if got != common.HexToHash(want) {
			t.Errorf("topic %s != %s", got, want)
		}
	}
}

func amt(r schema.TokenTransfer) uint64 { return new(big.Int).SetBytes(r.Amount[:]).Uint64() }

func TestERC20vsERC721ByTopicCount(t *testing.T) {
	var st TokenStats
	// ERC20: 3 topics + 32-byte data.
	rows := DecodeTokenLog(nil, 1, 0, 0, tok, []common.Hash{TopicTransfer, at(alice), at(bob)}, w(1234), nil, &st)
	if len(rows) != 1 || rows[0].Standard != schema.StdERC20 || rows[0].TokenID != nil || amt(rows[0]) != 1234 || common.Address(rows[0].From) != alice || common.Address(rows[0].To) != bob {
		t.Fatalf("erc20 %+v", rows)
	}
	// ERC721: 4 topics, no data; tokenId from topic3, amount 1.
	rows = DecodeTokenLog(nil, 1, 0, 1, tok, []common.Hash{TopicTransfer, at(alice), at(bob), common.BigToHash(big.NewInt(77))}, nil, nil, &st)
	if len(rows) != 1 || rows[0].Standard != schema.StdERC721 || rows[0].TokenID == nil || new(big.Int).SetBytes(rows[0].TokenID[:]).Int64() != 77 || amt(rows[0]) != 1 {
		t.Fatalf("erc721 %+v", rows)
	}
	// Unrecognized shapes: ERC20-like with 64-byte data, all-in-data (CryptoKitties style).
	rows = DecodeTokenLog(nil, 1, 0, 2, tok, []common.Hash{TopicTransfer, at(alice), at(bob)}, append(w(1), w(2)...), nil, &st)
	rows = DecodeTokenLog(rows, 1, 0, 3, tok, []common.Hash{TopicTransfer}, append(append(w(1), w(2)...), w(3)...), nil, &st)
	if len(rows) != 0 || st.Unrecognized != 2 {
		t.Fatalf("unrecognized %+v %+v", rows, st)
	}
	// Dirty address padding is rejected, not truncated.
	dirty := at(alice)
	dirty[0] = 1
	rows = DecodeTokenLog(nil, 1, 0, 4, tok, []common.Hash{TopicTransfer, dirty, at(bob)}, w(1), nil, &st)
	if len(rows) != 0 || st.Malformed != 1 {
		t.Fatalf("malformed %+v", st)
	}
}

func TestERC1155SingleAndBatch(t *testing.T) {
	var st TokenStats
	rows := DecodeTokenLog(nil, 9, 2, 5, tok, []common.Hash{TopicTransferSingle, at(op), at(alice), at(bob)}, append(w(42), w(3)...), nil, &st)
	if len(rows) != 1 || rows[0].Standard != schema.StdERC1155Single || new(big.Int).SetBytes(rows[0].TokenID[:]).Int64() != 42 || amt(rows[0]) != 3 || common.Address(*rows[0].Operator) != op {
		t.Fatalf("single %+v", rows)
	}
	// TransferBatch(ids=[1,2,3], values=[10,20,30]).
	var data []byte
	for _, v := range []uint64{64, 64 + 32*4, 3, 1, 2, 3, 3, 10, 20, 30} {
		data = append(data, w(v)...)
	}
	rows = DecodeTokenLog(nil, 9, 2, 6, tok, []common.Hash{TopicTransferBatch, at(op), at(alice), at(bob)}, data, nil, &st)
	if len(rows) != 3 {
		t.Fatalf("batch rows %d", len(rows))
	}
	for i, r := range rows {
		if r.Standard != schema.StdERC1155Batch || r.BatchIndex != uint32(i) || r.LogIndex != 6 || new(big.Int).SetBytes(r.TokenID[:]).Uint64() != uint64(i+1) || amt(r) != uint64(10*(i+1)) || common.Address(r.From) != alice || common.Address(r.To) != bob {
			t.Fatalf("batch row %d %+v", i, r)
		}
	}
	// Mismatched lengths / out-of-bounds offsets are rejected.
	bad := append([]byte{}, data...)
	copy(bad[32*6:], w(2)) // values length 2 != ids length 3
	if rows := DecodeTokenLog(nil, 9, 2, 7, tok, []common.Hash{TopicTransferBatch, at(op), at(alice), at(bob)}, bad, nil, &st); len(rows) != 0 {
		t.Fatal("length mismatch accepted")
	}
	oob := append([]byte{}, data...)
	copy(oob[32:], w(4096))
	if rows := DecodeTokenLog(nil, 9, 2, 8, tok, []common.Hash{TopicTransferBatch, at(op), at(alice), at(bob)}, oob, nil, &st); len(rows) != 0 {
		t.Fatal("oob offset accepted")
	}
	if st.Malformed != 2 {
		t.Fatalf("stats %+v", st)
	}
}

func TestWETHDepositWithdrawalAllowlist(t *testing.T) {
	var st TokenStats
	weth := &WETHFilter{Allow: map[common.Address]bool{tok: true}}
	rows := DecodeTokenLog(nil, 1, 0, 0, tok, []common.Hash{TopicDeposit, at(alice)}, w(5), weth, &st)
	rows = DecodeTokenLog(rows, 1, 0, 1, tok, []common.Hash{TopicWithdrawal, at(bob)}, w(6), weth, &st)
	if len(rows) != 2 || rows[0].Standard != schema.StdWETHDeposit || common.Address(rows[0].To) != alice || rows[0].From != ([20]byte{}) ||
		rows[1].Standard != schema.StdWETHWithdrawal || common.Address(rows[1].From) != bob || rows[1].To != ([20]byte{}) || amt(rows[1]) != 6 {
		t.Fatalf("weth %+v", rows)
	}
	// Same signature from a non-allowlisted emitter (e.g. a vault) is skipped.
	rows = DecodeTokenLog(nil, 1, 0, 2, alice, []common.Hash{TopicDeposit, at(alice)}, w(5), weth, &st)
	if len(rows) != 0 || st.SkippedWETH != 1 {
		t.Fatal("non-allowlisted deposit decoded")
	}
	if rows := DecodeTokenLog(nil, 1, 0, 2, alice, []common.Hash{TopicDeposit, at(alice)}, w(5), &WETHFilter{Any: true}, &st); len(rows) != 1 {
		t.Fatal("any mode")
	}
}

// Arbitrum system tx types: the sender is the explicit From (ArbOS for
// internal txs), never an ECDSA recovery; Classic legacy txs use the Sender
// override when present, otherwise recover from the wrapped signed LegacyTx.
func TestArbitrumTxTypeSenders(t *testing.T) {
	chain := big.NewInt(42161)
	d := New(Config{ChainID: 42161})
	from := common.HexToAddress("0xC0FFEE0000000000000000000000000000000001")
	to := common.HexToAddress("0x00000000000000000000000000000000000000d0")
	cases := []struct {
		name string
		tx   *types.Transaction
		want common.Address
	}{
		{"deposit(100)", types.NewTx(&types.ArbitrumDepositTx{ChainId: chain, L1RequestId: common.Hash{1}, From: from, To: to, Value: big.NewInt(1e18)}), from},
		{"unsigned(101)", types.NewTx(&types.ArbitrumUnsignedTx{ChainId: chain, From: from, Nonce: 3, GasFeeCap: big.NewInt(1e8), Gas: 21000, To: &to, Value: big.NewInt(1)}), from},
		{"contract(102)", types.NewTx(&types.ArbitrumContractTx{ChainId: chain, RequestId: common.Hash{2}, From: from, GasFeeCap: big.NewInt(1e8), Gas: 21000, To: &to, Value: big.NewInt(1)}), from},
		{"retry(104)", types.NewTx(&types.ArbitrumRetryTx{ChainId: chain, Nonce: 0, From: from, GasFeeCap: big.NewInt(1e8), Gas: 21000, To: &to, Value: big.NewInt(1), TicketId: common.Hash{3}, RefundTo: from, MaxRefund: big.NewInt(0), SubmissionFeeRefund: big.NewInt(0)}), from},
		{"submitRetryable(105)", types.NewTx(&types.ArbitrumSubmitRetryableTx{ChainId: chain, RequestId: common.Hash{4}, From: from, L1BaseFee: big.NewInt(1), DepositValue: big.NewInt(5), GasFeeCap: big.NewInt(1), Gas: 1, RetryTo: &to, RetryValue: big.NewInt(0), Beneficiary: from, MaxSubmissionFee: big.NewInt(0), FeeRefundAddr: from}), from},
		{"internal(106)", types.NewTx(&types.ArbitrumInternalTx{ChainId: chain, Data: []byte{1}}), types.ArbosAddress},
	}
	key, _ := crypto.GenerateKey()
	signerAddr := crypto.PubkeyToAddress(key.PublicKey)
	signed, _ := types.SignTx(types.NewTx(&types.LegacyTx{Nonce: 1, GasPrice: big.NewInt(1), Gas: 21000, To: &to, Value: big.NewInt(1)}), types.NewEIP155Signer(chain), key)
	legacySigned, _ := types.NewArbitrumLegacyTx(signed, common.Hash{9}, 1, 1, nil)
	override := common.HexToAddress("0x00000000000000000000000000000000000000e9")
	unsigned := types.NewTx(&types.LegacyTx{Nonce: 0, GasPrice: big.NewInt(0), Gas: 21000, To: &to, Value: big.NewInt(1), V: big.NewInt(0), R: big.NewInt(0), S: big.NewInt(0)})
	legacyOverride, _ := types.NewArbitrumLegacyTx(unsigned, common.Hash{10}, 0, 1, &override)
	unprotected, _ := types.SignTx(types.NewTx(&types.LegacyTx{Nonce: 2, GasPrice: big.NewInt(1), Gas: 21000, To: &to, Value: big.NewInt(1)}), types.HomesteadSigner{}, key)
	dyn, _ := types.SignTx(types.NewTx(&types.DynamicFeeTx{ChainID: chain, Nonce: 4, GasTipCap: big.NewInt(0), GasFeeCap: big.NewInt(1), Gas: 21000, To: &to}), types.LatestSignerForChainID(chain), key)
	cases = append(cases,
		struct {
			name string
			tx   *types.Transaction
			want common.Address
		}{"arbLegacy(120) signed", legacySigned, signerAddr},
		struct {
			name string
			tx   *types.Transaction
			want common.Address
		}{"arbLegacy(120) override", legacyOverride, override},
		struct {
			name string
			tx   *types.Transaction
			want common.Address
		}{"legacy pre-EIP155", unprotected, signerAddr},
		struct {
			name string
			tx   *types.Transaction
			want common.Address
		}{"dynamicFee(2)", dyn, signerAddr},
	)
	// Round-trip through a block body RLP exactly as stored in the database
	// (RLP decoding enables Arbitrum type parsing; UnmarshalBinary does not).
	var txs []*types.Transaction
	for _, c := range cases {
		txs = append(txs, c.tx)
	}
	raw, err := rlp.EncodeToBytes(&types.Body{Transactions: txs})
	if err != nil {
		t.Fatal(err)
	}
	var body types.Body
	if err := rlp.DecodeBytes(raw, &body); err != nil {
		t.Fatal(err)
	}
	for i, c := range cases {
		tx := body.Transactions[i]
		if tx.Type() != c.tx.Type() || tx.Hash() != c.tx.Hash() {
			t.Fatalf("%s: round trip type/hash", c.name)
		}
		got, err := SenderOf(d.Signer(), tx)
		if err != nil || got != c.want {
			t.Errorf("%s: sender %s err %v want %s", c.name, got, err, c.want)
		}
	}
	if legacySigned.Hash() != (common.Hash{9}) {
		t.Error("ArbitrumLegacyTx hash override")
	}
}

func TestSenderErrorPolicy(t *testing.T) {
	to := common.HexToAddress("0x00000000000000000000000000000000000000d0")
	bad := types.NewTx(&types.LegacyTx{Nonce: 1, GasPrice: big.NewInt(1), Gas: 21000, To: &to, Value: big.NewInt(1), V: big.NewInt(27), R: big.NewInt(0), S: big.NewInt(0)})
	h := &types.Header{Number: big.NewInt(5), Difficulty: big.NewInt(1), BaseFee: big.NewInt(1)}
	hraw, _ := rlp.EncodeToBytes(h)
	braw, _ := rlp.EncodeToBytes(&types.Body{Transactions: []*types.Transaction{bad}})
	rraw, _ := rlp.EncodeToBytes([]*types.ReceiptForStorage{{Status: 1, CumulativeGasUsed: 21000}})
	raw := chaindb.RawBlock{Number: 5, Hash: crypto.Keccak256Hash(hraw), Header: hraw, Body: braw, Receipts: rraw}
	var rows Rows
	var st Stats
	_, err := New(Config{ChainID: 42161}).Block(&raw, nil, &rows, &st)
	if be, ok := err.(*BlockError); !ok || be.Reason != ReasonSender {
		t.Fatalf("default policy: %v", err)
	}
	rows, st = Rows{}, Stats{}
	if _, err := New(Config{ChainID: 42161, SenderErrorZero: true}).Block(&raw, nil, &rows, &st); err != nil {
		t.Fatal(err)
	}
	if len(rows.Txs) != 1 || rows.Txs[0].From != ([20]byte{}) || st.SenderErrors != 1 || len(st.SenderErrTxs) != 1 {
		t.Fatalf("zero policy: %+v %+v", rows.Txs, st)
	}
}
