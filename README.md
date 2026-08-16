# polign_demo

A complete, working example of [polign_db](https://github.com/Polign/polign).

It searches all of English Wikipedia (12.5 million passages) from a 2 GB server,
and you can try it at **[demo.polign.com](https://demo.polign.com)**.

If you are setting up polign_db for the first time, this repo is meant to be the
thing you read. It shows you the whole shape of a real deployment: how you get
data in, how you get answers out, and which settings actually matter.

## Start here

The fastest way to understand polign_db is to look at one file:
[`internal/polign/client.go`](internal/polign/client.go). It is about 200 lines,
uses only the Go standard library, and it is the complete client for talking to
a polign_db server. This repo imports no polign_db packages at all. It just
speaks HTTP to the server, the same way your application will.

If you read nothing else, read that file and the "How it works" section below.

## What you need

* A `polign-server` and `polign-import` binary from polign_db
* Go 1.21 or newer
* Python 3.11 or newer
* Docker (only for the first step, to read Wikipedia's parquet files)
* About 15 minutes and 5 GB of free disk

## Try it locally

This builds a small index (about 250,000 passages) and searches it. Everything
runs on your laptop, and nothing touches the cloud.

### 1. Get some Wikipedia text

```sh
mkdir -p data work
docker run --rm -v "$PWD/data:/out" -v "$PWD/work:/work" -v "$PWD/index:/idx" \
    python:3.12-slim sh -c 'pip install -q pyarrow && python /idx/prepare.py --out /out --work /work --files 2'
```

This downloads two parquet files and turns them into passage shards: one JSON
line per passage, each with an id, title, url, and text. It takes a few minutes.
You can stop it and run it again, and it picks up where it left off.

### 2. Set up the embedding model

```sh
python3 -m venv venv
./venv/bin/pip install -q onnxruntime transformers optimum[onnxruntime] numpy
./venv/bin/python index/export_model.py bge-small
```

This downloads a sentence embedding model and converts it to a compressed format
that runs fast on a CPU. You do not need a GPU or an API key.

The model turns text into a list of numbers (a vector). Passages that mean
similar things get similar vectors, which is what makes semantic search work.

### 3. Turn passages into vectors

```sh
./venv/bin/python index/embed.py \
    --model bge-small \
    --shard data/passages-00000.jsonl \
    --out shard.jsonl
```

This is the slow part, because every passage goes through the model. On a laptop
expect roughly 100 passages per second per core. The output is the same passages
with a vector attached to each one.

### 4. Build the index

```sh
polign-import -stores fs:/tmp/polign-store -collection wikipedia \
    -metric l2 -dim 384 -pqm 96 -compact shard.jsonl
```

This is where polign_db comes in. `polign-import` reads your vectors and writes
a searchable index into `/tmp/polign-store`. No server is running yet, and none
is needed. The index is just files.

What the flags mean:

| flag | what it does |
|---|---|
| `-stores fs:/tmp/polign-store` | where to write. Use `s3://bucket/prefix` in production |
| `-collection wikipedia` | the name you will search later |
| `-dim 384` | how many numbers are in each vector. Must match your model |
| `-metric l2` | how to measure distance between vectors |
| `-pqm 96` | also store compressed vectors, so queries read less data |
| `-compact` | tidy the index at the end, which makes queries faster |

### 5. Start the server and search

```sh
# The database server.
polign-server -store fs:/tmp/polign-store \
    -restore-stores "" -hot-max 0 -persist=false -maintain 0 \
    -http 127.0.0.1:23000 &

# The embedding model, so queries can be turned into vectors too.
./venv/bin/python serve/embedserve.py --model bge-small --port 23200 &

# The search app.
go run ./cmd/demo -dir ./data -node http://127.0.0.1:23000 \
    -embed-addr http://127.0.0.1:23200 -collection wikipedia -http :24000
```

Open http://localhost:24000 and search.

Those flags on `polign-server` all switch things off. By default, `-store` gives
you a full read and write database that loads the whole index into memory when it
starts. This demo wants the opposite: a server that keeps almost nothing in
memory and reads from storage for each query. See
[deploy/polign-node.service](deploy/polign-node.service) for what each flag
prevents.

## How it works

Everything in this repo is either putting data in, or getting answers out.

```
WRITE PATH  (run once, on a big machine, then shut it down)

  index/prepare.py     Wikipedia dump   ->  passage shards
  index/embed.py       passage shards   ->  passages with vectors
  polign-import        vectors          ->  index files in storage

                                 |
                                 v
                    +-------------------------+
                    |     object storage      |
                    |   (S3, GCS, or disk)    |
                    +-------------------------+
                                 |
                                 |  small reads, one query at a time
                                 v

READ PATH  (runs forever, on a small machine)

  serve/embedserve.py  your question    ->  a vector
  cmd/demo             vector           ->  polign-server  ->  results
```

The write path is expensive but temporary. The read path is cheap and permanent.
Splitting them is the main idea: you rent a big machine for a few hours to build
the index, then serve it from a small one indefinitely.

## What is in this repo

| folder | what it is |
|---|---|
| [`index/`](index/) | write path: prepare text, export the model, embed, build the index |
| [`serve/`](serve/) | read path: the service that turns a question into a vector |
| [`cmd/demo/`](cmd/demo/) | read path: the search page and its API |
| [`internal/polign/`](internal/polign/) | the polign_db client. Start here |
| [`internal/embedder/`](internal/embedder/) | client for the embedding service |
| [`internal/corpus/`](internal/corpus/) | reads the dataset description file |
| [`deploy/`](deploy/) | service files and a production runbook |
| [`eval/`](eval/) | how to measure whether your search is any good |

## Three things that will save you time

**Use the same model for indexing and searching.** A vector only means something
relative to the model that produced it. If you index with one model and search
with another, you get results that look plausible and are actually nonsense, with
no error message anywhere. That is why `index/embed.py` and
`serve/embedserve.py` load the same model directory, and why the search app
refuses to start if the embedding service is not answering.

**Your model matters more than your database settings.** When we replaced a
simple embedding model with a proper one here, result quality tripled. No index
tuning we tried came close to that. If your search results are disappointing,
look at the model first.

**Measure before you tune.** Two things we assumed turned out to be wrong when we
checked. Searching more of the index (`nprobe`) made no difference to quality at
all on this data. And combining keyword search with semantic search, which is
usually recommended, made results worse rather than better. The numbers are in
[eval/](eval/), along with the scripts that produced them.

## What it costs to run

Measured on the live demo: 12.5 million passages, one small server, S3 storage.

| | |
|---|---|
| memory, sitting idle | 27 MB |
| memory, under a burst of queries | 290 MB |
| repeat query | 25 to 50 ms |
| first query for a new topic | 0.4 to 2 seconds |
| building the whole index | about 7 hours on a 48 core machine |

Memory stays flat as the collection grows, because the server never holds the
data. What grows is how much each query reads from storage. If you want every
query to be fast rather than just the repeated ones, polign_db can keep data in
memory instead. That is the same software with different flags.

## Attribution

Text from [English Wikipedia](https://en.wikipedia.org) via the
[wikimedia/wikipedia](https://huggingface.co/datasets/wikimedia/wikipedia)
`20231101.en` dataset, CC BY-SA 4.0. Embeddings from
[BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), MIT.
