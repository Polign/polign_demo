"""Compare retrieval quality between two demo deployments.

Comparing two deployments — different models, different modes, different
settings — only means something against a fixed query set scored the same way
for both, rather than examples picked after the fact. Each case names the
article a competent search should surface; scoring asks only whether it appears,
and how high.

    quality.py --base https://demo.polign.com
    quality.py --base https://demo.polign.com --mode semantic

Cases are deliberately mixed: some are relational (the static model's known
weakness), some are plain topic lookups (where it should do fine), and a few are
keyword-shaped. A fair comparison has to include what each approach is good at.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request

# (query, acceptable article titles). Several titles are listed where more than
# one article is a legitimately correct top hit.
CASES = [
    # Relational queries: meaning depends on how the words combine.
    ("capital of france", ["Paris"]),
    ("who invented the telephone", ["Alexander Graham Bell", "Invention of the telephone",
                                    "History of the telephone"]),
    ("what causes the seasons", ["Season", "Axial tilt", "Effect of Sun angle on climate"]),
    ("why is the sky blue", ["Rayleigh scattering", "Diffuse sky radiation"]),
    ("who wrote romeo and juliet", ["William Shakespeare", "Romeo and Juliet"]),
    ("largest planet in the solar system", ["Jupiter"]),
    ("what year did the berlin wall fall", ["Berlin Wall", "Fall of the Berlin Wall"]),
    ("first person to walk on the moon", ["Neil Armstrong", "Apollo 11"]),
    ("who painted the mona lisa", ["Mona Lisa", "Leonardo da Vinci"]),
    ("longest river in the world", ["Nile", "Amazon River", "List of river systems by length"]),
    ("what is the speed of light", ["Speed of light"]),
    ("currency of japan", ["Japanese yen"]),
    ("who discovered penicillin", ["Alexander Fleming", "Penicillin", "History of penicillin"]),
    ("tallest mountain on earth", ["Mount Everest", "Mauna Kea"]),
    ("capital city of australia", ["Canberra"]),

    # Topic lookups: no relational trap, both models should manage these.
    ("theory of relativity", ["Theory of relativity", "General relativity",
                              "Special relativity", "Introduction to general relativity",
                              "Albert Einstein"]),
    ("causes of world war one", ["Causes of World War I", "World War I"]),
    ("how do volcanoes erupt", ["Volcano", "Types of volcanic eruptions", "Volcanic eruption"]),
    ("photosynthesis in plants", ["Photosynthesis"]),
    ("the french revolution", ["French Revolution"]),
    ("black holes in space", ["Black hole"]),
    ("history of the roman empire", ["Roman Empire", "History of the Roman Empire"]),
    ("dna structure and function", ["DNA", "Nucleic acid double helix"]),
    ("the great depression", ["Great Depression"]),
    ("plate tectonics", ["Plate tectonics"]),

    # Keyword-shaped: rare or exact terms, where BM25 would normally carry.
    ("hubble space telescope", ["Hubble Space Telescope"]),
    ("chernobyl disaster", ["Chernobyl disaster"]),
    ("magna carta", ["Magna Carta"]),
    ("battle of waterloo", ["Battle of Waterloo"]),
    ("periodic table of elements", ["Periodic table"]),
]


def search(base, q, mode, k, retries=3):
    url = f"{base}/demo/search?q={urllib.parse.quote(q)}&mode={mode}&k={k}"
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))  # the public demo is rate limited
    raise RuntimeError(f"{q!r}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--mode", default="semantic")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    hits_at = {1: 0, 3: 0, 10: 0}
    ranks, lats, rows = [], [], []

    for q, expected in CASES:
        res = search(args.base, q, args.mode, args.k)
        titles = [h.get("title", "") for h in res.get("results", [])]
        lats.append(res.get("took_ms", 0.0))

        rank = None
        for i, t in enumerate(titles, start=1):
            if t in expected:
                rank = i
                break
        rows.append((q, rank, titles[0] if titles else "-"))
        if rank:
            ranks.append(rank)
            for n in hits_at:
                if rank <= n:
                    hits_at[n] += 1
        time.sleep(0.35)  # stay under the demo's per-client rate limit

    n = len(CASES)
    # Mean reciprocal rank over all cases; a miss contributes 0.
    mrr = sum(1.0 / r for r in ranks) / n

    print(f"\n{args.base}  mode={args.mode}  ({n} queries)")
    print(f"  hit@1  {hits_at[1]:2d}/{n}  ({100*hits_at[1]/n:.0f}%)")
    print(f"  hit@3  {hits_at[3]:2d}/{n}  ({100*hits_at[3]/n:.0f}%)")
    print(f"  hit@10 {hits_at[10]:2d}/{n}  ({100*hits_at[10]/n:.0f}%)")
    print(f"  MRR    {mrr:.3f}")
    print(f"  latency p50 {statistics.median(lats):.0f} ms, "
          f"max {max(lats):.0f} ms")
    print("\n  misses and low ranks:")
    for q, rank, top in rows:
        if rank is None or rank > 3:
            where = f"rank {rank}" if rank else "MISS"
            print(f"    {where:>7}  {q:<38} top: {top[:40]}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"base": args.base, "mode": args.mode, "n": n,
                       "hit_at": hits_at, "mrr": mrr,
                       "latency_p50_ms": statistics.median(lats),
                       "rows": [{"q": q, "rank": r, "top": t} for q, r, t in rows]},
                      f, indent=2)


if __name__ == "__main__":
    sys.exit(main())
