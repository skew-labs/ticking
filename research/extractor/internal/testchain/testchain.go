// Package testchain builds small synthetic Nitro-style chain databases
// (geth-written Pebble + ancient freezer) for tests. It covers Classic
// (ArbitrumLegacyTx + legacy-format receipts) and Nitro blocks with Arbitrum
// system tx types, signed legacy/dynamic-fee txs, creations, failed txs and
// token logs.
package testchain

import (
	"crypto/ecdsa"
	"fmt"
	"math/big"
	"path/filepath"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	gethpebble "github.com/ethereum/go-ethereum/ethdb/pebble"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
)

// ChainID used by synthetic chains.
const ChainID = 42161

var (
	topicTransfer = crypto.Keccak256Hash([]byte("Transfer(address,address,uint256)"))
	topicBatch    = crypto.Keccak256Hash([]byte("TransferBatch(address,address,address,uint256[],uint256[])"))
)

// Spec controls generation.
type Spec struct {
	Blocks       uint64 // total blocks including genesis (numbers 0..Blocks-1)
	Frozen       uint64 // blocks [0,Frozen) go to the freezer, the rest to pebble
	NitroGenesis uint64 // blocks below are Classic
	// CorruptReceiptAt, if non-zero, stores receipts for that (kv) block with
	// a modified cumulative gas so the receipt root no longer matches.
	CorruptReceiptAt uint64
	// ExtraTxs adds that many signed dynamic-fee txs per Nitro block, each
	// with ExtraLogs ERC20-Transfer-like logs (for benchmarks).
	ExtraTxs, ExtraLogs int
}

// Expect are the totals the generator produced.
type Expect struct {
	Txs, Logs, Transfers, Creations uint64
	Hashes                          []common.Hash
	Creator                         common.Address
}

func addrTopic(a common.Address) common.Hash { return common.BytesToHash(a.Bytes()) }

func u256(v uint64) []byte { return common.BigToHash(new(big.Int).SetUint64(v)).Bytes() }

// Build writes the chain into dir (pebble at dir, freezer at dir/ancient/chain).
func Build(dir string, s Spec) (*Expect, error) {
	key, _ := crypto.HexToECDSA("b71c71a67e1177ad4e901695e1b4b9ee17ae16c6668d313eac2f96dbcda3f291")
	sender := crypto.PubkeyToAddress(key.PublicKey)
	signer := types.LatestSignerForChainID(big.NewInt(ChainID))
	kv, err := gethpebble.New(dir, 16, 16, "", false, nil)
	if err != nil {
		return nil, err
	}
	db, err := rawdb.Open(kv, rawdb.OpenOptions{Ancient: filepath.Join(dir, "ancient")})
	if err != nil {
		kv.Close()
		return nil, err
	}
	defer db.Close()
	exp := &Expect{Creator: sender}
	var blocks []*types.Block
	var receiptsRLP [][]byte
	parent := common.Hash{}
	nonce := uint64(0)
	token := common.HexToAddress("0x1000000000000000000000000000000000000001")
	nft := common.HexToAddress("0x2000000000000000000000000000000000000002")
	for n := uint64(0); n < s.Blocks; n++ {
		h := &types.Header{
			ParentHash: parent,
			Number:     new(big.Int).SetUint64(n),
			GasLimit:   1 << 50,
			Time:       1_700_000_000 + n,
			Difficulty: big.NewInt(1),
			Coinbase:   common.HexToAddress("0xA4B000000000000000000073657175656e636572"),
		}
		classic := n < s.NitroGenesis
		if !classic {
			h.BaseFee = big.NewInt(100_000_000)
			types.HeaderInfo{SendCount: n, L1BlockNumber: 1000 + n, ArbOSFormatVersion: 32}.UpdateHeaderWithInfo(h)
		}
		var txs []*types.Transaction
		var rcs []*types.Receipt
		cum := uint64(0)
		add := func(tx *types.Transaction, status uint64, gas uint64, logs []*types.Log) {
			cum += gas
			r := &types.Receipt{Type: tx.Type(), Status: status, CumulativeGasUsed: cum, Logs: logs, GasUsedForL1: gas / 10}
			if tx.Type() == types.ArbitrumLegacyTxType {
				r.PostState = []byte{0}
				r.GasUsed = gas
				if tx.To() == nil && status == 1 {
					from, _ := types.Sender(signer, tx)
					r.ContractAddress = crypto.CreateAddress(from, tx.Nonce())
				}
			}
			r.Bloom = types.CreateBloom(r)
			txs = append(txs, tx)
			rcs = append(rcs, r)
			exp.Txs++
			exp.Logs += uint64(len(logs))
		}
		if n > 0 && classic {
			// Classic: signed legacy tx wrapped as ArbitrumLegacyTx, plus an
			// unsigned one with a Sender override (L1->L2 style).
			inner := types.NewTx(&types.LegacyTx{Nonce: nonce, GasPrice: big.NewInt(1e9), Gas: 100000, To: &token, Value: big.NewInt(0), Data: []byte{0xa9, 0x05, 0x9c, 0xbb}})
			signed, _ := types.SignTx(inner, types.NewEIP155Signer(big.NewInt(ChainID)), key)
			nonce++
			wrapped, err := types.NewArbitrumLegacyTx(signed, signed.Hash(), 1e9-1, 900+n, nil)
			if err != nil {
				return nil, err
			}
			logs := []*types.Log{{Address: token, Topics: []common.Hash{topicTransfer, addrTopic(sender), addrTopic(common.Address{0x42})}, Data: u256(5)}}
			add(wrapped, 1, 30000, logs)
			exp.Transfers++
			over := common.HexToAddress("0x3333333333333333333333333333333333333333")
			unsigned := types.NewTx(&types.LegacyTx{Nonce: 0, GasPrice: big.NewInt(0), Gas: 21000, To: &token, Value: big.NewInt(7), V: big.NewInt(0), R: big.NewInt(0), S: big.NewInt(0)})
			w2, _ := types.NewArbitrumLegacyTx(unsigned, crypto.Keccak256Hash([]byte(fmt.Sprint("classic-unsigned", n))), 0, 900+n, &over)
			add(w2, 1, 21000, nil)
		} else if n > 0 {
			add(types.NewTx(&types.ArbitrumInternalTx{ChainId: big.NewInt(ChainID), Data: []byte{0x6b, 0xf6, 0xa4, 0x2d, byte(n)}}), 1, 0, nil)
			dyn := types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(ChainID), Nonce: nonce, GasTipCap: big.NewInt(0), GasFeeCap: big.NewInt(2e8), Gas: 100000, To: &token, Value: big.NewInt(0), Data: []byte{0xa9, 0x05, 0x9c, 0xbb, 1}})
			dyn, _ = types.SignTx(dyn, signer, key)
			nonce++
			logs := []*types.Log{{Address: token, Topics: []common.Hash{topicTransfer, addrTopic(sender), addrTopic(common.Address{0x42})}, Data: u256(n)}}
			add(dyn, 1, 50000, logs)
			exp.Transfers++
			for k := 0; k < s.ExtraTxs; k++ {
				x := types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(ChainID), Nonce: nonce, GasTipCap: big.NewInt(0), GasFeeCap: big.NewInt(2e8), Gas: 300000, To: &token, Value: big.NewInt(int64(k)), Data: make([]byte, 196)})
				x, _ = types.SignTx(x, signer, key)
				nonce++
				var logs []*types.Log
				for j := 0; j < s.ExtraLogs; j++ {
					logs = append(logs, &types.Log{Address: common.BigToAddress(big.NewInt(int64(0x1000 + j))), Topics: []common.Hash{topicTransfer, addrTopic(sender), addrTopic(common.BigToAddress(big.NewInt(int64(n*100 + uint64(j)))))}, Data: u256(n + uint64(j))})
				}
				add(x, 1, 90000, logs)
				exp.Transfers += uint64(s.ExtraLogs)
			}
			if n%5 == 0 {
				add(types.NewTx(&types.ArbitrumDepositTx{ChainId: big.NewInt(ChainID), L1RequestId: common.Hash{byte(n)}, From: common.HexToAddress("0x1111000000000000000000000000000000001111"), To: common.Address{0x77}, Value: big.NewInt(1e18)}), 1, 0, nil)
			}
			if n%7 == 0 {
				c := types.NewTx(&types.LegacyTx{Nonce: nonce, GasPrice: big.NewInt(2e8), Gas: 500000, Value: big.NewInt(0), Data: []byte{0x60, 0x00}})
				c, _ = types.SignTx(c, types.NewEIP155Signer(big.NewInt(ChainID)), key)
				nonce++
				add(c, 1, 60000, nil)
				exp.Creations++
			}
			if n%11 == 0 {
				f := types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(ChainID), Nonce: nonce, GasTipCap: big.NewInt(0), GasFeeCap: big.NewInt(2e8), Gas: 100000, To: &token, Value: big.NewInt(0)})
				f, _ = types.SignTx(f, signer, key)
				nonce++
				add(f, 0, 40000, nil)
			}
			if n%13 == 0 {
				var data []byte
				data = append(data, u256(64)...)
				data = append(data, u256(160)...)
				data = append(data, u256(2)...)
				data = append(data, u256(11)...)
				data = append(data, u256(12)...)
				data = append(data, u256(2)...)
				data = append(data, u256(100)...)
				data = append(data, u256(200)...)
				logs := []*types.Log{
					{Address: nft, Topics: []common.Hash{topicTransfer, addrTopic(common.Address{}), addrTopic(sender), common.BigToHash(big.NewInt(int64(n)))}},
					{Address: nft, Topics: []common.Hash{topicBatch, addrTopic(sender), addrTopic(sender), addrTopic(common.Address{0x55})}, Data: data},
				}
				d := types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(ChainID), Nonce: nonce, GasTipCap: big.NewInt(0), GasFeeCap: big.NewInt(2e8), Gas: 200000, To: &nft, Value: big.NewInt(0)})
				d, _ = types.SignTx(d, signer, key)
				nonce++
				add(d, 1, 70000, logs)
				exp.Transfers += 3
			}
		}
		h.GasUsed = cum
		block := types.NewBlock(h, &types.Body{Transactions: txs}, rcs, trie.NewStackTrie(nil))
		parent = block.Hash()
		exp.Hashes = append(exp.Hashes, parent)
		stored := make([]*types.ReceiptForStorage, len(rcs))
		for i, r := range rcs {
			stored[i] = (*types.ReceiptForStorage)(r)
		}
		if s.CorruptReceiptAt != 0 && n == s.CorruptReceiptAt && len(stored) > 0 {
			c := *rcs[len(rcs)-1]
			c.CumulativeGasUsed += 1
			stored[len(stored)-1] = (*types.ReceiptForStorage)(&c)
		}
		raw, err := rlp.EncodeToBytes(stored)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, block)
		receiptsRLP = append(receiptsRLP, raw)
	}
	if s.Frozen > 0 {
		raws := make([]rlp.RawValue, s.Frozen)
		for i := range raws {
			raws[i] = receiptsRLP[i]
		}
		if _, err := rawdb.WriteAncientBlocks(db, blocks[:s.Frozen], raws); err != nil {
			return nil, err
		}
	}
	rawdb.WriteCanonicalHash(db, blocks[0].Hash(), 0)
	cfg := &params.ChainConfig{ChainID: big.NewInt(ChainID), ArbitrumChainParams: params.ArbitrumChainParams{EnableArbOS: true, GenesisBlockNum: s.NitroGenesis}}
	rawdb.WriteChainConfig(db, blocks[0].Hash(), cfg)
	for i := s.Frozen; i < uint64(len(blocks)); i++ {
		b := blocks[i]
		rawdb.WriteBlock(db, b)
		if err := db.Put(append(append([]byte("r"), encNum(i)...), b.Hash().Bytes()...), receiptsRLP[i]); err != nil {
			return nil, err
		}
		rawdb.WriteCanonicalHash(db, b.Hash(), i)
	}
	last := blocks[len(blocks)-1]
	rawdb.WriteHeadBlockHash(db, last.Hash())
	rawdb.WriteHeadHeaderHash(db, last.Hash())
	rawdb.WriteHeaderNumber(db, last.Hash(), last.NumberU64())
	return exp, nil
}

func encNum(n uint64) []byte {
	b := make([]byte, 8)
	for i := 7; i >= 0; i-- {
		b[i] = byte(n)
		n >>= 8
	}
	return b
}

// Key returns the deterministic test signing key.
func Key() *ecdsa.PrivateKey {
	k, _ := crypto.HexToECDSA("b71c71a67e1177ad4e901695e1b4b9ee17ae16c6668d313eac2f96dbcda3f291")
	return k
}
