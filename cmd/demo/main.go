// Command demo serves the public Wikipedia search demo: a browser UI and a
// read-only text-query endpoint in front of a polign_db node.
//
//	demo -dir ~/polign-demo-data -node http://127.0.0.1:23000
//
// It holds no corpus and no index. A search is: embed the query (via the
// sidecar), send one cold query to the node, render what comes back. The node
// serves it from object storage. That split is the point of this repo — the app
// is stateless and small, and the corpus can grow to millions of passages
// without changing what the app costs to run.
//
// -public is the hosted configuration: UI plus the read-only /demo endpoints
// only, rate-limited per client, with no path through to the node's write API.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Polign/polign_demo/internal/corpus"
	"github.com/Polign/polign_demo/internal/embedder"
	"github.com/Polign/polign_demo/internal/polign"
)

func main() {
	addr := flag.String("http", ":24000", "listen address for the UI and /demo endpoints")
	node := flag.String("node", "http://127.0.0.1:23000", "polign_db node HTTP address")
	dir := flag.String("dir", "", "directory holding corpus.json and examples.txt (required)")
	collection := flag.String("collection", "wikipedia", "collection to search")
	embedAddr := flag.String("embed-addr", "", "embedding sidecar address, e.g. http://127.0.0.1:23200 (required). It must run the same model that embedded the collection — vectors from two models are points in different spaces")
	embedDim := flag.Int("embed-dim", 384, "vector width the sidecar returns, checked against every reply")
	notice := flag.String("notice", "", "banner text shown above the results, e.g. to say the index is still loading. A collection is searchable from its first published generation, so this is how a partial index says so rather than letting a miss look like an absence")
	k := flag.Int("k", 8, "results per query")
	nprobe := flag.Int("nprobe", 0, "IVF cells probed per cold query (0 = the node's default): higher is better recall, more IO")
	rescore := flag.Int("rescore", 0, "exact-rescore pool on a compressed collection (0 = node default, <0 = ADC-only)")
	public := flag.Bool("public", false, "hosted mode: rate-limited, no /v1 passthrough, no query knobs from the browser")
	modes := flag.String("modes", "semantic,keyword,hybrid", "search paths to serve, comma-separated: semantic, keyword, hybrid. The first is the default for a request that names no mode")
	timeout := flag.Duration("timeout", 20*time.Second, "per-query timeout against the node")
	flag.Parse()

	if *dir == "" {
		log.Fatal("-dir is required (the directory holding corpus.json)")
	}
	if *embedAddr == "" {
		log.Fatal("-embed-addr is required (the embedding sidecar; see serve/embedserve.py)")
	}

	// The query embedder must be the model that embedded the collection, so the
	// app proves it is reachable before serving rather than failing on a
	// visitor's first search.
	model := embedder.NewRemote(*embedAddr, *embedDim, *timeout)
	probeCtx, cancelProbe := context.WithTimeout(context.Background(), 30*time.Second)
	if err := model.Health(probeCtx); err != nil {
		cancelProbe()
		log.Fatalf("%v\n(is serve/embedserve.py running? see deploy/README.md)", err)
	}
	cancelProbe()

	// corpus.json is written by prepare.py and is descriptive only — the app
	// reports it in the UI. A missing manifest is not fatal: the demo still
	// searches, it just cannot say how large the corpus is.
	var manifest *corpus.Manifest
	if m, merr := corpus.LoadManifest(*dir); merr == nil {
		manifest = m
	} else {
		log.Printf("no corpus.json in %s (%v) — the UI will not show a corpus size", *dir, merr)
	}

	enabled, err := parseModes(*modes)
	if err != nil {
		log.Fatal(err)
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
		dir: *dir, notice: *notice,
		modes: enabled,
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
	log.Printf("demo on http://localhost%s — %s, searching %q (%s) on %s, embedding via %s (%d dims)",
		*addr, mode, *collection, size, *node, *embedAddr, *embedDim)

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

// parseModes validates the -modes list, preserving the caller's order (which
// is the order the UI shows them in).
func parseModes(spec string) ([]string, error) {
	valid := map[string]bool{modeSemantic: true, modeKeyword: true, modeHybrid: true}
	var out []string
	seen := map[string]bool{}
	for _, m := range strings.Split(spec, ",") {
		m = strings.TrimSpace(m)
		if m == "" {
			continue
		}
		if !valid[m] {
			return nil, fmt.Errorf("-modes: unknown mode %q (want semantic, keyword, or hybrid)", m)
		}
		if !seen[m] {
			seen[m] = true
			out = append(out, m)
		}
	}
	if len(out) == 0 {
		return nil, errors.New("-modes must name at least one of semantic, keyword, hybrid")
	}
	return out, nil
}
