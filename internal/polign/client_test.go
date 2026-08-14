package polign

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestQueryRequestShape pins the wire contract the demo relies on: the node
// must receive cold/nprobe/text exactly as set, and omit the knobs left at
// zero rather than sending them as explicit defaults.
func TestQueryRequestShape(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/collections/wikipedia/query" {
			t.Errorf("path = %q, want /v1/collections/wikipedia/query", r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		_, _ = w.Write([]byte(`{"hits":[{"id":"a","distance":0.5,"metadata":{"title":"T"}}]}`))
	}))
	defer srv.Close()

	hits, err := New(srv.URL, 5*time.Second).Query(context.Background(), "wikipedia", QueryOptions{
		Values: []float32{1, 0}, Text: "volcano", K: 3, Cold: true, NProbe: 24,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got["cold"] != true {
		t.Errorf("cold = %v, want true", got["cold"])
	}
	if got["nprobe"] != float64(24) {
		t.Errorf("nprobe = %v, want 24", got["nprobe"])
	}
	if got["text"] != "volcano" {
		t.Errorf("text = %v, want volcano", got["text"])
	}
	// Rescore was left at 0: the node's own default must win, so the field
	// should not appear at all.
	if _, ok := got["rescore"]; ok {
		t.Errorf("rescore present (%v); zero means 'node default' and must be omitted", got["rescore"])
	}
	if len(hits) != 1 || hits[0].ID != "a" || hits[0].Metadata["title"] != "T" {
		t.Errorf("hits = %+v", hits)
	}
}

// TestQueryTextOnlyOmitsValues checks that a pure keyword search does not send
// an empty values array, which the node would reject as a malformed vector.
func TestQueryTextOnlyOmitsValues(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		_, _ = w.Write([]byte(`{"hits":[]}`))
	}))
	defer srv.Close()

	if _, err := New(srv.URL, 5*time.Second).Query(context.Background(), "c", QueryOptions{
		Text: "bees", K: 5, Cold: true,
	}); err != nil {
		t.Fatal(err)
	}
	if _, ok := got["values"]; ok {
		t.Errorf("values present on a text-only query: %v", got["values"])
	}
}

// TestErrorCarriesNodeMessage: the node's message is the actionable part of a
// failure (missing segment store, dim mismatch), so it must survive.
func TestErrorCarriesNodeMessage(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"invalid argument: cold search requires -segment-stores"}`))
	}))
	defer srv.Close()

	_, err := New(srv.URL, 5*time.Second).Query(context.Background(), "c", QueryOptions{K: 1, Cold: true})
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "segment-stores") {
		t.Errorf("error lost the node's message: %v", err)
	}
	if !strings.Contains(err.Error(), "400") {
		t.Errorf("error lost the status: %v", err)
	}
}

func TestPutBatch(t *testing.T) {
	var got putBatchRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/vectors:batch") {
			t.Errorf("path = %q", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"ids":["x","y"]}`))
	}))
	defer srv.Close()

	ids, err := New(srv.URL, 5*time.Second).PutBatch(context.Background(), "wikipedia", []Vector{
		{ID: "x", Values: []float32{1, 2}, Metadata: map[string]string{"title": "X"}},
		{ID: "y", Values: []float32{3, 4}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 2 {
		t.Fatalf("ids = %v", ids)
	}
	if len(got.Vectors) != 2 || got.Vectors[0].Metadata["title"] != "X" {
		t.Errorf("request vectors = %+v", got.Vectors)
	}
}

// TestCosine pins the conversion the UI's "cosine" score depends on. The
// collection stores unit vectors under squared L2, where cos = 1 - d/2.
func TestCosine(t *testing.T) {
	cases := []struct {
		d, want float32
	}{
		{0, 1},  // identical
		{2, 0},  // orthogonal
		{4, -1}, // opposite
		{0.5, 0.75},
	}
	for _, c := range cases {
		if got := Cosine(c.d); got != c.want {
			t.Errorf("Cosine(%v) = %v, want %v", c.d, got, c.want)
		}
	}
}
