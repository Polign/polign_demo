package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/Polign/polign_demo/internal/benchmark"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	var c benchmark.Config
	flag.StringVar(&c.Endpoint, "endpoint", "", "Database HTTP origin")
	flag.StringVar(&c.Collection, "collection", "wikipedia_bge", "Existing collection")
	flag.StringVar(&c.Queries, "queries", "", "Precomputed query JSONL")
	flag.StringVar(&c.Output, "out", "", "New result directory")
	flag.StringVar(&c.Mode, "mode", "semantic", "semantic, keyword, hybrid")
	flag.StringVar(&c.Distribution, "distribution", "uniform", "uniform, zipf, sequential")
	flag.IntVar(&c.Concurrency, "concurrency", 1, "Maximum in-flight requests")
	flag.IntVar(&c.Requests, "requests", 0, "Maximum requests, zero means duration only")
	flag.DurationVar(&c.Duration, "duration", 0, "Measured duration")
	flag.DurationVar(&c.Timeout, "timeout", 10*time.Second, "Per-request deadline")
	flag.Float64Var(&c.Rate, "rate", 0, "Scheduled arrivals/sec, zero means closed loop")
	flag.Int64Var(&c.Seed, "seed", 20260906, "Replay seed")
	flag.IntVar(&c.K, "k", 10, "Number of hits")
	flag.IntVar(&c.NProbe, "nprobe", 8, "Cold search probes")
	flag.IntVar(&c.MetadataK, "metadata-k", 0, "Hits that carry metadata: 0 = all, N = first N, -1 = none")
	flag.IntVar(&c.Rescore, "rescore", 0, "Versioned default rescore policy")
	flag.Parse()
	if c.Output == "" {
		fmt.Fprintln(os.Stderr, "-out is required")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	result, err := benchmark.Run(ctx, c)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
