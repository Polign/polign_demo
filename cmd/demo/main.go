// Command demo serves the public Wikipedia search demo: a browser UI and a
// read-only text-query endpoint in front of a polign_db node.
//
//	demo -dir ~/polign-demo-data -node http://127.0.0.1:23000
//
// It holds no corpus. Its only state is the static embedding model it uses to
// turn a query string into a vector (~30 MB); every search is one cold query
// against the node, which serves it from object storage. That split is the
// point of this repo: the app is stateless and small, and the corpus can be
// millions of passages without changing what the app costs to run.
//
// -public is the hosted configuration: UI plus the read-only /demo endpoints
// only, rate-limited per client, with no path through to the node's write API.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Polign/polign_demo/internal/corpus"
	"github.com/Polign/polign_demo/internal/embedder"
	"github.com/Polign/polign_demo/internal/polign"
)

func main() {
	addr := flag.String("http", ":24000", "listen address for the UI and /demo endpoints")
	node := flag.String("node", "http://127.0.0.1:23000", "polign_db node HTTP address")
	dir := flag.String("dir", "", "dataset directory holding the embedding model and corpus.json (required)")
	collection := flag.String("collection", "wikipedia", "collection to search")
	k := flag.Int("k", 8, "results per query")
	nprobe := flag.Int("nprobe", 0, "IVF cells probed per cold query (0 = the node's default): higher is better recall, more IO")
	rescore := flag.Int("rescore", 0, "exact-rescore pool on a compressed collection (0 = node default, <0 = ADC-only)")
	public := flag.Bool("public", false, "hosted mode: rate-limited, no /v1 passthrough, no query knobs from the browser")
	timeout := flag.Duration("timeout", 20*time.Second, "per-query timeout against the node")
	flag.Parse()

	if *dir == "" {
		log.Fatal("-dir is required (the directory holding model.json, vocab.txt, embeddings.f32)")
	}
	model, err := embedder.Load(*dir)
	if err != nil {
		log.Fatal(err)
	}

	// corpus.json is written by prepare.py and is descriptive only — the app
	// reports it in the UI. A missing manifest is not fatal: the demo still
	// searches, it just cannot say how large the corpus is.
	var manifest *corpus.Manifest
	if m, merr := corpus.LoadManifest(*dir); merr == nil {
		manifest = m
	} else {
		log.Printf("no corpus.json in %s (%v) — the UI will not show a corpus size", *dir, merr)
	}

	client := polign.New(*node, *timeout)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := client.Health(ctx); err != nil {
		log.Fatalf("%v\n(is the node running? see deploy/README.md)", err)
	}

	srv := &server{
		client: client, model: model, collection: *collection,
		defaultK: *k, nprobe: *nprobe, rescore: *rescore,
		public: *public, examples: corpus.LoadExamples(*dir), manifest: manifest,
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.mux(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	mode := "open"
	if *public {
		mode = "public (rate-limited)"
	}
	size := "unknown size"
	if manifest != nil {
		size = fmt.Sprintf("%d passages", manifest.Passages)
	}
	log.Printf("demo on http://localhost%s — %s, searching %q (%s) on %s",
		*addr, mode, *collection, size, *node)

	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("http: %v", err)
			stop()
		}
	}()

	<-ctx.Done()
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutCtx); err != nil {
		log.Printf("shutdown: %v", err)
		os.Exit(1)
	}
}
