# Retrieval quality

Two scripts and the numbers they produced on the live deployment. They exist
because "does search work?" is not a yes/no question, and because two of the
things that looked obviously true here turned out to be false when measured.

```sh
python3 quality.py --base https://demo.polign.com --mode semantic
python3 sweep-nprobe.py --nprobes 8,16,32,64      # run on the serving host
```

`quality.py` scores a fixed 30-query set: each case names the article a
competent search should surface, and scoring asks only whether it appears and
how high. The set deliberately mixes relational queries ("capital of france"),
plain topic lookups ("plate tectonics") and keyword-shaped ones ("magna carta"),
because a comparison that only includes what one approach is good at is not a
comparison.

## What the numbers say

Same corpus, same 30 queries, on 12.5M passages:

| | hit@1 | hit@3 | hit@10 | MRR |
|---|---|---|---|---|
| static embedder (model2vec) | 3% | 10% | 37% | 0.101 |
| **sentence-transformer (bge-small)** | **27%** | **37%** | **47%** | **0.336** |
| BM25 only | 0% | 3% | 10% | 0.024 |
| hybrid (RRF of both) | 7% | 27% | 43% | 0.168 |

Raw output in [`results/`](results/).

**The embedding model dominates everything else.** Swapping a static
token-averaging model for a real sentence-transformer tripled MRR. Nothing else
measured here came close to that effect size.

**Hybrid is worse than semantic on this corpus, not better.** That is the
opposite of the usual advice and it is worth understanding before copying the
pattern. BM25 here surfaces disambiguation and list pages — short documents,
dense in the query terms — because the analyzer has no stop-word handling and
there is no title-field weighting. Reciprocal-rank fusion then pulls a good
semantic ranking toward a poor lexical one. Title-weighted BM25 is the work that
would make hybrid worth enabling; until then the demo defaults to semantic.

**nprobe does not affect quality here at all.** Flat from 8 to 64 — MRR 0.336 at
every value. IVF recall is not what limits this deployment, so the setting is
purely a cost decision, and the demo uses the cheapest one. Do not tune it
blind: measure whether recall binds before paying for it.

## The general lesson

Every one of those findings contradicts a reasonable prior. The index knobs
looked like where quality lives; they were not. Hybrid looked like a free win;
it was a regression. If you take one thing from this directory, take the habit
of running the harness rather than the conclusions in the table.
