package embedder

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Embedder turns a query string into a vector. The demo has two
// implementations: the in-process static Model, and Remote, which calls the
// bge-small sidecar over loopback. Both are query-side only -- the corpus is
// embedded offline by the build host -- but they must agree with whatever
// embedded the collection being searched, so the two are never mixed within one
// deployment.
type Embedder interface {
	Embed(ctx context.Context, text string) ([]float32, error)
}

// Static adapts the in-process Model to Embedder. Its Embed cannot fail and
// ignores the context: it is pure arithmetic over a resident matrix.
type Static struct{ M *Model }

func (s Static) Embed(_ context.Context, text string) ([]float32, error) {
	return s.M.Embed(text), nil
}

// Remote is a client for prepare/embedserve.py -- bge-small-en-v1.5 running as
// a loopback HTTP sidecar. The demo uses it for the v2 collection, whose
// passages were embedded with the same model: a real 12-layer encoder rather
// than a static token-vector average, which is what lets a query like "capital
// of france" rank Paris over every other French city.
//
// The sidecar owns the query prefix bge requires, so callers pass raw query
// text and this type stays a transport.
type Remote struct {
	url    string
	client *http.Client
	dim    int
}

// NewRemote returns a client for a sidecar at addr (e.g. "http://127.0.0.1:23200").
// dim is the vector width the collection was built at; a reply of any other
// width is rejected rather than passed to the node, which would otherwise fail
// the query with a confusing dimension mismatch from deep inside the engine.
func NewRemote(addr string, dim int, timeout time.Duration) *Remote {
	return &Remote{
		url:    addr + "/embed",
		client: &http.Client{Timeout: timeout},
		dim:    dim,
	}
}

func (r *Remote) Embed(ctx context.Context, text string) ([]float32, error) {
	body, err := json.Marshal(map[string]string{"text": text})
	if err != nil {
		return nil, fmt.Errorf("embedder: encode request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("embedder: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("embedder: call sidecar: %w", err)
	}
	defer resp.Body.Close()

	var out struct {
		Values []float32 `json:"values"`
		Error  string    `json:"error"`
	}
	if derr := json.NewDecoder(resp.Body).Decode(&out); derr != nil {
		return nil, fmt.Errorf("embedder: decode reply (status %d): %w", resp.StatusCode, derr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("embedder: sidecar status %d: %s", resp.StatusCode, out.Error)
	}
	if len(out.Values) != r.dim {
		return nil, fmt.Errorf("embedder: sidecar returned dim %d, collection is %d", len(out.Values), r.dim)
	}
	return out.Values, nil
}

// Health reports whether the sidecar is up. main calls it at boot so a
// misconfigured deployment fails immediately instead of on the first query.
func (r *Remote) Health(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, r.url[:len(r.url)-len("/embed")]+"/healthz", nil)
	if err != nil {
		return err
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return fmt.Errorf("embedder: sidecar unreachable: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("embedder: sidecar health status %d", resp.StatusCode)
	}
	return nil
}
