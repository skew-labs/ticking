.PHONY: all build test test-fork lint fmt clean \
        contracts-build contracts-test contracts-lint \
        aggregator-build aggregator-test aggregator-lint py-lint

PY_DIRS := data charting aggregator/harness aggregator/service

all: lint build test

build: contracts-build aggregator-build
test: contracts-test aggregator-test
lint: contracts-lint aggregator-lint py-lint

contracts/lib/forge-std:
	git clone --depth 1 https://github.com/foundry-rs/forge-std contracts/lib/forge-std

contracts-build: contracts/lib/forge-std
	cd contracts && forge build

contracts-test: contracts/lib/forge-std
	cd contracts && forge test --no-match-path 'test/Fork*'

test-fork: contracts/lib/forge-std
	cd contracts && forge test --match-path 'test/Fork*'

contracts-lint:
	cd contracts && forge fmt --check

aggregator-build:
	cd aggregator && cargo build --release --locked

aggregator-test:
	cd aggregator && cargo test --release --locked

aggregator-lint:
	cd aggregator && cargo fmt --check

py-lint:
	ruff check $(PY_DIRS)

fmt:
	cd contracts && forge fmt
	cd aggregator && cargo fmt

clean:
	rm -rf contracts/out contracts/cache aggregator/target

agents-check:
	python3 -m compileall -q \
		agents/track agents/muse agents/bridge agents/mcp agents/context agents/realtime \
		search/investigate search/overlay search/search-guard \
		research/refresh-py research/labels research/investigate research/mapping research/traders \
		research/ask_report.py
