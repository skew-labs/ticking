// Package chaindb opens a Nitro l2chaindata directory strictly read-only:
// frozen ranges are served by the internal read-only freezer reader, newer
// blocks by Pebble opened with ReadOnly=true.
package chaindb

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb"
	gethleveldb "github.com/ethereum/go-ethereum/ethdb/leveldb"
	gethpebble "github.com/ethereum/go-ethereum/ethdb/pebble"
	"github.com/ethereum/go-ethereum/params"

	"github.com/em-mapping/extractor/internal/freezer"
)

// RawBlock is the undecoded canonical block data.
type RawBlock struct {
	Number   uint64
	Hash     common.Hash // canonical hash (freezer hashes table or kv number->hash)
	Header   []byte
	Body     []byte
	Receipts []byte
	FromKV   bool
	// BorReceipt is bor's pre-PIP-74 state-sync receipt (one ReceiptForStorage), set only on bor databases for
	// sprint-start blocks with state-sync events.
	BorReceipt []byte
}

// Size returns the total decoded RLP size.
func (r *RawBlock) Size() int { return len(r.Header) + len(r.Body) + len(r.Receipts) + len(r.BorReceipt) }

// BorReceiptKey is bor's kv key of a block's state-sync receipt: "matic-bor-receipt-" + num (uint64 BE) + hash.
// keccak(key) is the hash bor's RPC gives that receipt's (derived) tx.
func BorReceiptKey(n uint64, h common.Hash) []byte {
	k := make([]byte, 0, 18+8+32)
	k = append(k, "matic-bor-receipt-"...)
	k = binary.BigEndian.AppendUint64(k, n)
	return append(k, h.Bytes()...)
}

// Options configure Open.
type Options struct {
	DataDir    string // l2chaindata dir, its parent, or a snapshot root
	AncientDir string // optional explicit chain freezer dir
	CacheMB    int
	Handles    int
	NoKV       bool // freezer only (no pebble)
}

// DB is the read-only chain database.
type DB struct {
	ChainDataDir string
	AncientDir   string
	Engine       string // "pebble" | "leveldb"
	kv           ethdb.KeyValueStore
	rdb          ethdb.Database // rawdb accessors over kv only (no freezer)
	fz           *freezer.Chain
	Frozen       uint64 // blocks [Tail, Frozen) are in the freezer
	Tail         uint64
	Warnings     []string
}

// ResolveChainData finds the l2chaindata directory from a user-supplied path.
func ResolveChainData(p string) (string, error) {
	p = filepath.Clean(p)
	isDB := func(d string) bool {
		_, err := os.Stat(filepath.Join(d, "CURRENT"))
		return err == nil
	}
	if isDB(p) {
		return p, nil
	}
	cands := []string{
		filepath.Join(p, "l2chaindata"),
		filepath.Join(p, "nitro", "l2chaindata"),
		filepath.Join(p, "arb1", "nitro", "l2chaindata"),
	}
	for _, c := range cands {
		if isDB(c) {
			return c, nil
		}
	}
	// Bounded search (depth <= 4) for a directory named l2chaindata.
	var found []string
	var walk func(d string, depth int)
	walk = func(d string, depth int) {
		if depth > 4 || len(found) > 4 {
			return
		}
		ents, err := os.ReadDir(d)
		if err != nil {
			return
		}
		for _, e := range ents {
			if !e.IsDir() {
				continue
			}
			sub := filepath.Join(d, e.Name())
			if e.Name() == "l2chaindata" && isDB(sub) {
				found = append(found, sub)
				continue
			}
			if e.Name() == "ancient" || e.Name() == "wasm" {
				continue
			}
			walk(sub, depth+1)
		}
	}
	walk(p, 0)
	sort.Strings(found)
	switch len(found) {
	case 0:
		return "", fmt.Errorf("no pebble chain database (CURRENT file) found at or under %s", p)
	case 1:
		return found[0], nil
	default:
		return "", fmt.Errorf("multiple l2chaindata candidates under %s: %v (pass the exact one)", p, found)
	}
}

// ResolveAncient auto-detects the chain freezer directory.
func ResolveAncient(chainData string) string {
	for _, c := range []string{
		filepath.Join(chainData, "ancient", "chain"),
		filepath.Join(chainData, "ancient"),
	} {
		if freezer.IsChainFreezerDir(c) {
			return c
		}
	}
	return ""
}

// Open opens the database read-only.
func Open(o Options) (*DB, error) {
	cd, err := ResolveChainData(o.DataDir)
	if err != nil && !(o.NoKV && o.AncientDir != "") {
		return nil, err
	}
	db := &DB{ChainDataDir: cd}
	db.AncientDir = o.AncientDir
	if db.AncientDir == "" && cd != "" {
		db.AncientDir = ResolveAncient(cd)
	}
	if db.AncientDir != "" {
		fz, err := freezer.OpenChain(db.AncientDir)
		if err != nil {
			return nil, fmt.Errorf("open freezer %s: %w", db.AncientDir, err)
		}
		db.fz = fz
		db.Frozen = fz.Frozen()
		db.Tail = fz.Tail()
		db.Warnings = append(db.Warnings, fz.Warnings()...)
	}
	if !o.NoKV {
		if o.CacheMB <= 0 {
			o.CacheMB = 1024
		}
		if o.Handles <= 0 {
			o.Handles = 4096
		}
		var kv ethdb.KeyValueStore
		switch rawdb.PreexistingDatabase(cd) {
		case rawdb.DBLeveldb:
			// Older snapshots: goleveldb, also opened read-only.
			ldb, err := gethleveldb.New(cd, o.CacheMB, o.Handles, "emextract/", true)
			if err != nil {
				db.Close()
				return nil, fmt.Errorf("open leveldb %s read-only: %w", cd, err)
			}
			kv, db.Engine = ldb, rawdb.DBLeveldb
		default:
			pdb, err := gethpebble.New(cd, o.CacheMB, o.Handles, "emextract/", true, nil)
			if err != nil {
				db.Close()
				return nil, fmt.Errorf("open pebble %s read-only: %w", cd, err)
			}
			kv, db.Engine = pdb, rawdb.DBPebble
		}
		db.kv = kv
		db.rdb = rawdb.NewDatabase(kv)
	}
	return db, nil
}

// Close closes everything.
func (db *DB) Close() {
	if db.fz != nil {
		db.fz.Close()
	}
	if db.kv != nil {
		db.kv.Close()
	}
}

// SourceWarnings returns the warnings collected while opening (recorded in
// every manifest).
func (db *DB) SourceWarnings() []string { return db.Warnings }

// HasKV reports whether a key-value store is attached.
func (db *DB) HasKV() bool { return db.kv != nil }

// CanonicalHash returns the canonical hash of block n (zero if unknown).
func (db *DB) CanonicalHash(n uint64) common.Hash {
	if db.fz != nil && n >= db.Tail && n < db.Frozen {
		items, _, err := db.fz.Hashes.Range(n, 1)
		if err == nil && len(items) == 1 && len(items[0]) == 32 {
			return common.BytesToHash(items[0])
		}
	}
	if db.rdb != nil {
		return rawdb.ReadCanonicalHash(db.rdb, n)
	}
	return common.Hash{}
}

// Head returns the head block number (from kv head-block pointer, falling back to frozen-1).
func (db *DB) Head() (uint64, bool) {
	if db.rdb != nil {
		h := rawdb.ReadHeadBlockHash(db.rdb)
		if h != (common.Hash{}) {
			if n, ok := rawdb.ReadHeaderNumber(db.rdb, h); ok {
				return n, true
			}
		}
	}
	if db.Frozen > 0 {
		return db.Frozen - 1, true
	}
	return 0, false
}

// ChainConfig reads the stored chain config (keyed by the block-0 hash).
func (db *DB) ChainConfig() *params.ChainConfig {
	if db.rdb == nil {
		return nil
	}
	g := db.CanonicalHash(0)
	if g == (common.Hash{}) {
		return nil
	}
	return rawdb.ReadChainConfig(db.rdb, g)
}

// ErrMissing is returned when a block's data is not present.
var ErrMissing = errors.New("block data missing")

// ReadRange reads blocks [from, from+count). Each returned block has all
// four parts or an error is returned naming the first missing block.
// rawBytes is bytes read from the freezer (on disk, compressed) plus kv value sizes.
func (db *DB) ReadRange(from, count uint64) ([]RawBlock, uint64, error) {
	out := make([]RawBlock, 0, count)
	var rawBytes uint64
	end := from + count
	n := from
	if db.fz != nil && n < db.Frozen {
		if n < db.Tail {
			return nil, 0, fmt.Errorf("%w: block %d below freezer tail %d (pruned history)", ErrMissing, n, db.Tail)
		}
		fend := end
		if fend > db.Frozen {
			fend = db.Frozen
		}
		c := fend - n
		hs, b1, err := db.fz.Headers.Range(n, c)
		if err != nil {
			return nil, 0, err
		}
		hh, b2, err := db.fz.Hashes.Range(n, c)
		if err != nil {
			return nil, 0, err
		}
		bs, b3, err := db.fz.Bodies.Range(n, c)
		if err != nil {
			return nil, 0, err
		}
		rs, b4, err := db.fz.Receipt.Range(n, c)
		if err != nil {
			return nil, 0, err
		}
		rawBytes += b1 + b2 + b3 + b4
		var brs [][]byte // bor state-sync receipts over [n, fend), where the table has them
		if bt := db.fz.BorReceipts; bt != nil {
			s, e := max(n, bt.Tail()), min(fend, bt.Items())
			if s < e {
				items, b5, err := bt.Range(s, e-s)
				if err != nil {
					return nil, 0, err
				}
				rawBytes += b5
				brs = make([][]byte, c)
				copy(brs[s-n:], items)
			}
		}
		for i := uint64(0); i < c; i++ {
			if len(hh[i]) != 32 {
				return nil, 0, fmt.Errorf("%w: block %d freezer hash has %d bytes", ErrMissing, n+i, len(hh[i]))
			}
			rb := RawBlock{Number: n + i, Hash: common.BytesToHash(hh[i]), Header: hs[i], Body: bs[i], Receipts: rs[i]}
			if brs != nil && len(brs[i]) > 0 {
				rb.BorReceipt = brs[i]
			}
			out = append(out, rb)
		}
		n = fend
	}
	for ; n < end; n++ {
		if db.rdb == nil {
			return nil, 0, fmt.Errorf("%w: block %d not frozen and no kv store", ErrMissing, n)
		}
		h := rawdb.ReadCanonicalHash(db.rdb, n)
		if h == (common.Hash{}) {
			return nil, 0, fmt.Errorf("%w: no canonical hash for block %d", ErrMissing, n)
		}
		hdr := rawdb.ReadHeaderRLP(db.rdb, h, n)
		body := rawdb.ReadBodyRLP(db.rdb, h, n)
		rcp := rawdb.ReadReceiptsRLP(db.rdb, h, n)
		if len(hdr) == 0 || len(body) == 0 || len(rcp) == 0 {
			return nil, 0, fmt.Errorf("%w: block %d (%s) header=%d body=%d receipts=%d bytes", ErrMissing, n, h, len(hdr), len(body), len(rcp))
		}
		rawBytes += uint64(32 + len(hdr) + len(body) + len(rcp))
		rb := RawBlock{Number: n, Hash: h, Header: hdr, Body: body, Receipts: rcp, FromKV: true}
		if db.kv != nil && db.fz != nil && db.fz.BorReceipts != nil { // bor database: unfrozen state-sync receipts live in kv
			if v, err := db.kv.Get(BorReceiptKey(n, h)); err == nil && len(v) > 0 {
				rb.BorReceipt = v
				rawBytes += uint64(len(v))
			}
		}
		out = append(out, rb)
	}
	return out, rawBytes, nil
}
