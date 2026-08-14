package corpus

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// TestShardsFollowsManifestOrder: load order comes from corpus.json, not from
// the filesystem, so a dataset whose shards do not sort into load order still
// loads correctly.
func TestShardsFollowsManifestOrder(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "corpus.json", `{"passages":3,"shards":["passages-00001.jsonl","passages-00000.jsonl"]}`)
	write(t, dir, "passages-00000.jsonl", "")
	write(t, dir, "passages-00001.jsonl", "")

	got, err := Shards(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 ||
		filepath.Base(got[0]) != "passages-00001.jsonl" ||
		filepath.Base(got[1]) != "passages-00000.jsonl" {
		t.Errorf("shards = %v, want manifest order", got)
	}
}

// TestShardsFallsBackToGlob covers a hand-assembled directory with no manifest.
func TestShardsFallsBackToGlob(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "passages-00002.jsonl", "")
	write(t, dir, "passages-00000.jsonl", "")
	write(t, dir, "passages-00001.jsonl", "")

	got, err := Shards(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || filepath.Base(got[0]) != "passages-00000.jsonl" {
		t.Errorf("shards = %v, want sorted order", got)
	}
}

// TestShardsSingleFile is the smoke-test slice shape: one passages.jsonl.
func TestShardsSingleFile(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "passages.jsonl", "")
	got, err := Shards(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || filepath.Base(got[0]) != "passages.jsonl" {
		t.Errorf("shards = %v", got)
	}
}

func TestShardsMissing(t *testing.T) {
	_, err := Shards(t.TempDir())
	if err == nil {
		t.Fatal("expected an error for a directory with no shards")
	}
	if !strings.Contains(err.Error(), "prepare.py") {
		t.Errorf("error should point at prepare.py: %v", err)
	}
}

func TestScanShard(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "passages.jsonl", strings.Join([]string{
		`{"id":"wiki-1-0","title":"Bee","url":"https://en.wikipedia.org/wiki/Bee","text":"Bees are insects."}`,
		``, // blank lines are skipped rather than treated as corrupt
		`{"id":"wiki-2-0","title":"Ant","url":"u","text":"Ants too."}`,
	}, "\n"))

	var ids, titles []string
	if err := ScanShard(filepath.Join(dir, "passages.jsonl"), func(p *Passage) error {
		ids = append(ids, p.ID)
		titles = append(titles, p.Title)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(ids) != 2 || ids[0] != "wiki-1-0" || ids[1] != "wiki-2-0" {
		t.Errorf("ids = %v", ids)
	}
	if titles[0] != "Bee" {
		t.Errorf("titles = %v", titles)
	}
}

// TestScanShardReportsBadLine: a corrupt corpus should fail loudly, naming the
// line, rather than silently loading a short corpus.
func TestScanShardReportsBadLine(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "passages.jsonl", "{\"id\":\"ok\"}\nnot json\n")
	err := ScanShard(filepath.Join(dir, "passages.jsonl"), func(*Passage) error { return nil })
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "line 2") {
		t.Errorf("error should name the line: %v", err)
	}
}

func TestLoadManifest(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "corpus.json",
		`{"dataset":"wikimedia/wikipedia 20231101.en","articles":5355565,"passages":12519135,
		  "shards":["passages-00000.jsonl"],"model":"minishlab/potion-base-8M","dim":256}`)

	m, err := LoadManifest(dir)
	if err != nil {
		t.Fatal(err)
	}
	if m.Passages != 12519135 || m.Articles != 5355565 || m.Dim != 256 {
		t.Errorf("manifest = %+v", m)
	}
}

func TestLoadExamples(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "examples.txt", "why do volcanoes erupt\n\n  how do bees make honey  \n")
	got := LoadExamples(dir)
	if len(got) != 2 || got[0] != "why do volcanoes erupt" || got[1] != "how do bees make honey" {
		t.Errorf("examples = %q", got)
	}
	if LoadExamples(t.TempDir()) != nil {
		t.Error("a dataset with no examples.txt should yield nil, not an error")
	}
}
