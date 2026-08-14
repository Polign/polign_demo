// Command load embeds a prepared Wikipedia corpus and hands it to polign_db,
// by one of two routes.
//
//	load -dir ~/polign-demo-data -out shard.jsonl      # bulk: for polign-import
//	load -dir ~/polign-demo-data -addr http://...      # online: through a node
//
// It embeds each passage in-process with the same static model the search app
// uses for queries (internal/embedder), so corpus and query vectors can never
// disagree on tokenization, and upserts them in batches. At full English
// Wikipedia scale this is a multi-hour run over millions of records, so it is
// built to be interrupted: progress is checkpointed per shard and -resume
// picks up where it stopped. Upserts are idempotent, so replaying part of a
// shard is harmless.
//
// -out is the right route for an initial corpus: it writes the JSONL that
// polign-import turns into segments directly in object storage, skipping the
// write log and the node's unpersisted-write buffer entirely.
//
// -addr is the online route, through a running node's HTTP API. That path is
// built for live traffic — every write is logged and buffered until the
// persistor covers it — so a node under a bulk load will push back once its
// buffer fills (-overlay-max-buffered). This command treats that as flow
// control and paces itself rather than failing.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/Polign/polign_demo/internal/corpus"
	"github.com/Polign/polign_demo/internal/embedder"
	"github.com/Polign/polign_demo/internal/polign"
)

func main() {
	dir := flag.String("dir", "", "prepared dataset directory: model files, passage shards, corpus.json (required)")
	addr := flag.String("addr", "http://127.0.0.1:23000", "polign_db node HTTP address")
	collection := flag.String("collection", "wikipedia", "collection to load into")
	batch := flag.Int("batch", 512, "passages per upsert request")
	workers := flag.Int("workers", 0, "concurrent upsert workers (0 = NumCPU)")
	limit := flag.Int("limit", 0, "stop after this many passages (0 = the whole corpus); useful for smoke tests")
	maxShards := flag.Int("shards", 0, "stop after this many shards this run (0 = all of them)")
	resume := flag.Bool("resume", true, "skip shards already completed according to the checkpoint file")
	timeout := flag.Duration("timeout", 2*time.Minute, "per-request timeout")
	out := flag.String("out", "", `instead of writing to a node, embed to this JSONL file ("-" for stdout) for polign-import`)
	flag.Parse()

	if *dir == "" {
		log.Fatal("-dir is required (the directory prepare/prepare.py wrote)")
	}
	if *workers <= 0 {
		*workers = runtime.NumCPU()
	}

	model, err := embedder.Load(*dir)
	if err != nil {
		log.Fatal(err)
	}
	shards, err := corpus.Shards(*dir)
	if err != nil {
		log.Fatal(err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Two destinations, one embedding path. -out writes the JSONL that
	// polign-import reads, which builds the segment index directly in object
	// storage: no node, no write log, and none of the buffering that an online
	// write path has to do. Use it for an initial corpus; use the node for
	// incremental writes to a live collection.
	var sink recordSink
	if *out != "" {
		s, cerr := newJSONLSink(*out)
		if cerr != nil {
			log.Fatal(cerr)
		}
		defer s.Close()
		sink = s
	} else {
		client := polign.New(*addr, *timeout)
		if err := client.Health(ctx); err != nil {
			log.Fatalf("%v\n(start the node first: see deploy/README.md)", err)
		}
		sink = &nodeSink{client: client, collection: *collection}
	}

	cp := loadCheckpoint(*dir, *collection)
	// Descriptive only — corpus.json is written by prepare.py and lets progress
	// be reported against the whole corpus rather than just this run.
	corpusTotal := 0
	if m, merr := corpus.LoadManifest(*dir); merr == nil {
		corpusTotal = m.Passages
	}
	l := &loader{
		sink: sink, model: model, collection: *collection,
		batch: *batch, workers: *workers, limit: *limit, outPath: *out,
	}

	dest := *addr
	if *out != "" {
		dest = *out + " (JSONL for polign-import)"
	}
	log.Printf("loading %d shard(s) into %q at %s — dim %d, %d workers, batches of %d",
		len(shards), *collection, dest, model.Dim(), *workers, *batch)
	start := time.Now()
	loaded := 0
	for i, shard := range shards {
		name := filepath.Base(shard)
		if *resume && cp.done(name) {
			log.Printf("[%d/%d] %s — already loaded, skipping", i+1, len(shards), name)
			continue
		}
		// -shards bounds one run, not the corpus. A node accumulates every
		// write in its in-memory index as well as its write log, so a load
		// this large is done in chunks with a node restart between them; the
		// checkpoint makes each run pick up exactly where the last stopped.
		if *maxShards > 0 && loaded >= *maxShards {
			log.Printf("-shards %d reached for this run — %d shard(s) still to load",
				*maxShards, len(shards)-i)
			break
		}
		n, err := l.loadShard(ctx, shard)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				log.Printf("interrupted after %d passages in %s — rerun with -resume to continue",
					n, name)
				os.Exit(1)
			}
			log.Fatalf("%s: %v", name, err)
		}
		cp.markDone(name, n)
		loaded++
		if err := cp.save(); err != nil {
			log.Printf("checkpoint: %v (the load continues; a rerun may redo this shard)", err)
		}
		// Two different numbers, kept apart on purpose: this run's rate is what
		// tells you whether the load is healthy, while the corpus total is what
		// tells you how far along it is. Dividing the total by this run's
		// elapsed time would flatter a resumed run enormously.
		throttled := ""
		if t := l.throttled.Load(); t > 0 {
			// Not a warning: it means the loader is correctly pacing itself to
			// the persistor instead of piling unindexed writes into node memory.
			throttled = fmt.Sprintf(", %d batches paced by backpressure", t)
		}
		log.Printf("[%d/%d] %s — %d passages at %.0f/s this run (%d of %d loaded%s)",
			i+1, len(shards), name, n,
			float64(l.sent.Load())/time.Since(start).Seconds(), cp.Total, corpusTotal, throttled)
		if l.reachedLimit() {
			log.Printf("-limit %d reached", *limit)
			break
		}
	}
	log.Printf("done: %d passages this run in %s; %d of %d loaded overall",
		l.sent.Load(), time.Since(start).Round(time.Second), cp.Total, corpusTotal)
	if l.outPath != "" {
		log.Printf("next: polign-import -stores <spec> -collection %s -expected-size %d %s",
			l.collection, corpusTotal, l.outPath)
	}
}

// loader embeds and upserts one shard at a time. Embedding is CPU-bound and
// upserting is network-bound, so both run on the same worker pool: each worker
// takes a batch of passages, embeds them, and sends them.
type loader struct {
	sink       recordSink
	model      *embedder.Model
	collection string
	batch      int
	workers    int
	limit      int

	outPath   string       // non-empty when emitting JSONL for polign-import
	sent      atomic.Int64 // passages successfully upserted across all shards
	throttled atomic.Int64 // batches the node pushed back on (see putWithBackpressure)
}

func (l *loader) reachedLimit() bool {
	return l.limit > 0 && l.sent.Load() >= int64(l.limit)
}

// loadShard streams one shard through the worker pool and returns how many
// passages it upserted.
func (l *loader) loadShard(ctx context.Context, path string) (int, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	batches := make(chan []corpus.Passage, l.workers)
	var wg sync.WaitGroup
	var firstErr error
	var errOnce sync.Once
	var shardSent atomic.Int64

	for range l.workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for b := range batches {
				if err := l.send(ctx, b); err != nil {
					errOnce.Do(func() { firstErr = err; cancel() })
					return
				}
				shardSent.Add(int64(len(b)))
				l.sent.Add(int64(len(b)))
			}
		}()
	}

	// The reader owns backpressure: it stops at -limit, at cancellation, or at
	// the first worker error.
	readErr := func() error {
		pending := make([]corpus.Passage, 0, l.batch)
		flush := func() error {
			if len(pending) == 0 {
				return nil
			}
			select {
			case batches <- pending:
				pending = make([]corpus.Passage, 0, l.batch)
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		err := corpus.ScanShard(path, func(p *corpus.Passage) error {
			if l.reachedLimit() {
				return errLimit
			}
			pending = append(pending, *p)
			if len(pending) < l.batch {
				return nil
			}
			return flush()
		})
		if err != nil && !errors.Is(err, errLimit) {
			return err
		}
		return flush()
	}()

	close(batches)
	wg.Wait()

	if firstErr != nil {
		return int(shardSent.Load()), firstErr
	}
	if readErr != nil && !errors.Is(readErr, errLimit) {
		return int(shardSent.Load()), readErr
	}
	return int(shardSent.Load()), ctx.Err()
}

// errLimit unwinds the shard scanner when -limit is reached; it is never
// reported as a failure.
var errLimit = errors.New("limit reached")

// send embeds one batch and upserts it. Passages whose text has no known
// tokens would embed to the zero vector, which is meaningless to rank against;
// they are dropped rather than stored.
func (l *loader) send(ctx context.Context, ps []corpus.Passage) error {
	vecs := make([]polign.Vector, 0, len(ps))
	for i := range ps {
		p := &ps[i]
		v := l.model.Embed(p.Text)
		if isZero(v) {
			continue
		}
		vecs = append(vecs, polign.Vector{
			ID:     p.ID,
			Values: v,
			Metadata: map[string]string{
				"title": p.Title,
				"url":   p.URL,
				"text":  p.Text,
			},
		})
	}
	if len(vecs) == 0 {
		return nil
	}
	return l.putWithBackpressure(ctx, vecs)
}

// putWithBackpressure upserts a batch, waiting out the node's backpressure
// rather than failing on it.
//
// A node that serves cold-first buffers writes until the persistor covers them
// with a segment, and refuses new ones once that buffer hits its cap
// (-overlay-max-buffered). That refusal is not an error in any meaningful
// sense: it is the node telling a loader that writes faster than the persistor
// indexes — which this one does, by roughly 4x — to slow to the rate the
// pipeline can actually absorb. Nothing was written, so the retry is safe, and
// waiting is the correct response rather than something to report.
func (l *loader) putWithBackpressure(ctx context.Context, vecs []polign.Vector) error {
	for attempt := 0; ; attempt++ {
		err := l.sink.Put(ctx, vecs)
		var bp *polign.BackpressureError
		if !errors.As(err, &bp) {
			return err
		}
		l.throttled.Add(1)
		wait := bp.RetryAfter
		// Back off gently on a persistently full buffer so a stalled persistor
		// does not turn into a tight retry loop.
		if attempt > 3 {
			wait *= 2
		}
		if wait > 30*time.Second {
			wait = 30 * time.Second
		}
		select {
		case <-time.After(wait):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func isZero(v []float32) bool {
	for _, x := range v {
		if x != 0 {
			return false
		}
	}
	return true
}

// checkpoint records which shards finished, so an interrupted multi-hour load
// resumes at shard granularity. It lives next to the dataset and is keyed by
// collection, so loading the same corpus into two collections does not confuse
// the two.
type checkpoint struct {
	path  string
	Done  map[string]int `json:"done"`  // shard file name -> passages loaded
	Total int            `json:"total"` // passages loaded across completed shards
}

func checkpointPath(dir, collection string) string {
	return filepath.Join(dir, fmt.Sprintf(".load-checkpoint-%s.json", collection))
}

func loadCheckpoint(dir, collection string) *checkpoint {
	cp := &checkpoint{path: checkpointPath(dir, collection), Done: map[string]int{}}
	raw, err := os.ReadFile(cp.path)
	if err != nil {
		return cp
	}
	if err := json.Unmarshal(raw, cp); err != nil {
		log.Printf("checkpoint %s is unreadable (%v) — starting from the first shard", cp.path, err)
		cp.Done, cp.Total = map[string]int{}, 0
	}
	if cp.Done == nil {
		cp.Done = map[string]int{}
	}
	return cp
}

func (c *checkpoint) done(shard string) bool { _, ok := c.Done[shard]; return ok }

func (c *checkpoint) markDone(shard string, n int) {
	c.Done[shard] = n
	c.Total += n
}

func (c *checkpoint) save() error {
	blob, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := c.path + ".tmp"
	if err := os.WriteFile(tmp, blob, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, c.path)
}
