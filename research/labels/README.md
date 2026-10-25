# Institution label registry (exchanges, funds, market makers, bridges, custodians)

Named labels for institutions only (no private persons), each with evidence and a grade:

| grade | meaning | UI text |
|---|---|---|
| official | published by the institution itself (e.g. signed proof-of-reserves address list) | official |
| strong | structural on-chain evidence from our map (e.g. a deposit that swept to the entity's official wallets at least twice) | strong |
| candidate | weak evidence (a single sweep; a PoR row whose signature we could not verify) | presumed / 추정 |

No probabilities are invented.

## Sources

| id | what | licence / terms | status |
|---|---|---|---|
| okx_por | OKX monthly Proof-of-Reserves address file (`static.okx.com/cdn/okx/por/chain/por_csv_<date>_V*.zip`, linked from okx.com/proof-of-reserves/download). EVM rows are signed "I am an OKX address"; we re-verify each signature with OKX's scheme `keccak256("\x19Ethereum Signed Message:\n32" ‖ keccak256(msg))` (okx/proof-of-reserves, MIT). | published by OKX for public verification | 2026-10-03 file in use; copy in Blob `research/sources/okx/` |
| DefiLlama-Adapters CEX lists | addresses per CEX | repository has no licence (GitHub: none; package.json: none) | **not used** |
| Dune Spellbook `cex.addresses` | curated CEX addresses | Business Source License 1.1 | **not used** |
| Etherscan / Arkham labels | label databases | terms forbid scraping | **never used** |

Exchanges whose PoR pages render the address list with JavaScript (Binance "Download All Address", Bybit audit data,
Bitget, KuCoin, Gate, Crypto.com, HTX, MEXC, Bitfinex) block plain HTTP fetches. Their lists have to be downloaded once
in a browser (the official "download addresses" button) and dropped into Blob `research/sources/<exchange>/`; then add a
parser next to `okx_por.py` (verify signatures where the file provides them) and an entry in `entities.json`.

## Pipeline (skew, low memory; large files stay in Blob)

1. `okx_por.py <por.zip> <out.parquet>`: chain rows for our chains + signature check.
2. `build_seed.py`: graded seed (verified -> official, else candidate).
3. `sweep_expand.py <chain> <seed> <labels.parquet> <edges glob> <out>`: classifier sweep hubs that send >= 50% to the entity's
   official wallets -> strong; classifier deposits whose top counterparty (>= 80% of transfers) is an official wallet or such a
   hub -> strong if swept at least twice, else candidate. Evidence: counterparty, transfer counts, block range. Run on chains
   whose map is local (arbitrum, robinhood); for the others, run it against Blob map edges (DuckDB httpfs) or on a VM.
4. `build_registry.py <workdir>`: `out/addresses.parquet`, `out/entities.parquet`, `out/<chain>/research_labels.parquet`.
5. Upload: Blob `research/labels/`, Blob `<chain>/graphloop/export/research_labels.parquet`, and skew
   `/srv/skew/em-graphloop/export/{arbitrum,robinhood}/research_labels.parquet` (tmp + rename). The compact serving and the
   galaxy agent overlay research rows first (official > strong > candidate > agent > classifier) and re-pull on ETag / mtime.

## Next

- Seeds for Binance, Bybit, Bitget, KuCoin, Gate, Crypto.com, HTX, MEXC, Coinbase, Kraken (official lists, browser download).
- Sweep expansion on Ethereum, Base, OP, Polygon, Linea, Unichain, X Layer (their maps are in Blob).
- Cross-chain: same address signed on several chains (EOA) -> same entity, from the cross-chain layer's same-key judgement.
- Funds / market makers / bridges / custodians: official announcements, governance delegate disclosures, vesting contracts,
  published Safe addresses; Muse only to summarise documents, never to invent a name without on-chain evidence.
