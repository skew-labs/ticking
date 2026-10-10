# Building the selected source

Use Linux for native collectors and durability tests. The public source does not start a production collector, download market history, fund an account, or submit a transaction during a build.

System packages: a C++20 compiler, pkg-config, protobuf compiler, gRPC C++ plugin and development packages for gRPC, protobuf, OpenSSL, zlib, libdeflate, Zstandard, SQLite, libcurl and nlohmann/json. The venue collector needs a libcurl build with WebSocket support. Node 22+ is used for module checks. Python 3.12+ needs the pinned `requirements.lock` in an isolated environment. Direct dependency ranges remain in `requirements-dev.txt`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
PATH="$PWD/.venv/bin:$PATH" ./scripts/build-native.sh
./scripts/check.sh
```

The build fetches only pinned wire-interface definitions and their upstream license. Generated code lives under ignored dependency/build paths. It compiles the submitted collector/decoder source, not copied production binaries. No host-specific `-march=native` is required, and no fast-math option is used.

## Start a local API control plane

```sh
mkdir -p .build/runtime
TICKING_DB="$PWD/.build/runtime/accounts.sqlite3" \
TICKING_ORIGINS=http://localhost:8080 \
TICKING_MARKETS_ENABLED=false \
TICKING_CRYPTO_ENABLED=false \
TICKING_CREDITS_ENABLED=false \
PYTHONPATH=api .venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8081
```

This starts account/API control-plane code without production accounts or credentials. Data reads require a configured private engine bridge and retained stores. The production acquisition scheduler and historical downloaders are excluded; this command is not a complete hosted-service deployment.

## Retained data bridge

The selected bridge serves retained token/pool and venue data over authenticated loopback HTTP. Create a local capability file outside this repository and pass its path in `TICKING_BRIDGE_SECRET_FILE`. Set `TICKING_DATA_ROOT` to your isolated store root, then run `PYTHONPATH=engine .venv/bin/python engine/ticking_bridge.py` from the repository root. Configure the API with the same capability file and `TICKING_ENGINE_BRIDGE=http://127.0.0.1:19643`. Never expose this operator bridge directly to browsers.

An empty store returns no markets; it does not bootstrap historical data. New managed watches remain queued until verified market/account evidence is supplied by the operator. The private discovery and external-history federation jobs are not included.

## Native inputs

`.build/bin/metadata-recorder` receives an executed-result stream configured by an operator-owned credential file. Read the argument validation in its source; never commit that file. Its upstream is an executing node, so the collector does not manufacture execution results from raw transaction packets.

`.build/bin/venue-collector discover catalog.json 20` discovers an explicitly bounded set of live instruments; `live catalog.json path/to/history.sqlite3` starts live candle/trade/book intake. This public variant deliberately excludes bulk and recent REST candle acquisition. Replay with `fixture catalog.json isolated.sqlite3 inputs.jsonl` is available for offline validation. The original normalized candle schema and reader remain compatible.

Native observation/branch-join utilities are included for review. Their presence is not proof of live packet-supply integration. Do not enable multiple collectors against the same mutable output directory.

## Terminal

The minimal example serves only terminal source and GET-only public API requests. It binds loopback. Its isolated shell replaces the private website/navigation; it is not the marketing frontend. The chart starts empty until a data source responds. No production dataset, artwork bundle, synthetic price generator or private web dependency is copied.
