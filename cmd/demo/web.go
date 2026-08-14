package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Polign/polign_demo/internal/corpus"
	"github.com/Polign/polign_demo/internal/embedder"
	"github.com/Polign/polign_demo/internal/polign"
)

//go:embed ui/index.html
var uiHTML []byte

// searchModes are the three read paths the demo exposes. Each is one query
// against the node; the difference is only which legs the request asks for.
const (
	modeSemantic = "semantic" // vector only
	modeKeyword  = "keyword"  // BM25 only
	modeHybrid   = "hybrid"   // both, fused server-side
)

type server struct {
	client     *polign.Client
	model      *embedder.Model
	collection string
	defaultK   int
	nprobe     int
	rescore    int
	public     bool
	examples   []string
	manifest   *corpus.Manifest

	limiter *demoLimiter
	once    sync.Once
}

func (s *server) mux() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(uiHTML)
	})
	mux.HandleFunc("GET /demo/meta", s.handleMeta)
	mux.HandleFunc("GET /demo/search", s.handleSearch)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	return mux
}

func (s *server) handleMeta(w http.ResponseWriter, _ *http.Request) {
	meta := map[string]any{
		"modes":    []string{modeSemantic, modeKeyword, modeHybrid},
		"examples": s.examples,
		"public":   s.public,
	}
	if s.manifest != nil {
		meta["corpus"] = s.manifest.Passages
		meta["articles"] = s.manifest.Articles
		meta["dataset"] = s.manifest.Dataset
		meta["model"] = s.manifest.Model
	}
	writeJSON(w, meta)
}

// searchResult is one hit as the browser sees it.
type searchResult struct {
	ID    string  `json:"id"`
	Score float32 `json:"score"`
	Title string  `json:"title"`
	URL   string  `json:"url"`
	Text  string  `json:"text"`
}

func (s *server) handleSearch(w http.ResponseWriter, r *http.Request) {
	if s.public {
		s.once.Do(func() { s.limiter = newDemoLimiter() })
		if !s.limiter.allow(clientIP(r)) {
			http.Error(w, "rate limit exceeded — this is a shared demo, try again in a moment", http.StatusTooManyRequests)
			return
		}
	}

	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if q == "" {
		http.Error(w, "missing ?q= query text", http.StatusBadRequest)
		return
	}
	if len(q) > 300 {
		http.Error(w, "query too long (max 300 bytes)", http.StatusBadRequest)
		return
	}
	mode := r.URL.Query().Get("mode")
	if mode == "" {
		mode = modeHybrid
	}
	if mode != modeSemantic && mode != modeKeyword && mode != modeHybrid {
		http.Error(w, "mode must be semantic, keyword, or hybrid", http.StatusBadRequest)
		return
	}
	k := s.defaultK
	if raw := r.URL.Query().Get("k"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > 20 {
			http.Error(w, "k must be 1..20", http.StatusBadRequest)
			return
		}
		k = n
	}
	// The read-path knobs are operator settings, not public inputs: exposing
	// nprobe to the internet is an invitation to make every query as expensive
	// as possible.
	nprobe := s.nprobe
	if !s.public {
		if raw := r.URL.Query().Get("nprobe"); raw != "" {
			n, err := strconv.Atoi(raw)
			if err != nil || n < 0 || n > 4096 {
				http.Error(w, "nprobe must be 0..4096", http.StatusBadRequest)
				return
			}
			nprobe = n
		}
	}

	hits, took, err := s.search(r.Context(), q, mode, k, nprobe)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}

	resp := struct {
		Query     string         `json:"query"`
		Mode      string         `json:"mode"`
		ScoreKind string         `json:"score_kind"`
		TookMS    float64        `json:"took_ms"`
		Corpus    int            `json:"corpus"`
		Results   []searchResult `json:"results"`
	}{
		Query: q, Mode: mode, ScoreKind: scoreKind(mode),
		TookMS: float64(took.Microseconds()) / 1000,
	}
	if s.manifest != nil {
		resp.Corpus = s.manifest.Passages
	}
	for _, h := range hits {
		resp.Results = append(resp.Results, searchResult{
			ID:    h.ID,
			Score: displayScore(mode, h),
			Title: h.Metadata["title"],
			URL:   h.Metadata["url"],
			Text:  h.Metadata["text"],
		})
	}
	writeJSON(w, resp)
}

// search runs one query in the given mode. Every query is cold: the node holds
// no corpus in memory, so this is a read straight out of object storage.
func (s *server) search(ctx context.Context, q, mode string, k, nprobe int) ([]polign.Hit, time.Duration, error) {
	opts := polign.QueryOptions{K: k, Cold: true, NProbe: nprobe, Rescore: s.rescore}
	if mode != modeKeyword {
		vec := s.model.Embed(q)
		// A query of entirely unknown words embeds to the zero vector, which
		// ranks against nothing. On the hybrid path the text leg still works,
		// so drop the vector leg rather than failing the request.
		if !isZero(vec) {
			opts.Values = vec
		}
	}
	if mode != modeSemantic {
		opts.Text = q
	}
	if opts.Values == nil && opts.Text == "" {
		return nil, 0, errNoKnownWords
	}
	start := time.Now()
	hits, err := s.client.Query(ctx, s.collection, opts)
	return hits, time.Since(start), err
}

type simpleErr string

func (e simpleErr) Error() string { return string(e) }

const errNoKnownWords = simpleErr("no known words in that query — try different wording")

// displayScore maps a hit to the number shown for it: cosine similarity on the
// semantic path (converted from the collection's squared-L2 distance, exact
// for the unit vectors this embedder produces), BM25 on the keyword path, and
// the fused RRF score on the hybrid path.
func displayScore(mode string, h polign.Hit) float32 {
	if mode == modeSemantic {
		return polign.Cosine(h.Distance)
	}
	return h.Score
}

func scoreKind(mode string) string {
	switch mode {
	case modeKeyword:
		return "bm25"
	case modeHybrid:
		return "rrf"
	default:
		return "cosine"
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

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

// demoLimiter guards the public endpoint: a global cap so one node stays
// healthy under a burst, plus a per-client cap so nobody monopolises it. It is
// a plain token bucket — the demo has no dependencies beyond the standard
// library, and this is the only rate limiting it needs.
type demoLimiter struct {
	mu     sync.Mutex
	global *bucket
	perIP  map[string]*ipEntry
}

type ipEntry struct {
	b    *bucket
	seen time.Time
}

const (
	globalRate, globalBurst = 100, 200 // requests/second across all clients
	perIPRate, perIPBurst   = 5, 20    // requests/second per client
	maxTrackedIPs           = 8192
	ipIdleTTL               = 10 * time.Minute
)

func newDemoLimiter() *demoLimiter {
	return &demoLimiter{
		global: newBucket(globalRate, globalBurst),
		perIP:  make(map[string]*ipEntry),
	}
}

func (l *demoLimiter) allow(ip string) bool {
	now := time.Now()
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.global.allow(now) {
		return false
	}
	e, ok := l.perIP[ip]
	if !ok {
		if len(l.perIP) >= maxTrackedIPs {
			cutoff := now.Add(-ipIdleTTL)
			for k, v := range l.perIP {
				if v.seen.Before(cutoff) {
					delete(l.perIP, k)
				}
			}
		}
		e = &ipEntry{b: newBucket(perIPRate, perIPBurst)}
		l.perIP[ip] = e
	}
	e.seen = now
	return e.b.allow(now)
}

// bucket is a token bucket refilling at rate tokens/second up to burst. It is
// not safe for concurrent use; demoLimiter holds the lock.
type bucket struct {
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
}

func newBucket(rate, burst float64) *bucket {
	return &bucket{rate: rate, burst: burst, tokens: burst}
}

func (b *bucket) allow(now time.Time) bool {
	if b.last.IsZero() {
		b.last = now
	}
	b.tokens += now.Sub(b.last).Seconds() * b.rate
	if b.tokens > b.burst {
		b.tokens = b.burst
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// clientIP prefers the leftmost X-Forwarded-For hop (the demo is expected to
// sit behind a TLS-terminating reverse proxy) and falls back to the peer.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		first, _, _ := strings.Cut(xff, ",")
		return strings.TrimSpace(first)
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
