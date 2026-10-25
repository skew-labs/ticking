// Package freezer is a minimal, strictly read-only reader for geth's ancient
// "chain" freezer (headers / hashes / bodies / receipts tables).
//
// It deliberately does NOT use rawdb.NewChainFreezer: geth's freezer, even in
// read-only mode, takes a lock file, MkdirAll's table directories, requires
// every table it knows about (including tables added by newer forks such as
// "bals") to exist, and its repair path may delete "leftover" data files.
// This reader only ever opens files with O_RDONLY and uses pread (ReadAt), so
// it is safe for concurrent use from many goroutines and never mutates the
// snapshot.
//
// On-disk layout (identical to geth core/rawdb/freezer_table.go):
//
//	<name>.cidx | <name>.ridx   index: N+1 entries of 6 bytes {uint16 filenum, uint32 offset} (big-endian)
//	<name>.meta                 RLP {Version uint16, VirtualTail uint64, FlushOffset uint64 (v2)}
//	<name>.NNNN.cdat | .rdat    data files; item i lives in file entry[i+1].filenum
//	                            between entry[i].offset (or 0 if the file changed) and entry[i+1].offset
//
// Index entry 0 is special: filenum = first (tail) data file, offset = number
// of items deleted from the tail ("itemOffset"). "c" tables are snappy (block
// format) compressed per item, "r" tables are raw.
package freezer

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/ethereum/go-ethereum/rlp"
	"github.com/golang/snappy"
)

const indexEntrySize = 6

// Table names of the chain freezer.
const (
	Headers  = "headers"
	Hashes   = "hashes"
	Bodies   = "bodies"
	Receipts = "receipts"
	// BorReceipts is bor's (Polygon PoS) pre-PIP-74 state-sync receipt table; optional.
	BorReceipts = "matic-bor-receipts"
)

// ErrOutOfBounds is returned for item ranges outside the readable window.
var ErrOutOfBounds = errors.New("freezer: out of bounds")

type indexEntry struct {
	filenum uint32
	offset  uint32
}

func parseEntry(b []byte) indexEntry {
	return indexEntry{filenum: uint32(binary.BigEndian.Uint16(b[:2])), offset: binary.BigEndian.Uint32(b[2:6])}
}

// Table is a read-only freezer table.
type Table struct {
	Name       string
	dir        string
	compressed bool

	index      *os.File
	entries    uint64 // number of usable index entries (>=1)
	itemOffset uint64 // items deleted from tail (index entry 0 offset)
	hidden     uint64 // virtual tail (>= itemOffset)
	items      uint64 // total items = itemOffset + entries - 1

	Warnings []string

	mu    sync.Mutex
	files map[uint32]*os.File
}

type metaV2 struct {
	Version uint16
	Tail    uint64
	Offset  uint64 `rlp:"optional"`
}

// OpenTable opens one freezer table read-only.
func OpenTable(dir, name string, compressed bool) (*Table, error) {
	ext := "ridx"
	if compressed {
		ext = "cidx"
	}
	idxPath := filepath.Join(dir, name+"."+ext)
	index, err := os.OpenFile(idxPath, os.O_RDONLY, 0)
	if err != nil {
		return nil, err
	}
	t := &Table{Name: name, dir: dir, compressed: compressed, index: index, files: map[uint32]*os.File{}}
	st, err := index.Stat()
	if err != nil {
		index.Close()
		return nil, err
	}
	size := st.Size()
	if size == 0 {
		t.entries = 1 // brand-new empty table
		return t, nil
	}
	if size%indexEntrySize != 0 {
		index.Close()
		return nil, fmt.Errorf("freezer %s: index size %d not a multiple of %d", name, size, indexEntrySize)
	}
	// Metadata (optional for very old freezers).
	var meta metaV2
	haveMeta := false
	if raw, err := os.ReadFile(filepath.Join(dir, name+".meta")); err == nil && len(raw) > 0 {
		if err := rlp.DecodeBytes(raw, &meta); err != nil {
			index.Close()
			return nil, fmt.Errorf("freezer %s: bad meta: %w", name, err)
		}
		if meta.Version != 1 && meta.Version != 2 {
			index.Close()
			return nil, fmt.Errorf("freezer %s: unsupported meta version %d", name, meta.Version)
		}
		haveMeta = true
	}
	usable := size
	if haveMeta && meta.Version == 2 && int64(meta.Offset) < size && meta.Offset >= indexEntrySize {
		// Entries beyond flushOffset were never fsync'd; geth would truncate
		// them on a writable open. Treat them as absent (they must still be in
		// the key-value store in that case).
		t.Warnings = append(t.Warnings, fmt.Sprintf("%s: %d index bytes beyond flushOffset ignored", name, size-int64(meta.Offset)))
		usable = int64(meta.Offset) - int64(meta.Offset)%indexEntrySize
	}
	t.entries = uint64(usable / indexEntrySize)
	var buf [indexEntrySize]byte
	if _, err := index.ReadAt(buf[:], 0); err != nil {
		index.Close()
		return nil, err
	}
	first := parseEntry(buf[:])
	t.itemOffset = uint64(first.offset)
	t.items = t.itemOffset + t.entries - 1
	t.hidden = t.itemOffset
	if haveMeta && meta.Tail > t.hidden {
		t.hidden = meta.Tail
	}
	if t.hidden > t.items {
		t.hidden = t.items
	}
	// Sanity check the head data file length against the last index entry.
	if t.entries > 1 {
		if _, err := index.ReadAt(buf[:], int64(t.entries-1)*indexEntrySize); err != nil {
			index.Close()
			return nil, err
		}
		last := parseEntry(buf[:])
		f, err := t.file(last.filenum)
		switch {
		case errors.Is(err, os.ErrNotExist):
			// A snapshot split into archives (PublicNode base + part) can hold the full index without the head data
			// files; items in absent data files fail when they are read.
			t.Warnings = append(t.Warnings, fmt.Sprintf("%s: head data file %d absent (partial freezer)", name, last.filenum))
		case err != nil:
			index.Close()
			return nil, err
		default:
			fst, err := f.Stat()
			if err != nil {
				index.Close()
				return nil, err
			}
			if fst.Size() < int64(last.offset) {
				index.Close()
				return nil, fmt.Errorf("freezer %s: head file %d is %d bytes, index expects %d (truncated snapshot?)", name, last.filenum, fst.Size(), last.offset)
			}
			if fst.Size() > int64(last.offset) {
				t.Warnings = append(t.Warnings, fmt.Sprintf("%s: %d dangling bytes after last indexed item (ignored)", name, fst.Size()-int64(last.offset)))
			}
		}
	}
	return t, nil
}

func (t *Table) dataName(num uint32) string {
	ext := "rdat"
	if t.compressed {
		ext = "cdat"
	}
	return filepath.Join(t.dir, fmt.Sprintf("%s.%04d.%s", t.Name, num, ext))
}

func (t *Table) file(num uint32) (*os.File, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if f, ok := t.files[num]; ok {
		return f, nil
	}
	f, err := os.OpenFile(t.dataName(num), os.O_RDONLY, 0)
	if err != nil {
		return nil, err
	}
	t.files[num] = f
	return f, nil
}

// Items returns the total number of items (the next item number to be written).
func (t *Table) Items() uint64 { return t.items }

// Tail returns the first readable item number.
func (t *Table) Tail() uint64 { return t.hidden }

// Range reads items [start, start+count). Each returned slice is freshly
// allocated (decompressed for "c" tables). rawBytes is the on-disk size read.
func (t *Table) Range(start, count uint64) (out [][]byte, rawBytes uint64, err error) {
	if count == 0 {
		return nil, 0, nil
	}
	if start < t.hidden || start+count > t.items || start+count < start {
		return nil, 0, fmt.Errorf("%w: %s [%d,%d) readable [%d,%d)", ErrOutOfBounds, t.Name, start, start+count, t.hidden, t.items)
	}
	rel := start - t.itemOffset
	ibuf := make([]byte, (count+1)*indexEntrySize)
	if _, err := t.index.ReadAt(ibuf, int64(rel)*indexEntrySize); err != nil {
		return nil, 0, fmt.Errorf("freezer %s: index read: %w", t.Name, err)
	}
	entries := make([]indexEntry, count+1)
	for i := range entries {
		entries[i] = parseEntry(ibuf[i*indexEntrySize:])
	}
	if rel == 0 {
		// Entry 0 carries the deleted-item count, not a data offset; the first
		// stored item always starts at offset 0 of the tail file.
		entries[0].offset = 0
	}
	type span struct{ file, from, to uint32 }
	spans := make([]span, count)
	for i := uint64(0); i < count; i++ {
		a, b := entries[i], entries[i+1]
		s := span{file: b.filenum, from: a.offset, to: b.offset}
		if a.filenum != b.filenum {
			s.from = 0
		}
		if s.to < s.from {
			return nil, 0, fmt.Errorf("freezer %s: corrupt index at item %d", t.Name, start+i)
		}
		spans[i] = s
	}
	out = make([][]byte, count)
	// Coalesce contiguous spans in the same file into one pread.
	for i := uint64(0); i < count; {
		j := i + 1
		for j < count && spans[j].file == spans[i].file && spans[j].from == spans[j-1].to && spans[j].to-spans[i].from <= 64<<20 {
			j++
		}
		f, err := t.file(spans[i].file)
		if err != nil {
			return nil, 0, err
		}
		buf := make([]byte, spans[j-1].to-spans[i].from)
		if _, err := f.ReadAt(buf, int64(spans[i].from)); err != nil {
			return nil, 0, fmt.Errorf("freezer %s: data read file %d [%d,%d): %w", t.Name, spans[i].file, spans[i].from, spans[j-1].to, err)
		}
		rawBytes += uint64(len(buf))
		for k := i; k < j; k++ {
			item := buf[spans[k].from-spans[i].from : spans[k].to-spans[i].from]
			if t.compressed {
				dec, err := snappy.Decode(nil, item)
				if err != nil {
					return nil, 0, fmt.Errorf("freezer %s: snappy item %d: %w", t.Name, start+k, err)
				}
				out[k] = dec
			} else {
				out[k] = item
			}
		}
		i = j
	}
	return out, rawBytes, nil
}

// Close releases all file handles.
func (t *Table) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, f := range t.files {
		f.Close()
	}
	t.files = nil
	return t.index.Close()
}

// Chain bundles the four chain tables, plus bor's state-sync receipts when present.
type Chain struct {
	Dir                              string
	Headers, Hashes, Bodies, Receipt *Table
	BorReceipts                      *Table // nil unless the freezer has matic-bor-receipts (not counted in Frozen/Tail)
}

// IsChainFreezerDir reports whether dir holds a chain freezer.
func IsChainFreezerDir(dir string) bool {
	_, err := os.Stat(filepath.Join(dir, Headers+".cidx"))
	return err == nil
}

// OpenChain opens the chain freezer tables in dir read-only.
func OpenChain(dir string) (*Chain, error) {
	c := &Chain{Dir: dir}
	var err error
	if c.Headers, err = OpenTable(dir, Headers, true); err != nil {
		return nil, err
	}
	if c.Hashes, err = OpenTable(dir, Hashes, false); err != nil {
		c.Close()
		return nil, err
	}
	if c.Bodies, err = OpenTable(dir, Bodies, true); err != nil {
		c.Close()
		return nil, err
	}
	if c.Receipt, err = OpenTable(dir, Receipts, true); err != nil {
		c.Close()
		return nil, err
	}
	if _, err := os.Stat(filepath.Join(dir, BorReceipts+".cidx")); err == nil {
		if c.BorReceipts, err = OpenTable(dir, BorReceipts, true); err != nil {
			c.Close()
			return nil, err
		}
	}
	return c, nil
}

// Frozen returns the number of blocks fully present in all four tables.
func (c *Chain) Frozen() uint64 {
	n := c.Headers.Items()
	for _, t := range []*Table{c.Hashes, c.Bodies, c.Receipt} {
		if t.Items() < n {
			n = t.Items()
		}
	}
	return n
}

// Tail returns the first block number readable from all four tables.
func (c *Chain) Tail() uint64 {
	n := uint64(0)
	for _, t := range c.tables() {
		if t.Tail() > n {
			n = t.Tail()
		}
	}
	return n
}

// Warnings aggregates table warnings.
func (c *Chain) Warnings() []string {
	var w []string
	for _, t := range c.tables() {
		w = append(w, t.Warnings...)
	}
	return w
}

func (c *Chain) tables() []*Table {
	var ts []*Table
	for _, t := range []*Table{c.Headers, c.Hashes, c.Bodies, c.Receipt} {
		if t != nil {
			ts = append(ts, t)
		}
	}
	return ts
}

// Close closes all tables.
func (c *Chain) Close() {
	for _, t := range c.tables() {
		t.Close()
	}
	if c.BorReceipts != nil {
		c.BorReceipts.Close()
	}
}
