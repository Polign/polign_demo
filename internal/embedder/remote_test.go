package embedder

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

// TestRemoteEmbedRequestShape pins the sidecar contract: the query text goes
// out raw. The bge query prefix belongs to the sidecar, so a client that added
// one here would double it.
func TestRemoteEmbedRequestShape(t *testing.T) {
	var got map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embed" {
			t.Errorf("path = %q, want /embed", r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		_, _ = w.Write([]byte(`{"values":[0.1,0.2,0.3]}`))
	}))
	defer srv.Close()

	vec, err := NewRemote(srv.URL, 3, 5*time.Second).Embed(context.Background(), "who invented the telephone")
	if err != nil {
		t.Fatal(err)
	}
	if got["text"] != "who invented the telephone" {
		t.Errorf("text = %q, want the raw query", got["text"])
	}
	if len(vec) != 3 || vec[0] != 0.1 {
		t.Errorf("values = %v, want the sidecar's vector", vec)
	}
}

// TestRemoteEmbedRejectsWrongDim guards the model/collection pairing: a
// collection can only be queried by the model that embedded it. A reply of the
// wrong width means the sidecar is serving a different model, and failing here
// beats passing it to the node, which would report a confusing dimension
// mismatch from deep inside the engine.
func TestRemoteEmbedRejectsWrongDim(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"values":[0.1,0.2]}`))
	}))
	defer srv.Close()

	_, err := NewRemote(srv.URL, 384, 5*time.Second).Embed(context.Background(), "q")
	if err == nil {
		t.Fatal("want an error for a 2-dim reply against a 384-dim collection")
	}
	if !strings.Contains(err.Error(), "384") {
		t.Errorf("error %q should name the expected dimension", err)
	}
}

// TestRemoteEmbedSurfacesSidecarError keeps a sidecar failure legible: the
// demo turns this into a 502 with the reason, rather than an empty result set
// that looks like "nothing matched".
func TestRemoteEmbedSurfacesSidecarError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"model not loaded"}`))
	}))
	defer srv.Close()

	_, err := NewRemote(srv.URL, 3, 5*time.Second).Embed(context.Background(), "q")
	if err == nil {
		t.Fatal("want an error for a 500 from the sidecar")
	}
	if !strings.Contains(err.Error(), "model not loaded") {
		t.Errorf("error %q should carry the sidecar's reason", err)
	}
}

// TestRemoteHealth is what main calls at boot: a misconfigured deployment must
// fail immediately rather than on the first visitor's query.
func TestRemoteHealth(t *testing.T) {
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	r := NewRemote(srv.URL, 384, 5*time.Second)
	if err := r.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	if path != "/healthz" {
		t.Errorf("probed %q, want /healthz", path)
	}

	srv.Close()
	if err := r.Health(context.Background()); err == nil {
		t.Error("want an error when the sidecar is down")
	}
}
