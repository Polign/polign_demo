"""Export the embedding model to ONNX, quantized to int8.

Both halves of the system load what this writes: index/embed.py to embed the
corpus, serve/embedserve.py to embed queries. Exporting once into a directory
they share is what guarantees they cannot drift onto different models — a
collection can only be searched by the model that wrote it, and a mismatch does
not error, it just returns nonsense.

int8 is worth the step: measured 3x the throughput of fp32 on the corpus, at no
measurable cost to retrieval quality.

    python export_model.py bge-small
"""

import os
import sys

from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig
from transformers import AutoTokenizer

MODEL = "BAAI/bge-small-en-v1.5"


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "bge-small"
    os.makedirs(out, exist_ok=True)

    print(f"exporting {MODEL} -> {out}", flush=True)
    ORTModelForFeatureExtraction.from_pretrained(MODEL, export=True).save_pretrained(out)
    AutoTokenizer.from_pretrained(MODEL).save_pretrained(out)

    print("quantizing to int8 (dynamic, per-channel)", flush=True)
    quantizer = ORTQuantizer.from_pretrained(out)
    # arm64=True targets Graviton's dot-product path; the graph also runs on x86.
    quantizer.quantize(save_dir=out,
                       quantization_config=AutoQuantizationConfig.arm64(is_static=False, per_channel=True))

    for name in sorted(os.listdir(out)):
        size = os.path.getsize(os.path.join(out, name)) // 1024
        print(f"  {name}  {size} KiB")


if __name__ == "__main__":
    main()
