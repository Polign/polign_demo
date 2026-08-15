"""Sweep nprobe against the v2 collection, measuring quality and cost together.

Run this ON the serving host: it talks to the node and the embedding sidecar
directly, because the public app deliberately fixes nprobe (it is the knob that
decides how expensive a query is, and it is not a public input).

nprobe is the cold path's whole cost model. A cell holds ~sqrt(N) vectors, so at
12.5M each is several MB of full-f32 vectors plus the passage text stored beside
them, and a query fetches nprobe of them. Quality rises with nprobe and so does
latency, so the operating point has to be chosen from measurements rather than
assumed — v1 pinned 4 on the reasoning that higher bought nothing, which was
true only because its static embedder, not its recall, was the binding limit.

    python3 sweep-nprobe.py --nprobes 4,8,16,24,32

Latency is reported cold (first touch of a region, paying S3) and warm (repeat,
served from the local disk cache); both matter, since a demo's traffic is mostly
cold and its front page is mostly warm.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

NODE = "http://127.0.0.1:23000"
EMBED = "http://127.0.0.1:23200/embed"


def post(url, payload, timeout=180):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_cases():
    """Reuse the evaluation set so sweep and eval agree on what 'good' means."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "evalq", __file__.rsplit("/", 1)[0] + "/eval-quality.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CASES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="wikipedia_bge")
    ap.add_argument("--nprobes", default="4,8,16,24,32")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    cases = load_cases()
    vecs = {q: post(EMBED, {"text": q})["values"] for q, _ in cases}
    print(f"{len(cases)} queries, embedded\n")

    out = []
    header = f"{'nprobe':>7} {'hit@1':>7} {'hit@3':>7} {'hit@10':>7} {'MRR':>6} {'cold p50':>9} {'warm p50':>9}"
    print(header)
    print("-" * len(header))

    for nprobe in [int(x) for x in args.nprobes.split(",")]:
        hits = {1: 0, 3: 0, 10: 0}
        rr = 0.0
        cold, warm = [], []

        for q, expected in cases:
            body = {"values": vecs[q], "k": args.k, "cold": True, "nprobe": nprobe}

            t0 = time.perf_counter()
            res = post(f"{NODE}/v1/collections/{args.collection}/query", body)
            cold.append((time.perf_counter() - t0) * 1000)

            # Immediately repeat: the cells are now in the local disk cache, so
            # this isolates compute from object-store round trips.
            t1 = time.perf_counter()
            post(f"{NODE}/v1/collections/{args.collection}/query", body)
            warm.append((time.perf_counter() - t1) * 1000)

            titles = [h.get("metadata", {}).get("title", "") for h in res.get("hits", [])]
            for i, t in enumerate(titles, start=1):
                if t in expected:
                    rr += 1.0 / i
                    for n in hits:
                        if i <= n:
                            hits[n] += 1
                    break

        n = len(cases)
        row = {"nprobe": nprobe, "hit_at": dict(hits), "mrr": rr / n,
               "cold_p50_ms": statistics.median(cold),
               "warm_p50_ms": statistics.median(warm)}
        out.append(row)
        print(f"{nprobe:>7} {hits[1]:>4}/{n:<2} {hits[3]:>4}/{n:<2} {hits[10]:>4}/{n:<2} "
              f"{rr/n:>6.3f} {statistics.median(cold):>8.0f}m {statistics.median(warm):>8.0f}m")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
