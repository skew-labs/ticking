package freezer

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb"
	gethpebble "github.com/ethereum/go-ethereum/ethdb/pebble"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/golang/snappy"

	"github.com/em-mapping/extractor/internal/testchain"
)

// Items read by this reader must equal what geth's own freezer returns, and
// the reader must honour a truncated (pruned) tail.
func TestMatchesGethFreezerAndTail(t *testing.T) {
	dir := t.TempDir()
	if _, err := testchain.Build(dir, testchain.Spec{Blocks: 120, Frozen: 120, NitroGenesis: 40}); err != nil {
		t.Fatal(err)
	}
	anc := filepath.Join(dir, "ancient", "chain")
	c, err := OpenChain(anc)
	if err != nil {
		t.Fatal(err)
	}
	if c.Frozen() != 120 || c.Tail() != 0 {
		t.Fatalf("frozen %d tail %d", c.Frozen(), c.Tail())
	}
	ours := map[string][][]byte{}
	for _, tb := range []*Table{c.Headers, c.Hashes, c.Bodies, c.Receipt} {
		items, _, err := tb.Range(0, 120)
		if err != nil {
			t.Fatal(err)
		}
		ours[tb.Name] = items
	}
	c.Close()

	// Re-open with geth (writable) to compare and then prune the tail.
	kvdir := dir
	db, err := openGeth(kvdir)
	if err != nil {
		t.Fatal(err)
	}
	for name, items := range ours {
		ref, err := db.AncientRange(name, 0, 120, 0)
		if err != nil {
			t.Fatal(err)
		}
		for i := range ref {
			if !bytes.Equal(ref[i], items[i]) {
				t.Fatalf("%s item %d differs from geth", name, i)
			}
		}
	}
	if _, err := db.TruncateTail(rawdb.ChainFreezerBlockDataGroup, 30); err != nil {
		t.Fatal(err)
	}
	db.Close()

	c, err = OpenChain(anc)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if c.Tail() != 30 {
		t.Fatalf("tail after prune %d", c.Tail())
	}
	if _, _, err := c.Bodies.Range(29, 2); err == nil {
		t.Fatal("read below tail succeeded")
	}
	got, _, err := c.Receipt.Range(30, 90)
	if err != nil || !bytes.Equal(got[0], ours[Receipts][30]) || !bytes.Equal(got[89], ours[Receipts][119]) {
		t.Fatalf("post-prune read %v", err)
	}
	// headers are not prunable
	if _, _, err := c.Headers.Range(0, 1); err != nil {
		t.Fatal(err)
	}
}

// Hand-built table spanning two data files with 5 items deleted from the tail.
func TestMultiFileAndDeletedTail(t *testing.T) {
	dir := t.TempDir()
	items := [][]byte{[]byte("alpha"), []byte("bravo-bravo"), []byte(""), []byte("delta"), []byte("echo-echo-echo")}
	// items 5..6 in file 3, items 7..9 in file 4 (5 deleted items lived in files 0..2).
	var idx []byte
	put := func(file uint16, off uint32) {
		var b [6]byte
		binary.BigEndian.PutUint16(b[:2], file)
		binary.BigEndian.PutUint32(b[2:], off)
		idx = append(idx, b[:]...)
	}
	put(3, 5) // tail entry: first file 3, 5 deleted items
	files := map[uint16]*bytes.Buffer{3: {}, 4: {}}
	for i, it := range items {
		f := uint16(3)
		if i >= 2 {
			f = 4
		}
		files[f].Write(snappy.Encode(nil, it))
		put(f, uint32(files[f].Len()))
	}
	os.WriteFile(filepath.Join(dir, "t.cidx"), idx, 0o644)
	for f, b := range files {
		os.WriteFile(filepath.Join(dir, fmt.Sprintf("t.%04d.cdat", f)), b.Bytes(), 0o644)
	}
	meta, _ := rlp.EncodeToBytes(&metaV2{Version: 2, Tail: 5, Offset: uint64(len(idx))})
	os.WriteFile(filepath.Join(dir, "t.meta"), meta, 0o644)
	tb, err := OpenTable(dir, "t", true)
	if err != nil {
		t.Fatal(err)
	}
	defer tb.Close()
	if tb.Tail() != 5 || tb.Items() != 10 {
		t.Fatalf("tail %d items %d", tb.Tail(), tb.Items())
	}
	got, _, err := tb.Range(5, 5)
	if err != nil {
		t.Fatal(err)
	}
	for i := range items {
		if !bytes.Equal(got[i], items[i]) {
			t.Fatalf("item %d = %q want %q", 5+i, got[i], items[i])
		}
	}
	one, _, err := tb.Range(7, 1)
	if err != nil || string(one[0]) != "" {
		t.Fatalf("cross-file single item %q %v", one, err)
	}
	if _, _, err := tb.Range(4, 1); err == nil {
		t.Fatal("deleted item readable")
	}
	if _, _, err := tb.Range(9, 2); err == nil {
		t.Fatal("beyond head readable")
	}
}

func openGeth(dir string) (ethdb.Database, error) {
	kv, err := gethpebble.New(dir, 16, 16, "", false, nil)
	if err != nil {
		return nil, err
	}
	return rawdb.Open(kv, rawdb.OpenOptions{Ancient: filepath.Join(dir, "ancient")})
}
