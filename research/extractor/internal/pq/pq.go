// Package pq writes one Parquet file atomically (tmp file + fsync + rename),
// zstd-compressed, hashing the bytes while they are written.
package pq

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"

	"github.com/parquet-go/parquet-go"
	"github.com/parquet-go/parquet-go/compress/zstd"
)

// FileInfo describes a finished file (recorded in manifests).
type FileInfo struct {
	Path   string `json:"path"` // relative to the output root
	Rows   uint64 `json:"rows"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

type hashCounter struct {
	w io.Writer
	h hash.Hash
	n int64
}

func (c *hashCounter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.h.Write(p[:n])
	c.n += int64(n)
	return n, err
}

// Writer is a typed atomic Parquet writer.
type Writer[T any] struct {
	root, rel, tmp string
	f              *os.File
	hc             *hashCounter
	w              *parquet.GenericWriter[T]
	rows           uint64
}

// Codec is the shared zstd codec (level: SpeedDefault ~ zstd 3).
var Codec = &zstd.Codec{Level: zstd.SpeedDefault}

// Create opens <root>/<rel>.tmp-<pid> for writing.
func Create[T any](root, rel string, rowGroupRows int64, meta map[string]string) (*Writer[T], error) {
	final := filepath.Join(root, rel)
	if err := os.MkdirAll(filepath.Dir(final), 0o755); err != nil {
		return nil, err
	}
	tmp := fmt.Sprintf("%s.tmp-%d", final, os.Getpid())
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	hc := &hashCounter{w: f, h: sha256.New()}
	opts := []parquet.WriterOption{
		parquet.Compression(Codec),
		parquet.MaxRowsPerRowGroup(rowGroupRows),
		parquet.PageBufferSize(1 << 20),
		parquet.CreatedBy("em-extract", "0.1.0", ""),
	}
	for k, v := range meta {
		opts = append(opts, parquet.KeyValueMetadata(k, v))
	}
	return &Writer[T]{root: root, rel: rel, tmp: tmp, f: f, hc: hc, w: parquet.NewGenericWriter[T](hc, opts...)}, nil
}

// Write appends rows.
func (w *Writer[T]) Write(rows []T) error {
	if len(rows) == 0 {
		return nil
	}
	n, err := w.w.Write(rows)
	w.rows += uint64(n)
	if err == nil && n != len(rows) {
		err = io.ErrShortWrite
	}
	return err
}

// Rows written so far.
func (w *Writer[T]) Rows() uint64 { return w.rows }

// Commit closes, fsyncs and renames into place.
func (w *Writer[T]) Commit() (FileInfo, error) {
	if err := w.w.Close(); err != nil {
		w.Abort()
		return FileInfo{}, err
	}
	if err := w.f.Sync(); err != nil {
		w.Abort()
		return FileInfo{}, err
	}
	if err := w.f.Close(); err != nil {
		os.Remove(w.tmp)
		return FileInfo{}, err
	}
	final := filepath.Join(w.root, w.rel)
	if err := os.Rename(w.tmp, final); err != nil {
		os.Remove(w.tmp)
		return FileInfo{}, err
	}
	return FileInfo{Path: filepath.ToSlash(w.rel), Rows: w.rows, Bytes: w.hc.n, SHA256: hex.EncodeToString(w.hc.h.Sum(nil))}, nil
}

// Abort discards the temporary file.
func (w *Writer[T]) Abort() {
	if w.f != nil {
		w.f.Close()
	}
	os.Remove(w.tmp)
}

// FileSHA256 hashes an existing file.
func FileSHA256(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.CopyBuffer(h, f, make([]byte, 1<<20))
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}
