# Requires Go >= 1.24 on PATH.
VERSION ?= 0.1.0
LDFLAGS := -s -w
OUT ?= bin

.PHONY: all linux-amd64 native test clean

all: linux-amd64

linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/linux-amd64/em-extract ./cmd/em-extract
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/linux-amd64/em-trace ./cmd/em-trace
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/linux-amd64/em-rpc-extract ./cmd/em-rpc-extract

# Build on the target VM with cgo for libsecp256k1 sender recovery (needs gcc).
native:
	CGO_ENABLED=1 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/native/em-extract ./cmd/em-extract
	CGO_ENABLED=1 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/native/em-trace ./cmd/em-trace
	CGO_ENABLED=1 go build -trimpath -ldflags '$(LDFLAGS)' -o $(OUT)/native/em-rpc-extract ./cmd/em-rpc-extract

test:
	CGO_ENABLED=0 go test ./...

clean:
	rm -rf $(OUT)
