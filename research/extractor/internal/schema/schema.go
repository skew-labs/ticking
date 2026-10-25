// Package schema defines the Parquet row layouts.
//
// Encoding conventions (documented in README):
//   - addresses: FIXED_LEN_BYTE_ARRAY(20); hashes / topics: FIXED_LEN_BYTE_ARRAY(32)
//   - uint256 amounts (tx value, token amount, token id, trace value):
//     FIXED_LEN_BYTE_ARRAY(32), unsigned big-endian. Lossless, fixed width,
//     and byte-wise ordering equals numeric ordering.
//   - gas quantities / gas prices / base fee: INT64 annotated UINT_64. Values
//     that do not fit uint64 fail the chunk (never silently truncated).
//   - Go pointer fields are OPTIONAL (NULL) columns.
package schema

// Block is one row of blocks/*.parquet.
type Block struct {
	Number        uint64   `parquet:"number,delta"`
	Hash          [32]byte `parquet:"hash"`
	ParentHash    [32]byte `parquet:"parent_hash"`
	Timestamp     uint64   `parquet:"timestamp,delta"`
	TxCount       uint32   `parquet:"tx_count"`
	GasUsed       uint64   `parquet:"gas_used"`
	GasLimit      uint64   `parquet:"gas_limit"`
	BaseFee       *uint64  `parquet:"base_fee"`
	L1BlockNumber *uint64  `parquet:"l1_block_number"`
	ArbOSVersion  *uint64  `parquet:"arbos_version"`
	SendCount     *uint64  `parquet:"send_count"`
	Miner         [20]byte `parquet:"miner"`
	IsClassic     bool     `parquet:"is_classic"`
	Size          uint32   `parquet:"rlp_size"`
}

// Tx is one row of transactions/*.parquet.
type Tx struct {
	BlockNumber       uint64    `parquet:"block_number,delta"`
	TxIndex           uint32    `parquet:"tx_index"`
	Hash              [32]byte  `parquet:"hash"`
	Type              uint8     `parquet:"type"`
	From              [20]byte  `parquet:"from"`
	To                *[20]byte `parquet:"to"`
	Value             [32]byte  `parquet:"value"`
	DepositValue      *[32]byte `parquet:"deposit_value"`
	Nonce             uint64    `parquet:"nonce"`
	GasLimit          uint64    `parquet:"gas_limit"`
	GasUsed           uint64    `parquet:"gas_used"`
	Status            uint8     `parquet:"status"`
	ContractAddress   *[20]byte `parquet:"contract_address"`
	EffectiveGasPrice *uint64   `parquet:"effective_gas_price"`
	GasUsedForL1      uint64    `parquet:"gas_used_for_l1"`
	MethodID          *[4]byte  `parquet:"method_id"`
	InputSize         uint32    `parquet:"input_size"`
	LogCount          uint32    `parquet:"log_count"`
}

// Log is one row of logs/*.parquet. Data is kept in full (no cap).
type Log struct {
	BlockNumber uint64    `parquet:"block_number,delta"`
	TxIndex     uint32    `parquet:"tx_index"`
	LogIndex    uint32    `parquet:"log_index"`
	Address     [20]byte  `parquet:"address,dict"`
	Topic0      *[32]byte `parquet:"topic0,dict"`
	Topic1      *[32]byte `parquet:"topic1"`
	Topic2      *[32]byte `parquet:"topic2"`
	Topic3      *[32]byte `parquet:"topic3"`
	TopicCount  uint8     `parquet:"topic_count"`
	Data        []byte    `parquet:"data"`
}

// Token standards emitted in token_transfers.standard.
const (
	StdERC20          = "ERC20"
	StdERC721         = "ERC721"
	StdERC1155Single  = "ERC1155_SINGLE"
	StdERC1155Batch   = "ERC1155_BATCH"
	StdWETHDeposit    = "WETH_DEPOSIT"
	StdWETHWithdrawal = "WETH_WITHDRAWAL"
)

// TokenTransfer is one row of token_transfers/*.parquet.
type TokenTransfer struct {
	BlockNumber uint64    `parquet:"block_number,delta"`
	TxIndex     uint32    `parquet:"tx_index"`
	LogIndex    uint32    `parquet:"log_index"`
	BatchIndex  uint32    `parquet:"batch_index"`
	Standard    string    `parquet:"standard,dict"`
	Token       [20]byte  `parquet:"token,dict"`
	From        [20]byte  `parquet:"from"`
	To          [20]byte  `parquet:"to"`
	Operator    *[20]byte `parquet:"operator"`
	TokenID     *[32]byte `parquet:"token_id"`
	Amount      [32]byte  `parquet:"amount"`
}

// ContractCreation is one row of contract_creations/*.parquet (tx-level creations).
type ContractCreation struct {
	BlockNumber uint64   `parquet:"block_number,delta"`
	TxIndex     uint32   `parquet:"tx_index"`
	Address     [20]byte `parquet:"address"`
	Creator     [20]byte `parquet:"creator"`
	TxType      uint8    `parquet:"tx_type"`
}

// NativeInternal is one row of native_internal/*.parquet (em-trace).
type NativeInternal struct {
	BlockNumber uint64   `parquet:"block_number,delta"`
	TxIndex     uint32   `parquet:"tx_index"`
	TxHash      [32]byte `parquet:"tx_hash"`
	TracePath   string   `parquet:"trace_path"`
	Depth       uint16   `parquet:"depth"`
	CallType    string   `parquet:"call_type,dict"`
	From        [20]byte `parquet:"from"`
	To          [20]byte `parquet:"to"`
	Value       [32]byte `parquet:"value"`
}

// InternalCreation is one row of internal_creations/*.parquet (em-trace):
// successful CREATE/CREATE2 frames at depth>0 regardless of value.
type InternalCreation struct {
	BlockNumber uint64   `parquet:"block_number,delta"`
	TxIndex     uint32   `parquet:"tx_index"`
	TxHash      [32]byte `parquet:"tx_hash"`
	TracePath   string   `parquet:"trace_path"`
	CallType    string   `parquet:"call_type,dict"`
	Creator     [20]byte `parquet:"creator"`
	Address     [20]byte `parquet:"address"`
	Value       [32]byte `parquet:"value"`
}
