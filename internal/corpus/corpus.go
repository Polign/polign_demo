// Package corpus reads the dataset prepare/prepare.py produces: the passage
// shards the loader streams into the database, and the small manifest the
// search app reads to describe the corpus it is serving.
//
// The passages themselves are never held in memory as a whole — at full
// English Wikipedia scale the corpus is several gigabytes of JSONL, so
// everything here streams.
package corpus

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Passage is one corpus document, one JSON object per line in a shard.
type Passage struct {
	ID    string `json:"id"`
	Title string `json:"title"`
	URL   string `json:"url"`
	Text  string `json:"text"`
}

// Manifest describes a prepared dataset. prepare.py writes it as
// corpus.json alongside the shards; the loader rewrites Loaded as it goes and
// the demo app reads it to report what is being searched.
type Manifest struct {
	Dataset  string   `json:"dataset"`  // e.g. "wikimedia/wikipedia 20231101.en"
	Articles int      `json:"articles"` // source articles that contributed passages
	Passages int      `json:"passages"` // total passages across every shard
	Shards   []string `json:"shards"`   // shard file names, in load order
	Model    string   `json:"model"`    // embedding model id
	Dim      int      `json:"dim"`      // embedding dimensionality
}

// LoadManifest reads corpus.json from dir.
func LoadManifest(dir string) (*Manifest, error) {
	raw, err := os.ReadFile(filepath.Join(dir, "corpus.json"))
	if err != nil {
		return nil, fmt.Errorf("corpus: %w", err)
	}
	var m Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("corpus: parse corpus.json: %w", err)
	}
	return &m, nil
}

// Shards returns the passage shard paths in dir, in load order. It prefers the
// order recorded in corpus.json and falls back to sorted passages-*.jsonl, so
// a hand-assembled directory still works.
func Shards(dir string) ([]string, error) {
	if m, err := LoadManifest(dir); err == nil && len(m.Shards) > 0 {
		out := make([]string, len(m.Shards))
		for i, name := range m.Shards {
			out[i] = filepath.Join(dir, name)
		}
		return out, nil
	}
	found, err := filepath.Glob(filepath.Join(dir, "passages-*.jsonl"))
	if err != nil {
		return nil, err
	}
	if len(found) == 0 {
		// A single-file dataset (what the small smoke-test slice produces).
		one := filepath.Join(dir, "passages.jsonl")
		if _, serr := os.Stat(one); serr == nil {
			return []string{one}, nil
		}
		return nil, fmt.Errorf("corpus: no passage shards in %s (run prepare/prepare.py)", dir)
	}
	sort.Strings(found)
	return found, nil
}

// maxLine bounds one JSONL record. Passages are capped at ~1200 characters by
// prepare.py; 1 MiB is generous enough that a legitimate line never trips it
// and a corrupt file fails loudly instead of consuming memory.
const maxLine = 1 << 20

// ScanShard calls fn for every passage in one shard file, in order. fn must
// not retain p beyond the call: the underlying buffer is reused.
func ScanShard(path string, fn func(p *Passage) error) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("corpus: %w", err)
	}
	defer f.Close()
	return scan(f, path, fn)
}

func scan(r io.Reader, name string, fn func(p *Passage) error) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64<<10), maxLine)
	var p Passage
	line := 0
	for sc.Scan() {
		line++
		if len(strings.TrimSpace(sc.Text())) == 0 {
			continue
		}
		p = Passage{}
		if err := json.Unmarshal(sc.Bytes(), &p); err != nil {
			return fmt.Errorf("corpus: %s line %d: %w", name, line, err)
		}
		if err := fn(&p); err != nil {
			return err
		}
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("corpus: read %s: %w", name, err)
	}
	return nil
}

// LoadExamples returns the curated example queries shown in the UI, or nil if
// the dataset carries none (they are a nicety, not a requirement).
func LoadExamples(dir string) []string {
	raw, err := os.ReadFile(filepath.Join(dir, "examples.txt"))
	if err != nil {
		return nil
	}
	var out []string
	for _, line := range strings.Split(string(raw), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			out = append(out, line)
		}
	}
	return out
}
