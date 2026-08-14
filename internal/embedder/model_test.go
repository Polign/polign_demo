package embedder

import (
	"bufio"
	"encoding/json"
	"math"
	"os"
	"strconv"
	"testing"
)

// golden mirrors testdata/golden.json, produced by prepare/prepare.py
// straight from the reference model2vec implementation: token ids from the
// tokenizers library, vectors from StaticModel.encode, and the embedding rows
// the golden texts touch (so the pooling math is checked without shipping the
// full matrix; the complete vocab.txt is committed because greedy
// longest-match is only faithful against the full vocabulary).
type golden struct {
	Source       string               `json:"source"`
	Texts        []string             `json:"texts"`
	TokenIDs     [][]int              `json:"token_ids"`
	Vectors      [][]float32          `json:"vectors"`
	TokenVectors map[string][]float32 `json:"token_vectors"`
}

func loadGolden(t *testing.T) (*Model, golden) {
	t.Helper()
	raw, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if uerr := json.Unmarshal(raw, &g); uerr != nil {
		t.Fatal(uerr)
	}

	vf, err := os.Open("testdata/vocab.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer vf.Close()
	vocab := make(map[string]int)
	sc := bufio.NewScanner(vf)
	for sc.Scan() {
		vocab[sc.Text()] = len(vocab)
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}

	dim := len(g.Vectors[0])
	m := &Model{
		dim:          dim,
		vocab:        vocab,
		vecs:         make([]float32, len(vocab)*dim),
		unkID:        vocab["[UNK]"],
		maxWordChars: 100,
		normalize:    true,
	}
	for id, row := range g.TokenVectors {
		i, err := strconv.Atoi(id)
		if err != nil {
			t.Fatal(err)
		}
		copy(m.vecs[i*dim:], row)
	}
	return m, g
}

// TestTokenizeGolden pins the Go tokenizer (BERT normalize + pre-tokenize +
// WordPiece) to the reference tokenizers-library output.
func TestTokenizeGolden(t *testing.T) {
	m, g := loadGolden(t)
	for i, text := range g.Texts {
		got := m.tokenize(text)
		want := g.TokenIDs[i]
		if len(got) != len(want) {
			t.Errorf("%q: got %d tokens %v, want %d %v", text, len(got), got, len(want), want)
			continue
		}
		for j := range got {
			if got[j] != want[j] {
				t.Errorf("%q: token %d = %d, want %d (got %v want %v)", text, j, got[j], want[j], got, want)
				break
			}
		}
	}
}

// TestEmbedGolden pins Embed (drop-UNK mean pooling + L2 normalization) to
// reference StaticModel.encode vectors.
func TestEmbedGolden(t *testing.T) {
	m, g := loadGolden(t)
	const tol = 1e-5
	for i, text := range g.Texts {
		got := m.Embed(text)
		want := g.Vectors[i]
		if len(got) != len(want) {
			t.Fatalf("%q: dim %d, want %d", text, len(got), len(want))
		}
		for j := range got {
			if d := math.Abs(float64(got[j] - want[j])); d > tol {
				t.Errorf("%q: dim %d differs by %.2e (got %g, want %g)", text, j, d, got[j], want[j])
				break
			}
		}
	}
}

func TestEmbedEmpty(t *testing.T) {
	m, _ := loadGolden(t)
	for _, text := range []string{"", "   ", "\x00"} {
		v := m.Embed(text)
		if len(v) != m.dim {
			t.Fatalf("Embed(%q): dim %d, want %d", text, len(v), m.dim)
		}
		for _, x := range v {
			if x != 0 {
				t.Fatalf("Embed(%q) = non-zero vector", text)
			}
		}
	}
}
