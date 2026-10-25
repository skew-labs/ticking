// Command gen writes a synthetic Nitro-style chain database (pebble +
// ancient freezer) for smoke tests and local benchmarks of em-extract.
//
//	go run ./internal/testchain/gen -out /tmp/synth/l2chaindata -blocks 20000 -frozen 19000 -txs 4 -logs 4
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/em-mapping/extractor/internal/testchain"
)

func main() {
	out := flag.String("out", "", "output l2chaindata dir (must not exist)")
	blocks := flag.Uint64("blocks", 10000, "blocks")
	frozen := flag.Uint64("frozen", 9000, "blocks in the freezer")
	nitro := flag.Uint64("nitro-genesis", 1000, "first Nitro block")
	txs := flag.Int("txs", 4, "extra signed txs per Nitro block")
	logs := flag.Int("logs", 4, "logs per extra tx")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "-out required")
		os.Exit(1)
	}
	if _, err := os.Stat(*out); err == nil {
		fmt.Fprintln(os.Stderr, "output exists")
		os.Exit(1)
	}
	os.MkdirAll(*out, 0o755)
	e, err := testchain.Build(*out, testchain.Spec{Blocks: *blocks, Frozen: *frozen, NitroGenesis: *nitro, ExtraTxs: *txs, ExtraLogs: *logs})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("blocks=%d txs=%d logs=%d transfers=%d creations=%d\n", *blocks, e.Txs, e.Logs, e.Transfers, e.Creations)
}
