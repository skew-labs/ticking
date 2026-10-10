# Source and dependency notices

Ticking-owned source is made public for review. No new general redistribution or commercial license is granted by this publication. The existing `packages/chart-sdk/LICENSE.md` remains attached to that package. This is not a claim that the whole repository has an OSI open-source license.

Native dependencies include the standard C++ library, OpenSSL, zlib, libdeflate, Zstandard, SQLite, libcurl, gRPC, Protocol Buffers and nlohmann/json. Python dependencies are listed in the requirements files. These are external dependencies under their own licenses; compiled copies are not included.

The Solana stream interface is pinned to the upstream revision and hashes in `engine/proto-provenance.json`. `scripts/fetch-proto.py` obtains the interface definitions and upstream license into an ignored build dependency directory. The external interface is not represented as Ticking-authored code. Review the upstream license before distributing a combined build. No Firedancer or third-party chart implementation is included.

Provider names, program addresses and instrument identities in adapter source are needed for interoperability and attribution. They are not endorsements. Venue market data, logos and historical datasets are not licensed by this source publication. No captured production data or customer secrets are included.
