package embedder

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Remote is a client for serve/embedserve.py, the sentence-transformer that
// turns query text into a vector.
//
// Embedding runs in its own process because the model is ONNX and this app is
// Go. What matters is not where it runs but that it is the *same* model that
// embedded the passages: a collection can only be searched by the model that
// wrote it, because a vector from any other model is a point in a different
// space and its nearest neighbours mean nothing. That pairing is why the app
// refuses to start when the sidecar is unreachable, and why Embed checks the
// width of every reply.
//
// The sidecar owns the asymmetric query prefix the model expects, so callers
// pass raw query text and this type stays a transport.
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
