package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"

	"github.com/Polign/polign_demo/internal/polign"
)

// recordSink is where an embedded batch goes: a running node, or a JSONL file
// for polign-import to turn into segments.
type recordSink interface {
	Put(ctx context.Context, vecs []polign.Vector) error
}

// nodeSink writes through a node's HTTP API, honoring its backpressure.
type nodeSink struct {
	client     *polign.Client
	collection string
}

func (s *nodeSink) Put(ctx context.Context, vecs []polign.Vector) error {
	_, err := s.client.PutBatch(ctx, s.collection, vecs)
	return err
}

// jsonlSink writes {"id","values","metadata"} lines — the format polign-import
// reads. Vectors dominate the output (a 512-dim vector is a few KB of JSON),
// so this trades disk for skipping the online write path entirely.
type jsonlSink struct {
	mu   sync.Mutex
	w    *bufio.Writer
	f    *os.File // nil when writing to stdout
	enc  *json.Encoder
	path string
}

func newJSONLSink(path string) (*jsonlSink, error) {
	s := &jsonlSink{path: path}
	if path == "-" {
		s.w = bufio.NewWriterSize(os.Stdout, 1<<20)
	} else {
		f, err := os.Create(path)
		if err != nil {
			return nil, fmt.Errorf("open -out %s: %w", path, err)
		}
		s.f = f
		s.w = bufio.NewWriterSize(f, 4<<20)
	}
	s.enc = json.NewEncoder(s.w)
	return s, nil
}

// importRecord mirrors polign-import's jsonl schema.
type importRecord struct {
	ID       string            `json:"id"`
	Values   []float32         `json:"values"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

func (s *jsonlSink) Put(_ context.Context, vecs []polign.Vector) error {
	// Workers embed in parallel; serialize only the write.
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, v := range vecs {
		if err := s.enc.Encode(importRecord{ID: v.ID, Values: v.Values, Metadata: v.Metadata}); err != nil {
			return fmt.Errorf("write %s: %w", s.path, err)
		}
	}
	return nil
}

func (s *jsonlSink) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.w.Flush(); err != nil {
		return err
	}
	if s.f == nil {
		return nil
	}
	return s.f.Close()
}
