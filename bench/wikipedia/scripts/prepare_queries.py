#!/usr/bin/env python3
"""Embed query text only, using the copied live-demo model; no corpus writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("embedserve", root / "embedserve.py")
embed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embed)
embed.load(str(root / "model"), 1)
train = [json.loads(s) for s in (root / "NQ-open.train.jsonl").read_text().splitlines()]
dev = [json.loads(s) for s in (root / "NQ-open.dev.jsonl").read_text().splitlines()]
rng = random.Random(20260906)
rng.shuffle(train)
rng.shuffle(dev)
dev_text = {q["question"].strip().lower() for q in dev}
seen = set()
train = [q for q in train if q["question"].strip().lower() not in dev_text]
sets = {"quality": dev[:300], "tuning": train[:100], "load": train[100:]}
out = root / "fixtures"
out.mkdir(exist_ok=True)
manifest = {"seed":20260906,"source":"https://github.com/google-research-datasets/natural-questions/tree/master/nq_open",
            "metric":"answer-containing passage retrieval; token-bounded case-insensitive matching",
            "query_prefix":embed.QUERY_PREFIX,"model_sha256":hashlib.sha256((root / "model/model_quantized.onnx").read_bytes()).hexdigest(),"sets":{}}
for label,rows in sets.items():
    target = 10000 if label == "load" else len(rows)
    path = out / f"{label}.vectors.jsonl"
    count = 0
    with path.open("w") as f:
        for q in rows:
            text = q["question"].strip()
            if text.lower() in seen:
                continue
            seen.add(text.lower())
            row = {"id":label+"-"+hashlib.sha256(text.encode()).hexdigest()[:16],"query":text,
                   "answers":q["answer"],"split":label,"values":[float(x) for x in embed.embed_query(text)]}
            f.write(json.dumps(row,separators=(",",":"))+"\n")
            count += 1
            if count%500 == 0: print(label,count,flush=True)
            if count==target: break
    manifest["sets"][label] = {"count":count,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    print(label,"complete",count,flush=True)
legacy = json.loads((root / "regression.json").read_text())
with (out / "regression.vectors.jsonl").open("w") as f:
    for i,(query,titles) in enumerate(legacy):
        f.write(json.dumps({"id":f"regression-{i}","query":query,"expected_titles":titles,"split":"regression","values":[float(x) for x in embed.embed_query(query)]})+"\n")
manifest["sets"]["regression"] = {"count":len(legacy),"sha256":hashlib.sha256((out / "regression.vectors.jsonl").read_bytes()).hexdigest()}
(out / "manifest.json").write_text(json.dumps(manifest,indent=2))
print("Fixture preparation complete",flush=True)
