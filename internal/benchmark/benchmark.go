// Package benchmark drives only the existing collection's HTTP query endpoint.
package benchmark

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"
)

type Query struct {
	ID      string    `json:"id"`
	Text    string    `json:"query"`
	Values  []float32 `json:"values"`
	Answers []string  `json:"answers,omitempty"`
	Titles  []string  `json:"expected_titles,omitempty"`
	Split   string    `json:"split"`
}
type Config struct {
	Endpoint     string
	Collection   string
	Queries      string
	Output       string
	Mode         string
	Distribution string
	Concurrency  int
	Requests     int
	Duration     time.Duration
	Timeout      time.Duration
	Rate         float64
	Seed         int64
	K            int
	NProbe       int
	// MetadataK caps how many hits carry metadata (0 = all, as the node
	// defaults; -1 = none). Sent only when set, so older nodes ignore it.
	MetadataK int
	Rescore   int
}
type Hit struct {
	ID       string            `json:"id"`
	Distance float64           `json:"distance"`
	Score    float64           `json:"score"`
	Metadata map[string]string `json:"metadata"`
}
type Record struct {
	Sequence          int64     `json:"sequence"`
	QueryID           string    `json:"query_id"`
	Scheduled         string    `json:"scheduled_at"`
	Sent              string    `json:"sent_at,omitempty"`
	Completed         string    `json:"completed_at,omitempty"`
	LatencyMS         float64   `json:"latency_ms"`
	ScheduleLatencyMS float64   `json:"schedule_latency_ms"`
	DispatchMS        float64   `json:"dispatch_ms"`
	ValidationMS      float64   `json:"validation_ms"`
	Status            int       `json:"status"`
	Error             string    `json:"error,omitempty"`
	Valid             bool      `json:"valid"`
	Dropped           bool      `json:"dropped"`
	Bytes             int       `json:"bytes"`
	Rank              int       `json:"answer_rank"`
	ArticleRank       int       `json:"article_answer_rank"`
	IDs               []string  `json:"hit_ids,omitempty"`
	Titles            []string  `json:"hit_titles,omitempty"`
	Distances         []float64 `json:"hit_distances,omitempty"`
}
type Summary struct {
	Config           Config             `json:"config"`
	QuerySHA256      string             `json:"query_sha256"`
	Started          string             `json:"started_at"`
	Elapsed          float64            `json:"elapsed_seconds"`
	Scheduled        int                `json:"scheduled"`
	Attempted        int                `json:"attempted"`
	Success          int                `json:"success"`
	Errors           int                `json:"errors"`
	Dropped          int                `json:"dropped"`
	QPS              float64            `json:"successful_qps"`
	Goodput250       float64            `json:"goodput_250ms_qps"`
	Goodput1000      float64            `json:"goodput_1000ms_qps"`
	PeakInflight     int64              `json:"peak_inflight"`
	SuccessLatency   map[string]float64 `json:"success_latency_ms"`
	AllLatency       map[string]float64 `json:"all_attempt_latency_ms"`
	ScheduledLatency map[string]float64 `json:"schedule_latency_ms"`
	DispatchLatency  map[string]float64 `json:"dispatch_delay_ms"`
	HitAt            map[string]float64 `json:"hit_at"`
	MRR              float64            `json:"mrr_at_k"`
	Statuses         map[int]int        `json:"statuses"`
}
type prepared struct {
	query Query
	body  []byte
}
type job struct {
	index     int
	seq       int64
	scheduled time.Time
}

func LoadQueries(path string) ([]Query, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, "", err
	}
	h := sha256.Sum256(raw)
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 65536), 2<<20)
	var qs []Query
	ids := map[string]bool{}
	for scanner.Scan() {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		var q Query
		if err := json.Unmarshal(scanner.Bytes(), &q); err != nil {
			return nil, "", err
		}
		if q.ID == "" || q.Text == "" || ids[q.ID] {
			return nil, "", errors.New("missing/duplicate query id or text")
		}
		if len(q.Values) != 384 {
			return nil, "", fmt.Errorf("query %s has dimension %d, require 384", q.ID, len(q.Values))
		}
		norm := 0.0
		for _, v := range q.Values {
			norm += float64(v) * float64(v)
		}
		if math.IsNaN(norm) || math.IsInf(norm, 0) || math.Abs(norm-1) > 0.005 {
			return nil, "", fmt.Errorf("query %s is not normalized", q.ID)
		}
		ids[q.ID] = true
		qs = append(qs, q)
	}
	if err := scanner.Err(); err != nil {
		return nil, "", err
	}
	if len(qs) == 0 {
		return nil, "", errors.New("empty queries")
	}
	return qs, hex.EncodeToString(h[:]), nil
}

func Tokens(s string) []string {
	return strings.FieldsFunc(strings.ToLower(s), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) })
}
func AnswerMatch(text string, answers []string) bool {
	padded := " " + strings.Join(Tokens(text), " ") + " "
	for _, a := range answers {
		tok := Tokens(a)
		if len(tok) > 0 && strings.Contains(padded, " "+strings.Join(tok, " ")+" ") {
			return true
		}
	}
	return false
}

// Validate checks one response and scores it. hydrated is how many leading
// hits must carry passage metadata (k unless the request capped it); hits
// past it are valid without metadata and simply cannot match an answer.
// hydrated is how many hits the request asked to carry metadata.
func (c Config) hydrated() int {
	switch {
	case c.MetadataK < 0:
		return 0
	case c.MetadataK > 0 && c.MetadataK < c.K:
		return c.MetadataK
	}
	return c.K
}

func Validate(hits []Hit, q Query, mode string, k, hydrated int) (Record, error) {
	rec := Record{}
	if len(hits) != k {
		return rec, fmt.Errorf("expected %d hits, received %d", k, len(hits))
	}
	ids := map[string]bool{}
	articles := map[string]int{}
	articleRank := 0
	for i, h := range hits {
		if h.ID == "" || ids[h.ID] {
			return rec, errors.New("empty or duplicate hit ID")
		}
		ids[h.ID] = true
		if math.IsNaN(h.Distance) || math.IsInf(h.Distance, 0) || math.IsNaN(h.Score) || math.IsInf(h.Score, 0) {
			return rec, errors.New("nonfinite score")
		}
		if i < hydrated && (h.Metadata["title"] == "" || h.Metadata["text"] == "" || h.Metadata["url"] == "") {
			return rec, errors.New("missing passage metadata")
		}
		if i > 0 && mode == "semantic" && h.Distance+1e-5 < hits[i-1].Distance {
			return rec, errors.New("distance ordering")
		}
		if i > 0 && mode != "semantic" && h.Score > hits[i-1].Score+1e-5 {
			return rec, errors.New("score ordering")
		}
		title := h.Metadata["title"]
		match := false
		if i >= hydrated {
			// Unhydrated by request: keep the hit, skip the answer check.
		} else if len(q.Titles) > 0 {
			for _, t := range q.Titles {
				if title == t {
					match = true
				}
			}
		} else {
			match = AnswerMatch(h.Metadata["text"], q.Answers)
		}
		if title != "" && articles[title] == 0 {
			articleRank++
			articles[title] = articleRank
		}
		if match && rec.Rank == 0 {
			rec.Rank = i + 1
			rec.ArticleRank = articles[title]
		}
		rec.IDs = append(rec.IDs, h.ID)
		rec.Titles = append(rec.Titles, title)
		rec.Distances = append(rec.Distances, h.Distance)
	}
	return rec, nil
}
func Percentiles(values []float64) map[string]float64 {
	result := map[string]float64{"p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0}
	if len(values) == 0 {
		return result
	}
	sort.Float64s(values)
	for name, p := range map[string]float64{"p50": .5, "p90": .9, "p95": .95, "p99": .99, "max": 1} {
		result[name] = values[int(math.Ceil(p*float64(len(values))))-1]
	}
	return result
}

func Run(ctx context.Context, c Config) (Summary, error) {
	result := Summary{Config: c, HitAt: map[string]float64{}, Statuses: map[int]int{}}
	if c.Concurrency < 1 || c.K < 1 || c.Timeout <= 0 || c.Rate < 0 || c.Requests < 0 || (c.Duration <= 0 && c.Requests == 0) {
		return result, errors.New("invalid workload limits")
	}
	if c.Mode != "semantic" && c.Mode != "keyword" && c.Mode != "hybrid" {
		return result, errors.New("invalid query mode")
	}
	if c.Distribution != "uniform" && c.Distribution != "zipf" && c.Distribution != "sequential" {
		return result, errors.New("invalid distribution")
	}
	endpoint, err := url.Parse(c.Endpoint)
	if err != nil {
		return result, err
	}
	if (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" || (endpoint.Path != "" && endpoint.Path != "/") {
		return result, errors.New("endpoint must be a credential-free HTTP origin")
	}
	if c.Collection == "" || strings.ContainsAny(c.Collection, "/\\?#") {
		return result, errors.New("invalid collection")
	}
	endpoint.Path = "/v1/collections/" + c.Collection + "/query"
	qs, hash, err := LoadQueries(c.Queries)
	if err != nil {
		return result, err
	}
	result.QuerySHA256 = hash
	ps := make([]prepared, len(qs))
	for i, q := range qs {
		body := map[string]any{"k": c.K, "cold": true, "nprobe": c.NProbe, "rescore": c.Rescore}
		if c.MetadataK != 0 {
			body["metadata_k"] = c.MetadataK
		}
		if c.Mode != "keyword" {
			body["values"] = q.Values
		}
		if c.Mode != "semantic" {
			body["text"] = q.Text
		}
		ps[i] = prepared{query: q}
		ps[i].body, err = json.Marshal(body)
		if err != nil {
			return result, err
		}
	}
	if err := os.MkdirAll(c.Output, 0755); err != nil {
		return result, err
	}
	f, err := os.OpenFile(filepath.Join(c.Output, "requests.jsonl.gz"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return result, err
	}
	gz, _ := gzip.NewWriterLevel(f, gzip.BestSpeed)
	encoder := json.NewEncoder(gz)
	records := make(chan Record, 2*c.Concurrency+64)
	logDone := make(chan error, 1)
	var all, successful, scheduled, dispatch []float64
	var good250, good1000 int
	go func() {
		var writeErr error
		for rec := range records {
			if writeErr == nil {
				writeErr = encoder.Encode(rec)
			}
			result.Scheduled++
			if rec.Dropped {
				result.Dropped++
				result.Errors++
				continue
			}
			result.Attempted++
			result.Statuses[rec.Status]++
			all = append(all, rec.LatencyMS)
			scheduled = append(scheduled, rec.ScheduleLatencyMS)
			dispatch = append(dispatch, rec.DispatchMS)
			if rec.Valid {
				result.Success++
				successful = append(successful, rec.LatencyMS)
				if rec.ScheduleLatencyMS <= 250 {
					good250++
				}
				if rec.ScheduleLatencyMS <= 1000 {
					good1000++
				}
			} else {
				result.Errors++
			}
			if rec.Valid && rec.Rank > 0 {
				result.MRR += 1 / float64(rec.Rank)
				for _, k := range []int{1, 3, 10, 20} {
					if k > c.K {
						continue
					}
					if rec.Rank <= k {
						result.HitAt[fmt.Sprint(k)]++
					}
				}
			}
		}
		if err := gz.Close(); writeErr == nil {
			writeErr = err
		}
		if err := f.Close(); writeErr == nil {
			writeErr = err
		}
		logDone <- writeErr
	}()
	transport := &http.Transport{MaxIdleConns: c.Concurrency * 2, MaxIdleConnsPerHost: c.Concurrency, MaxConnsPerHost: c.Concurrency, IdleConnTimeout: 90 * time.Second, DisableCompression: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: c.Timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect refused") }}
	start := time.Now()
	result.Started = start.UTC().Format(time.RFC3339Nano)
	var inflight, peak atomic.Int64
	run := func(j job) {
		p := ps[j.index]
		sent := time.Now()
		n := inflight.Add(1)
		for {
			old := peak.Load()
			if n <= old || peak.CompareAndSwap(old, n) {
				break
			}
		}
		req, reqErr := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.String(), bytes.NewReader(p.body))
		var rec Record
		if reqErr == nil {
			req.Header.Set("Content-Type", "application/json")
			resp, e := client.Do(req)
			reqErr = e
			if resp != nil {
				rec.Status = resp.StatusCode
				body, e := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
				resp.Body.Close()
				rec.Bytes = len(body)
				done := time.Now()
				rec.Completed = done.UTC().Format(time.RFC3339Nano)
				rec.LatencyMS = float64(done.Sub(sent)) / 1e6
				rec.ScheduleLatencyMS = float64(done.Sub(j.scheduled)) / 1e6
				if e != nil {
					reqErr = e
				} else if resp.StatusCode != 200 {
					reqErr = fmt.Errorf("HTTP %d: %.200s", resp.StatusCode, body)
				} else {
					var decoded struct {
						Hits []Hit `json:"hits"`
					}
					reqErr = json.Unmarshal(body, &decoded)
					if reqErr == nil {
						v, e := Validate(decoded.Hits, p.query, c.Mode, c.K, c.hydrated())
						reqErr = e
						rec.Rank = v.Rank
						rec.ArticleRank = v.ArticleRank
						rec.IDs = v.IDs
						rec.Titles = v.Titles
						rec.Distances = v.Distances
					}
				}
				rec.ValidationMS = float64(time.Since(done)) / 1e6
			}
		}
		if rec.Completed == "" {
			done := time.Now()
			rec.Completed = done.UTC().Format(time.RFC3339Nano)
			rec.LatencyMS = float64(done.Sub(sent)) / 1e6
			rec.ScheduleLatencyMS = float64(done.Sub(j.scheduled)) / 1e6
		}
		inflight.Add(-1)
		rec.Sequence = j.seq
		rec.QueryID = p.query.ID
		rec.Scheduled = j.scheduled.UTC().Format(time.RFC3339Nano)
		rec.Sent = sent.UTC().Format(time.RFC3339Nano)
		rec.DispatchMS = float64(sent.Sub(j.scheduled)) / 1e6
		rec.Valid = reqErr == nil
		if reqErr != nil {
			rec.Error = reqErr.Error()
		}
		records <- rec
	}
	var wg sync.WaitGroup
	if c.Rate > 0 {
		jobs := make(chan job, c.Concurrency)
		for w := 0; w < c.Concurrency; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for j := range jobs {
					run(j)
				}
			}()
		}
		rng := rand.New(rand.NewSource(c.Seed))
		zipf := rand.NewZipf(rng, 1.15, 1, uint64(len(ps)-1))
		for seq := int64(0); ctx.Err() == nil; seq++ {
			due := start.Add(time.Duration(float64(seq) / c.Rate * float64(time.Second)))
			if c.Requests > 0 && seq >= int64(c.Requests) {
				break
			}
			if c.Duration > 0 && !due.Before(start.Add(c.Duration)) {
				break
			}
			if wait := time.Until(due); wait > 0 {
				timer := time.NewTimer(wait)
				select {
				case <-ctx.Done():
					timer.Stop()
				case <-timer.C:
				}
			}
			if ctx.Err() != nil {
				break
			}
			index := choose(rng, zipf, c.Distribution, int(seq), len(ps))
			j := job{index, seq, due}
			select {
			case jobs <- j:
			default:
				records <- Record{Sequence: seq, QueryID: ps[index].query.ID, Scheduled: due.UTC().Format(time.RFC3339Nano), Dropped: true, Error: "arrival queue full"}
			}
		}
		close(jobs)
	} else {
		var sequence atomic.Int64
		for w := 0; w < c.Concurrency; w++ {
			wg.Add(1)
			go func(w int) {
				defer wg.Done()
				rng := rand.New(rand.NewSource(c.Seed + int64(w)*7919))
				zipf := rand.NewZipf(rng, 1.15, 1, uint64(len(ps)-1))
				for ctx.Err() == nil {
					if c.Duration > 0 && time.Since(start) >= c.Duration {
						return
					}
					seq := sequence.Add(1) - 1
					if c.Requests > 0 && seq >= int64(c.Requests) {
						return
					}
					run(job{choose(rng, zipf, c.Distribution, int(seq), len(ps)), seq, time.Now()})
				}
			}(w)
		}
	}
	wg.Wait()
	end := time.Now()
	close(records)
	if err := <-logDone; err != nil {
		return result, err
	}
	result.Elapsed = end.Sub(start).Seconds()
	result.PeakInflight = peak.Load()
	result.QPS = float64(result.Success) / result.Elapsed
	result.Goodput250 = float64(good250) / result.Elapsed
	result.Goodput1000 = float64(good1000) / result.Elapsed
	result.SuccessLatency = Percentiles(successful)
	result.AllLatency = Percentiles(all)
	result.ScheduledLatency = Percentiles(scheduled)
	result.DispatchLatency = Percentiles(dispatch)
	if result.Scheduled > 0 {
		result.MRR /= float64(result.Scheduled)
		for k, v := range result.HitAt {
			result.HitAt[k] = v / float64(result.Scheduled)
		}
	}
	blob, _ := json.MarshalIndent(result, "", "  ")
	err = os.WriteFile(filepath.Join(c.Output, "summary.json"), blob, 0644)
	return result, err
}
func choose(r *rand.Rand, z *rand.Zipf, distribution string, seq, n int) int {
	if distribution == "sequential" {
		return seq % n
	}
	if distribution == "zipf" {
		return int(z.Uint64())
	}
	return r.Intn(n)
}
