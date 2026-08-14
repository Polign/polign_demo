#!/usr/bin/env python3
"""Prepare the full English Wikipedia demo corpus.

Streams the wikimedia/wikipedia 20231101.en dump (41 parquet files, ~6.4M
articles) into passage shards the loader can feed to a polign_db node, and
exports the static embedding model the loader and the search app both use:

    model.json          embedding-model metadata (dim, vocab size, source)
    vocab.txt           WordPiece vocabulary, one token per line, in id order
    embeddings.f32      token embedding matrix, row-major little-endian float32
    passages-00000.jsonl … one shard per source parquet file
    corpus.json         dataset manifest: counts, shard order, model, dim
    examples.txt        curated example queries shown in the demo UI
    ATTRIBUTION.md      licenses for the model and the text

The embedding model is minishlab/potion-base-8M (MIT), a model2vec static
model: a sentence embedding is the L2-normalized mean of its WordPiece token
embeddings. That is trivially reimplementable in Go, which is what makes the
demo's free-text search work with no inference runtime and no API key — see
internal/embedder. This script never embeds anything itself; the Go loader
does, so corpus and query vectors come from the same code.

Scale note: the full run downloads ~20 GB of parquet and writes ~4 GB of
JSONL. Both --work and --out want room. Shards are written atomically and
skipped if already complete, so an interrupted run resumes where it stopped.

Run via Docker (no local Python setup needed):

    docker run --rm -v "$PWD/data:/out" -v "$PWD/work:/work" -v "$PWD/prepare:/prep" \
        python:3.12-slim sh -c 'pip install -q pyarrow && python /prep/prepare.py --out /out --work /work'

A smaller slice for smoke tests (first two parquet files, ~250k passages):

    ... python /prep/prepare.py --out /out --work /work --files 2
"""

import argparse
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request

MODEL_REPO = "minishlab/potion-base-8M"
MODEL_BASE = f"https://huggingface.co/{MODEL_REPO}/resolve/main"

DATASET = "wikimedia/wikipedia 20231101.en"
DUMP_BASE = (
    "https://huggingface.co/datasets/wikimedia/wikipedia/resolve/main/20231101.en"
)
# The 20231101.en config ships as 41 shards named train-000NN-of-00041.parquet.
DUMP_FILES = 41

# A passage is one paragraph. Very short paragraphs are stubs and navigation
# noise; very long ones dilute a static-embedding mean vector until it means
# nothing in particular.
MIN_CHARS, MAX_CHARS = 200, 1200
# Paragraphs kept per article. The lead paragraphs carry the article's topic,
# which is what a semantic search over Wikipedia should match on; deeper
# paragraphs add bulk faster than they add answers.
PER_ARTICLE = 3

EXAMPLE_QUERIES = [
    "why do volcanoes erupt",
    "who invented the telephone",
    "largest animal in the ocean",
    "why is the sky blue",
    "how do bees make honey",
    "first person to walk on the moon",
    "what causes earthquakes",
    "how does a nuclear reactor work",
    "why did the roman empire fall",
    "what is the difference between weather and climate",
]

ATTRIBUTION = """# Demo dataset attribution

- Embedding model: [potion-base-8M](https://huggingface.co/minishlab/potion-base-8M)
  by The Minish Lab, MIT license. A model2vec static embedding model; this demo
  reimplements its inference (WordPiece tokenize, mean, L2-normalize) in Go.
- Text: [English Wikipedia](https://en.wikipedia.org), from the
  [wikimedia/wikipedia](https://huggingface.co/datasets/wikimedia/wikipedia)
  20231101.en dump, licensed CC BY-SA 4.0. Each passage links back to its
  source article.
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, dest: str) -> None:
    """Download url to dest unless it is already there. Partial downloads land
    on a .part path first, so an interrupted run never leaves a short file that
    looks complete."""
    if os.path.exists(dest):
        return
    log(f"  fetching {url}")
    tmp = dest + ".part"
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    os.rename(tmp, dest)


def export_model(work: str, out: str) -> int:
    """Write vocab.txt, embeddings.f32, and model.json. Returns the dimension."""
    fetch(f"{MODEL_BASE}/tokenizer.json", os.path.join(work, "tokenizer.json"))
    fetch(f"{MODEL_BASE}/model.safetensors", os.path.join(work, "model.safetensors"))
    fetch(f"{MODEL_BASE}/config.json", os.path.join(work, "config.json"))

    with open(os.path.join(work, "tokenizer.json")) as f:
        tok = json.load(f)
    model = tok["model"]
    norm = tok["normalizer"]
    # The Go embedder hardcodes BERT normalization + WordPiece. Refuse to
    # export a model it would silently mis-tokenize.
    assert model["type"] == "WordPiece", model["type"]
    assert model["continuing_subword_prefix"] == "##"
    assert norm["type"] == "BertNormalizer" and norm["lowercase"], norm
    assert tok["pre_tokenizer"]["type"] == "BertPreTokenizer"

    vocab = model["vocab"]
    by_id = sorted(vocab.items(), key=lambda kv: kv[1])
    assert [i for _, i in by_id] == list(range(len(by_id))), "vocab ids not dense"
    with open(os.path.join(out, "vocab.txt"), "w") as f:
        for token, _ in by_id:
            f.write(token + "\n")

    # safetensors: u64 header length, JSON header, then raw little-endian
    # tensor data. The single "embeddings" tensor is copied out verbatim.
    with open(os.path.join(work, "model.safetensors"), "rb") as f:
        hlen = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(hlen))
        info = header["embeddings"]
        assert info["dtype"] == "F32", info["dtype"]
        rows, dim = info["shape"]
        assert rows == len(by_id), (rows, len(by_id))
        begin, end = info["data_offsets"]
        f.seek(8 + hlen + begin)
        data = f.read(end - begin)
    assert len(data) == rows * dim * 4
    with open(os.path.join(out, "embeddings.f32"), "wb") as f:
        f.write(data)

    with open(os.path.join(work, "config.json")) as f:
        cfg = json.load(f)
    meta = {
        "format": 1,
        "source": MODEL_REPO,
        "dim": dim,
        "vocab_size": rows,
        "unk_token": model["unk_token"],
        "max_input_chars_per_word": model.get("max_input_chars_per_word", 100),
        "normalize": bool(cfg.get("normalize", True)),
    }
    with open(os.path.join(out, "model.json"), "w") as f:
        json.dump(meta, f, indent=2)
    log(f"  model: {rows} tokens x {dim} dims")
    return dim


def clean_paragraph(p: str) -> str:
    return re.sub(r"\s+", " ", p).strip()


def build_shard(src: str, dest: str, target: int) -> tuple[int, int]:
    """Chunk one parquet file into a passage shard.

    Returns (passages, articles). Reads row-group by row-group so peak memory
    is one row group, not the whole file. target caps the passages written
    (0 = no cap), which is how --limit stops mid-shard.
    """
    import pyarrow.parquet as pq

    passages = articles = 0
    tmp = dest + ".part"
    pf = pq.ParquetFile(src)
    with open(tmp, "w") as f:
        for batch in pf.iter_batches(batch_size=2048, columns=["id", "url", "title", "text"]):
            for row in batch.to_pylist():
                kept = 0
                for i, para in enumerate(row["text"].split("\n\n")):
                    if kept == PER_ARTICLE:
                        break
                    para = clean_paragraph(para)
                    if not (MIN_CHARS <= len(para) <= MAX_CHARS):
                        continue
                    rec = {
                        "id": f"wiki-{row['id']}-{i}",
                        "title": row["title"],
                        "url": row["url"],
                        "text": para,
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    passages += 1
                    kept += 1
                if kept:
                    articles += 1
                if target and passages >= target:
                    os.rename(tmp, dest)
                    return passages, articles
    os.rename(tmp, dest)
    return passages, articles


def load_shard_stats(out: str) -> dict:
    """Read per-shard counts from an existing manifest, if any."""
    try:
        with open(os.path.join(out, "corpus.json")) as f:
            return json.load(f).get("shard_stats", {})
    except (OSError, ValueError):
        return {}


def write_manifest(out: str, shards: list, stats: dict, dim: int) -> int:
    """Write corpus.json for the shards built so far. Returns total passages."""
    used = {name: stats[name] for name in shards}
    passages = sum(s["passages"] for s in used.values())
    manifest = {
        "dataset": DATASET,
        "articles": sum(s["articles"] for s in used.values()),
        "passages": passages,
        "shards": shards,
        "shard_stats": used,
        "model": MODEL_REPO,
        "dim": dim,
    }
    tmp = os.path.join(out, "corpus.json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, os.path.join(out, "corpus.json"))
    return passages


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output directory for the dataset")
    ap.add_argument("--work", default=None, help="scratch dir for downloads (default: <out>/work)")
    ap.add_argument("--files", type=int, default=DUMP_FILES,
                    help=f"how many of the {DUMP_FILES} parquet files to process (default: all)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after roughly this many passages (0 = no limit)")
    ap.add_argument("--model-only", action="store_true", help="export the model files and stop")
    args = ap.parse_args()

    if not 1 <= args.files <= DUMP_FILES:
        sys.exit(f"--files must be 1..{DUMP_FILES}")

    work = args.work or os.path.join(args.out, "work")
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    log("==> model")
    dim = export_model(work, args.out)
    if args.model_only:
        log("--model-only: done")
        return

    log(f"==> passages ({args.files} of {DUMP_FILES} parquet files)")
    # Per-shard counts live in the manifest so a resumed run recovers them for
    # shards it is skipping — recounting a cached shard would recover its
    # passages but not how many articles they came from.
    stats = load_shard_stats(args.out)
    shards: list[str] = []
    for n in range(args.files):
        name = f"passages-{n:05d}.jsonl"
        dest = os.path.join(args.out, name)
        src_name = f"train-{n:05d}-of-{DUMP_FILES:05d}.parquet"
        src = os.path.join(work, src_name)

        if os.path.exists(dest):
            # Already built by an earlier run: keep it rather than re-download
            # 500 MB of parquet to reproduce a file we have.
            if name not in stats:
                with open(dest) as f:
                    stats[name] = {"passages": sum(1 for _ in f), "articles": 0}
            shards.append(name)
            log(f"  [{n + 1}/{args.files}] {name} — {stats[name]['passages']} passages (cached)")
        else:
            try:
                fetch(f"{DUMP_BASE}/{src_name}", src)
            except urllib.error.HTTPError as e:
                sys.exit(f"fetch {src_name}: {e} (does the 20231101.en config still have {DUMP_FILES} files?)")

            done = sum(s["passages"] for s in stats.values())
            remaining = max(0, args.limit - done) if args.limit else 0
            p, a = build_shard(src, dest, remaining)
            stats[name] = {"passages": p, "articles": a}
            shards.append(name)
            log(f"  [{n + 1}/{args.files}] {name} — {p} passages from {a} articles")

            # The parquet is only needed to build its shard; at 41 files that
            # is ~20 GB of scratch nobody wants to keep.
            os.remove(src)

        # Rewritten after every shard, so an interrupted run still leaves a
        # manifest that describes exactly the shards on disk.
        total = write_manifest(args.out, shards, stats, dim)
        if args.limit and total >= args.limit:
            log(f"  --limit {args.limit} reached")
            break

    total_passages = write_manifest(args.out, shards, stats, dim)
    with open(os.path.join(args.out, "examples.txt"), "w") as f:
        f.write("\n".join(EXAMPLE_QUERIES) + "\n")
    with open(os.path.join(args.out, "ATTRIBUTION.md"), "w") as f:
        f.write(ATTRIBUTION)

    log(f"==> {total_passages} passages in {len(shards)} shard(s) -> {args.out}")
    log("    next: cmd/load streams these into a running node (see README.md)")


if __name__ == "__main__":
    main()
