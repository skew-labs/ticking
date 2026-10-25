package rpcsrc

import (
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/common/hexutil"

	"github.com/em-mapping/extractor/internal/chaindb"
	"github.com/em-mapping/extractor/internal/chunk"
)

func newBig(v uint64) *big.Int { return new(big.Int).SetUint64(v) }

// Source adapts a Pool to extract.Source.
type Source struct {
	ctx  context.Context
	pool *Pool

	mu     sync.Mutex
	hashes map[uint64]common.Hash // last block hash of fetched windows and of finished chunks (from manifests)
	warns  []string
}

// NewSource creates the source. outDir (optional) is scanned for existing OK
// manifests so that a chunk's first parent hash is checked against the
// previous chunk's recorded last_block_hash (e.g. the em-extract snapshot
// chunk ending at the snapshot head) instead of a fresh RPC answer.
func NewSource(ctx context.Context, pool *Pool, outDir string) *Source {
	s := &Source{ctx: ctx, pool: pool, hashes: map[uint64]common.Hash{}}
	if outDir != "" {
		s.LoadManifests(outDir)
	}
	return s
}

// LoadManifests indexes last_block_hash of every OK manifest under dir.
func (s *Source) LoadManifests(dir string) int {
	files, _ := filepath.Glob(filepath.Join(dir, "manifests", "*.json"))
	n := 0
	for _, f := range files {
		if strings.Contains(filepath.Base(f), ".tmp-") {
			continue
		}
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		var m chunk.Manifest
		if json.Unmarshal(b, &m) != nil || m.Status != chunk.StatusOK {
			continue
		}
		if h, ok := m.Extra["last_block_hash"].(string); ok && len(h) == 66 {
			s.Remember(m.Range.To, common.HexToHash(h))
			n++
		}
	}
	return n
}

// Remember records a known canonical hash.
func (s *Source) Remember(n uint64, h common.Hash) {
	s.mu.Lock()
	s.hashes[n] = h
	s.mu.Unlock()
}

// SourceWarnings implements the extract warner interface.
func (s *Source) SourceWarnings() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.warns...)
}

// ReadRange implements extract.Source.
func (s *Source) ReadRange(from, count uint64) ([]chaindb.RawBlock, uint64, error) {
	raws, n, err := s.pool.Fetch(s.ctx, from, count)
	if err != nil {
		return nil, 0, fmt.Errorf("%w: %v", chaindb.ErrMissing, err)
	}
	if len(raws) > 0 {
		last := raws[len(raws)-1]
		s.Remember(last.Number, last.Hash)
	}
	return raws, n, nil
}

// CanonicalHash implements extract.Source: a remembered hash (previous
// window / chunk manifest) or eth_getBlockByNumber(n, false).hash.
func (s *Source) CanonicalHash(n uint64) common.Hash {
	s.mu.Lock()
	h, ok := s.hashes[n]
	s.mu.Unlock()
	if ok {
		return h
	}
	res, err := s.pool.Call(s.ctx, "eth_getBlockByNumber", hexutil.EncodeUint64(n), false)
	if err != nil || isNull(res) {
		return common.Hash{}
	}
	var hdr struct {
		Hash   common.Hash    `json:"hash"`
		Number hexutil.Uint64 `json:"number"`
	}
	if json.Unmarshal(res, &hdr) != nil || uint64(hdr.Number) != n {
		return common.Hash{}
	}
	return hdr.Hash
}

// Head returns the lowest head reported by the endpoints that answered (so
// every endpoint can serve blocks up to it) and per-endpoint heads.
func (p *Pool) Head(ctx context.Context) (uint64, []string, error) {
	res, errs := p.CallEach(ctx, "eth_blockNumber")
	var lo uint64
	have := false
	var info []string
	for i, r := range res {
		if errs[i] != nil || r == nil {
			info = append(info, fmt.Sprintf("%s=error(%v)", p.eps[i].host, errs[i]))
			continue
		}
		var h hexutil.Uint64
		if json.Unmarshal(r, &h) != nil {
			info = append(info, fmt.Sprintf("%s=bad(%s)", p.eps[i].host, r))
			continue
		}
		info = append(info, fmt.Sprintf("%s=%d", p.eps[i].host, uint64(h)))
		if !have || uint64(h) < lo {
			lo, have = uint64(h), true
		}
	}
	if !have {
		return 0, info, fmt.Errorf("no endpoint returned a head: %s", strings.Join(info, " "))
	}
	return lo, info, nil
}

// CheckChainID verifies every endpoint serves the expected chain.
func (p *Pool) CheckChainID(ctx context.Context, want uint64) error {
	res, errs := p.CallEach(ctx, "eth_chainId")
	var bad []string
	for i, r := range res {
		if errs[i] != nil {
			bad = append(bad, fmt.Sprintf("%s: %v", p.eps[i].host, errs[i]))
			continue
		}
		var id hexutil.Uint64
		if json.Unmarshal(r, &id) != nil || uint64(id) != want {
			bad = append(bad, fmt.Sprintf("%s: chain id %s != %d", p.eps[i].host, r, want))
		}
	}
	if len(bad) > 0 {
		return fmt.Errorf("endpoint check failed: %s", strings.Join(bad, "; "))
	}
	return nil
}
