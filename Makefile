# Builds for this repo. The polign-server / polign-maintain binaries come from
# the polign_db repo and must be built with the "cloud" tag for s3:// support:
#
#   go build -tags cloud -o polign-server ./cmd/server
#
GO ?= go
BIN ?= bin

# The serving and build hosts are arm64 Linux (Graviton).
LINUX_ENV = CGO_ENABLED=0 GOOS=linux GOARCH=arm64

.PHONY: all build linux test vet clean

all: build

build:
	$(GO) build -o $(BIN)/ ./cmd/...

# Cross-compiled binaries for the EC2 hosts; see deploy/README.md.
linux:
	$(LINUX_ENV) $(GO) build -o $(BIN)/linux-arm64/ ./cmd/...

test:
	$(GO) test ./...

vet:
	$(GO) vet ./...

clean:
	rm -rf $(BIN)
