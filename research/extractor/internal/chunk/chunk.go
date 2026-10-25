// Package chunk plans block ranges into aligned chunks and handles
// per-chunk manifests (atomic write, validation for resume).
package chunk

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/em-mapping/extractor/internal/pq"
)

// Status values.
const (
	StatusOK               = "OK"
	StatusFailed           = "FAILED"
	StatusTraceUnavailable = "TRACE_UNAVAILABLE"
)

// Range is an inclusive block range.
type Range struct {
	From uint64 `json:"from"`
	To   uint64 `json:"to"`
}

// Len returns the number of blocks.
func (r Range) Len() uint64 { return r.To - r.From + 1 }

// Name returns the canonical chunk name.
func (r Range) Name() string { return fmt.Sprintf("%010d-%010d", r.From, r.To) }

// Plan splits [from,to] into chunks aligned to multiples of size.
func Plan(from, to, size uint64) []Range {
	if size == 0 {
		size = 1
	}
	var out []Range
	for s := from; s <= to; {
		e := (s/size+1)*size - 1
		if e > to {
			e = to
		}
		out = append(out, Range{From: s, To: e})
		if e == to {
			break
		}
		s = e + 1
	}
	return out
}

// Failure describes why a chunk failed.
type Failure struct {
	Block  uint64 `json:"block"`
	Reason string `json:"reason"`
	Detail string `json:"detail,omitempty"`
}

// Manifest is written last for every chunk; its presence + validation marks
// the chunk done.
type Manifest struct {
	Tool        string                 `json:"tool"`
	Version     string                 `json:"version"`
	Dependency  string                 `json:"dependency"`
	ChainID     uint64                 `json:"chain_id"`
	Range       Range                  `json:"range"`
	Status      string                 `json:"status"`
	Failure     *Failure               `json:"failure,omitempty"`
	Verify      *VerifyInfo            `json:"verify,omitempty"`
	Counts      map[string]uint64      `json:"counts"`
	Files       map[string]pq.FileInfo `json:"files"`
	Stats       any                    `json:"stats,omitempty"`
	Warnings    []string               `json:"warnings,omitempty"`
	ElapsedSec  float64                `json:"elapsed_sec"`
	BlocksPerS  float64                `json:"blocks_per_sec"`
	CreatedAt   string                 `json:"created_at"`
	Unavailable *Unavailable           `json:"trace_unavailable,omitempty"`
	Extra       map[string]any         `json:"extra,omitempty"`
}

// VerifyInfo records what was verified.
type VerifyInfo struct {
	Enabled        bool     `json:"enabled"`
	Checks         []string `json:"checks"`
	BlocksVerified uint64   `json:"blocks_verified"`
	PrevLink       string   `json:"prev_chunk_link"` // "verified" | "unavailable" | "n/a" | "skipped"
}

// Unavailable records blocks that could not be traced.
type Unavailable struct {
	From   uint64 `json:"from"`
	To     uint64 `json:"to"`
	Count  uint64 `json:"count"`
	Reason string `json:"reason"`
}

// Path returns the manifest path for a chunk.
func Path(root string, r Range) string {
	return filepath.Join(root, "manifests", r.Name()+".json")
}

// Write writes the manifest atomically.
func Write(root string, m *Manifest) error {
	if m.CreatedAt == "" {
		m.CreatedAt = time.Now().UTC().Format(time.RFC3339)
	}
	p := Path(root, m.Range)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := fmt.Sprintf("%s.tmp-%d", p, os.Getpid())
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	f.Close()
	return os.Rename(tmp, p)
}

// Read loads a manifest.
func Read(root string, r Range) (*Manifest, error) {
	b, err := os.ReadFile(Path(root, r))
	if err != nil {
		return nil, err
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// Expect describes what a resumable manifest must satisfy.
type Expect struct {
	Tool         string
	ChainID      uint64
	Statuses     []string // acceptable statuses
	RequireFiles []string // table names that must be present
	NeedVerify   bool
	CheckSHA     bool
}

// Validate returns nil if the chunk's manifest exists and validates.
func Validate(root string, r Range, e Expect) (*Manifest, error) {
	m, err := Read(root, r)
	if err != nil {
		return nil, err
	}
	if m.Tool != e.Tool || m.ChainID != e.ChainID || m.Range != r {
		return m, fmt.Errorf("manifest identity mismatch (tool=%s chain=%d range=%v)", m.Tool, m.ChainID, m.Range)
	}
	okStatus := false
	for _, s := range e.Statuses {
		if m.Status == s {
			okStatus = true
		}
	}
	if !okStatus {
		return m, fmt.Errorf("manifest status %s", m.Status)
	}
	if e.NeedVerify && (m.Verify == nil || !m.Verify.Enabled) {
		return m, fmt.Errorf("chunk was produced without verification")
	}
	for _, t := range e.RequireFiles {
		if _, ok := m.Files[t]; !ok {
			return m, fmt.Errorf("manifest lacks file for %s", t)
		}
	}
	for name, fi := range m.Files {
		p := filepath.Join(root, filepath.FromSlash(fi.Path))
		st, err := os.Stat(p)
		if err != nil {
			return m, fmt.Errorf("%s: %w", name, err)
		}
		if st.Size() != fi.Bytes {
			return m, fmt.Errorf("%s: size %d != manifest %d", name, st.Size(), fi.Bytes)
		}
		if e.CheckSHA {
			sum, _, err := pq.FileSHA256(p)
			if err != nil {
				return m, err
			}
			if sum != fi.SHA256 {
				return m, fmt.Errorf("%s: sha256 mismatch", name)
			}
		}
	}
	return m, nil
}

// RemoveFiles deletes the files listed in a (stale) manifest and any tmp files.
func RemoveFiles(root string, tables []string, r Range) {
	for _, t := range tables {
		base := filepath.Join(root, t, t+"_"+r.Name()+".parquet")
		os.Remove(base)
		matches, _ := filepath.Glob(base + ".tmp-*")
		for _, m := range matches {
			os.Remove(m)
		}
	}
}

// FileRel returns the relative path of a table file for a chunk.
func FileRel(table string, r Range) string {
	return filepath.ToSlash(filepath.Join(table, table+"_"+r.Name()+".parquet"))
}
