#!/usr/bin/env python3
"""Split the prepared dataset into N views for parallel loading.

Each view is a directory of symlinks: the shared model files plus a disjoint
subset of the passage shards, with its own corpus.json listing only that
subset. The loader keys its checkpoint by directory, so N loaders against N
nodes never touch each other's state, and the shards themselves are never
copied.
"""
import json
import os
import sys

DATA = os.environ.get("DATA", os.path.expanduser("~/data"))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
MODEL_FILES = ["model.json", "vocab.txt", "embeddings.f32", "examples.txt"]

manifest = json.load(open(os.path.join(DATA, "corpus.json")))
shards = manifest["shards"]
stats = manifest.get("shard_stats", {})

for i in range(N):
    view = f"{DATA}-p{i}"
    os.makedirs(view, exist_ok=True)
    for f in MODEL_FILES:
        link = os.path.join(view, f)
        if not os.path.exists(link):
            os.symlink(os.path.join(DATA, f), link)

    # Round-robin so every view gets a similar mix of shard sizes.
    mine = [s for j, s in enumerate(shards) if j % N == i]
    for s in mine:
        link = os.path.join(view, s)
        if not os.path.exists(link):
            os.symlink(os.path.join(DATA, s), link)

    sub = dict(manifest)
    sub["shards"] = mine
    sub["shard_stats"] = {s: stats[s] for s in mine if s in stats}
    sub["passages"] = sum(v["passages"] for v in sub["shard_stats"].values())
    json.dump(sub, open(os.path.join(view, "corpus.json"), "w"), indent=2)
    print(f"{view}: {len(mine)} shards, {sub['passages']:,} passages")
