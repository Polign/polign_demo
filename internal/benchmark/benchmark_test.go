package benchmark

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestAnswerBoundariesAndValidation(t *testing.T) {
	if AnswerMatch("a stone wall", []string{"one"}) {
		t.Fatal("substring false positive")
	}
	if !AnswerMatch("Paris, France.", []string{"paris france"}) {
		t.Fatal("token matching")
	}
	hits := []Hit{{ID: "1", Distance: 1, Metadata: map[string]string{"title": "Paris", "text": "Paris is in France", "url": "https://en.wikipedia.org/wiki/Paris"}}}
	rec, err := Validate(hits, Query{Answers: []string{"France"}}, "semantic", 1, 1)
	if err != nil || rec.Rank != 1 {
		t.Fatalf("valid answer: %+v %v", rec, err)
	}
	if _, err = Validate(append(hits, hits[0]), Query{}, "semantic", 2, 2); err == nil {
		t.Fatal("duplicate accepted")
	}
}
func TestScheduledLoadAccountsForOverloadWithoutMutations(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/collections/wikipedia_bge/query" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["cold"] != true {
			t.Error("not cold")
		}
		time.Sleep(40 * time.Millisecond)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"hits":[{"id":"1","distance":1,"metadata":{"title":"Paris","text":"Paris, France","url":"https://en.wikipedia.org/wiki/Paris"}}]}`))
	}))
	defer srv.Close()
	dir := t.TempDir()
	q := Query{ID: "q", Text: "where is Paris", Answers: []string{"France"}, Values: make([]float32, 384)}
	q.Values[0] = 1
	blob, _ := json.Marshal(q)
	file := filepath.Join(dir, "queries.jsonl")
	os.WriteFile(file, blob, 0600)
	c := Config{Endpoint: srv.URL, Collection: "wikipedia_bge", Queries: file, Output: filepath.Join(dir, "run"), Mode: "semantic", Distribution: "sequential", Concurrency: 1, Requests: 20, Timeout: time.Second, Rate: 1000, K: 1, NProbe: 8}
	result, err := Run(context.Background(), c)
	if err != nil {
		t.Fatal(err)
	}
	if result.Scheduled != 20 || result.Success+result.Errors != 20 || result.Attempted+result.Dropped != 20 || result.Dropped == 0 {
		t.Fatalf("lost accounting: %+v", result)
	}
	if result.PeakInflight != 1 {
		t.Fatal("exceeded concurrency")
	}
	c.Endpoint = srv.URL + "/v1/collections/x/vectors:batch"
	if _, err = Run(context.Background(), c); err == nil || !strings.Contains(err.Error(), "origin") {
		t.Fatal("unsafe endpoint accepted")
	}
}
func TestGlobalPercentiles(t *testing.T) {
	p := Percentiles([]float64{2, 100, 1, 3})
	if p["p50"] != 2 || p["p99"] != 100 {
		t.Fatalf("percentiles: %v", p)
	}
}

func TestArticleRankUsesFirstOccurrenceOfMatchingArticle(t *testing.T) {
	hits := []Hit{
		{ID: "a1", Distance: 1, Metadata: map[string]string{"title": "A", "text": "other passage", "url": "https://example.com/a"}},
		{ID: "b1", Distance: 2, Metadata: map[string]string{"title": "B", "text": "unrelated passage", "url": "https://example.com/b"}},
		{ID: "a2", Distance: 3, Metadata: map[string]string{"title": "A", "text": "the answer is Paris", "url": "https://example.com/a"}},
	}
	rec, err := Validate(hits, Query{Answers: []string{"Paris"}}, "semantic", 3, 3)
	if err != nil || rec.Rank != 3 || rec.ArticleRank != 1 {
		t.Fatalf("article grouping: %+v %v", rec, err)
	}
}

// TestValidateMetadataCap: hits past the requested cap may arrive without
// metadata and are still valid, only unscored; a hit inside the cap without
// metadata is still an error.
func TestValidateMetadataCap(t *testing.T) {
	hits := []Hit{
		{ID: "a", Distance: 0.1, Metadata: map[string]string{"title": "Paris", "text": "Paris is the capital of France", "url": "u"}},
		{ID: "b", Distance: 0.2},
		{ID: "c", Distance: 0.3},
	}
	rec, err := Validate(hits, Query{Answers: []string{"Paris"}}, "semantic", 3, 1)
	if err != nil {
		t.Fatalf("capped response rejected: %v", err)
	}
	if rec.Rank != 1 || len(rec.IDs) != 3 {
		t.Fatalf("rank %d, %d ids; want rank 1 and 3 ids", rec.Rank, len(rec.IDs))
	}
	if _, err := Validate(hits, Query{}, "semantic", 3, 2); err == nil {
		t.Fatal("hit 2 lacks metadata inside the cap; want an error")
	}
	if (Config{K: 10}).hydrated() != 10 || (Config{K: 10, MetadataK: 3}).hydrated() != 3 || (Config{K: 10, MetadataK: -1}).hydrated() != 0 {
		t.Fatal("hydrated() does not follow the 0 / n / -1 convention")
	}
}
