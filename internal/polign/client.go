// Package polign is a minimal client for a running polign_db node's HTTP/JSON
// API. It deliberately depends on nothing outside the standard library: the
// demo talks to the database exactly the way any other service would — over
// the documented /v1 wire contract, with no access to polign_db's internal
// packages.
//
// Only the three calls the demo needs are implemented: batch upsert (the
// loader), query (the search app), and health.
package polign

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client is a polign_db HTTP/JSON API client. The zero value is not usable;
// call New.
type Client struct {
	base string
	http *http.Client
}

// New returns a client for a node's HTTP address, e.g.
// "http://127.0.0.1:23000". A trailing slash is tolerated.
func New(base string, timeout time.Duration) *Client {
	return &Client{
		base: strings.TrimSuffix(base, "/"),
		http: &http.Client{Timeout: timeout},
	}
}

// Vector is one record to upsert.
type Vector struct {
	ID       string            `json:"id"`
	Values   []float32         `json:"values"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

// Hit is one search result.
type Hit struct {
	ID string `json:"id"`
	// Distance is the collection metric's distance, smaller is better. The
	// demo's collections use squared L2 over L2-normalized vectors, where
	// cosine similarity is 1 - Distance/2 (see Cosine).
	Distance float32 `json:"distance"`
	// Score is the BM25 (text) or fused (hybrid) relevance, larger is better.
	// It is absent on pure vector queries.
	Score    float32           `json:"score"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

// Cosine converts a squared-L2 distance between unit vectors into cosine
// similarity. The identity is exact: for ||a|| = ||b|| = 1,
// ||a-b||² = 2 - 2·cos(a,b).
func Cosine(squaredL2 float32) float32 { return 1 - squaredL2/2 }

// QueryOptions are the read-path knobs the demo exercises. They map one-to-one
// onto the node's query API.
type QueryOptions struct {
	// Values is the query embedding. Empty runs a pure text (BM25) search.
	Values []float32
	// Text runs a BM25 lexical search over the segment index. With Values set
	// it makes the query hybrid, fused server-side.
	Text string
	// K is the number of hits to return.
	K int
	// Cold serves the query from object-storage segments rather than the
	// in-memory index. The demo's serving node holds no corpus in memory, so
	// this is always true there; leaving it false on such a node returns
	// nothing.
	Cold bool
	// NProbe overrides the IVF probe count for cold queries (0 = the node's
	// default). Higher means more cells read: better recall, more IO.
	NProbe int
	// Rescore tunes the exact-rescore stage on a compressed (IVF-PQ)
	// collection: 0 = node default, > 0 = candidate pool size, < 0 = ADC-only
	// ranking with no exact rescore. Ignored by collections that rank exactly.
	Rescore int
}

type queryRequest struct {
	Values  []float32 `json:"values,omitempty"`
	K       int       `json:"k"`
	Cold    bool      `json:"cold,omitempty"`
	NProbe  int       `json:"nprobe,omitempty"`
	Text    string    `json:"text,omitempty"`
	Rescore int       `json:"rescore,omitempty"`
}

type queryResponse struct {
	Hits []Hit `json:"hits"`
}

// Query runs one search and returns its hits.
func (c *Client) Query(ctx context.Context, collection string, opts QueryOptions) ([]Hit, error) {
	body := queryRequest{
		Values:  opts.Values,
		K:       opts.K,
		Cold:    opts.Cold,
		NProbe:  opts.NProbe,
		Text:    opts.Text,
		Rescore: opts.Rescore,
	}
	var resp queryResponse
	path := "/v1/collections/" + url.PathEscape(collection) + "/query"
	if err := c.post(ctx, path, body, &resp); err != nil {
		return nil, err
	}
	return resp.Hits, nil
}

type putBatchRequest struct {
	Vectors []Vector `json:"vectors"`
}

type putBatchResponse struct {
	IDs []string `json:"ids"`
}

// PutBatch upserts vectors in one request and returns the stored ids.
func (c *Client) PutBatch(ctx context.Context, collection string, vecs []Vector) ([]string, error) {
	var resp putBatchResponse
	path := "/v1/collections/" + url.PathEscape(collection) + "/vectors:batch"
	if err := c.post(ctx, path, putBatchRequest{Vectors: vecs}, &resp); err != nil {
		return nil, err
	}
	return resp.IDs, nil
}

// Health reports whether the node answers /healthz.
func (c *Client) Health(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/healthz", nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("polign: %s unreachable: %w", c.base, err)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("polign: %s health: %s", c.base, resp.Status)
	}
	return nil
}

// post sends a JSON body and decodes a JSON response, turning a non-2xx into
// an error carrying the node's message.
func (c *Client) post(ctx context.Context, path string, body, out any) error {
	blob, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+path, bytes.NewReader(blob))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("polign: POST %s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		// Error bodies are small; include the node's own message, which is
		// usually the actionable part (missing segment store, bad dim, ...).
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		return fmt.Errorf("polign: POST %s: %s: %s", path, resp.Status, bytes.TrimSpace(msg))
	}
	if out == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
