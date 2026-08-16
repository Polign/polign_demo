"""Embed prepared passage shards with bge-small-en-v1.5 into polign-import JSONL.

This is the expensive half of building the index: a real 12-layer encoder at 384
dims, roughly 150x the compute per passage of a static token-averaging model,
and worth it — the model is the single largest factor in result quality (see
eval/). The batching care below is what makes that cost tolerable.

Two properties are load-bearing for accuracy, and both must match at query time
(see serve/embedserve.py):

  - CLS pooling, then L2 normalization. bge is trained that way; mean pooling
    silently degrades it.
  - The query prefix is asymmetric. Passages are embedded bare; queries get
    "Represent this sentence for searching relevant passages: ". Embedding a
    passage WITH the prefix, or a query WITHOUT it, costs real recall.

Throughput: measured 15.8 passages/s per vCPU on Graviton3 (int8, length
bucketed). Padding is the cost that batching controls -- passages run p50=105
tokens against a 256 cap, so sorting a window by length before batching lifted
throughput 1.6x. Emitted order follows the sort, which is irrelevant to the
importer (it routes by vector, not by position).

    embed.py --model bge-small --shard data/passages-00000.jsonl --out shard.jsonl
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np

# A window is read, sorted by length, and dealt out as slices. It must be much
# larger than a slice for the sort to have anything to work with, and small
# enough to stay comfortably in memory (a window of records is ~1 KB each).
WINDOW = 65536
# Slices are deliberately much smaller than WINDOW/workers: sorting the window
# by length makes each slice near-uniform (so batches barely pad), but it also
# makes them unequal in cost, since the last slice holds the longest passages.
# Many small slices let the pool load-balance dynamically instead of blocking on
# whichever worker drew the long tail.
SLICE = 512
BATCH = 32
MAX_TOKENS = 256


def load_session(model_dir, threads):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    path = os.path.join(model_dir, "model_quantized.onnx")
    if not os.path.exists(path):
        path = os.path.join(model_dir, "model.onnx")
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


_state = {}


def _init(model_dir, threads):
    from transformers import AutoTokenizer
    _state["sess"] = load_session(model_dir, threads)
    _state["tok"] = AutoTokenizer.from_pretrained(model_dir)
    _state["names"] = {i.name for i in _state["sess"].get_inputs()}


def embed_texts(texts):
    """Embed a list of texts in input order. CLS pooled, L2 normalized.

    The caller has already length-sorted the window, so texts arrives roughly
    uniform and the batches below pad by only a few tokens.
    """
    sess, tok, names = _state["sess"], _state["tok"], _state["names"]
    out = np.empty((len(texts), 384), dtype=np.float32)
    for s in range(0, len(texts), BATCH):
        chunk = texts[s:s + BATCH]
        enc = tok(chunk, padding=True, truncation=True,
                  max_length=MAX_TOKENS, return_tensors="np")
        res = sess.run(None, {k: v for k, v in enc.items() if k in names})[0]
        v = res[:, 0].astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        out[s:s + len(chunk)] = v
    return out


def _work(raw_lines):
    """Parse, embed, and re-encode one slice.

    Workers take raw input lines rather than parsed records: json.loads over a
    whole window costs seconds of single-threaded parent time during which 48
    workers sit idle, and shipping strings through the pool is cheaper than
    shipping dicts.
    """
    chunk = [json.loads(line) for line in raw_lines]
    vecs = embed_texts([r["text"] for r in chunk])
    lines = []
    for rec, vec in zip(chunk, vecs):
        lines.append(json.dumps({
            "id": rec["id"],
            # round-tripping through float() keeps the JSON compact; the
            # importer parses to float32 anyway.
            "values": [round(float(x), 6) for x in vec],
            "metadata": {
                "title": rec["title"],
                "url": rec["url"],
                "text": rec["text"],
            },
        }, ensure_ascii=False))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--shard", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--threads-each", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    t0 = time.perf_counter()
    done = 0
    tmp = args.out + ".partial"
    ctx = mp.get_context("fork")
    with ctx.Pool(args.workers, initializer=_init,
                  initargs=(args.model, args.threads_each)) as pool, \
            open(tmp, "w") as sink, open(args.shard) as src:
        window = []

        def drain():
            nonlocal done
            if not window:
                return
            # Sort by raw line length -- a good proxy for token count, and free
            # compared to parsing -- then hand out small contiguous slices:
            # uniform padding within a slice, dynamic balancing across them.
            # Output order follows completion, which the importer does not care
            # about: it routes each record by its vector.
            window.sort(key=len)
            slices = [window[i:i + SLICE] for i in range(0, len(window), SLICE)]
            for lines in pool.imap_unordered(_work, slices):
                sink.write("\n".join(lines))
                sink.write("\n")
            done += len(window)
            rate = done / (time.perf_counter() - t0)
            print(f"  embedded {done} ({rate:.0f}/s)", flush=True)
            window.clear()

        for line in src:
            window.append(line)
            if args.limit and done + len(window) >= args.limit:
                break
            if len(window) >= WINDOW:
                drain()
        drain()

    os.rename(tmp, args.out)
    dt = time.perf_counter() - t0
    print(f"embedded {done} passages in {dt:.0f}s ({done/dt:.0f}/s) -> {args.out}",
          flush=True)


if __name__ == "__main__":
    sys.exit(main())
