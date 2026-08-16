"""Query embedding sidecar: bge-small-en-v1.5 over loopback HTTP.

The demo app is Go and the model is ONNX, so the query embedder runs as its own
process rather than inside the app. It is deliberately tiny: one model, one
endpoint, loopback only, no dependencies beyond onnxruntime and transformers.

    POST /embed  {"text": "who invented the telephone"}
    -> {"values": [...384 floats...]}

The prefix is the subtle part. bge is trained asymmetrically: passages are
embedded bare, queries are embedded behind "Represent this sentence for
searching relevant passages: ". Dropping it costs real recall, so it lives here
rather than in the caller, where it could be forgotten -- index/embed.py
deliberately does NOT apply it to passages.

Single-threaded on purpose: a query is one short sequence, extra intra-op
threads cost more in synchronization than they save, and the serving host has
two cores it would rather spend on the node.
"""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MAX_TOKENS = 128  # queries are short; the cap only bounds a hostile request

_lock = threading.Lock()
_sess = None
_tok = None
_names = None


def load(model_dir, threads):
    global _sess, _tok, _names
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    path = os.path.join(model_dir, "model_quantized.onnx")
    if not os.path.exists(path):
        path = os.path.join(model_dir, "model.onnx")
    _sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
    _tok = AutoTokenizer.from_pretrained(model_dir)
    _names = {i.name for i in _sess.get_inputs()}


def embed_query(text):
    enc = _tok([QUERY_PREFIX + text], padding=True, truncation=True,
               max_length=MAX_TOKENS, return_tensors="np")
    # ORT sessions are thread-safe for Run, but serializing here keeps latency
    # predictable on a two-core box under a burst.
    with _lock:
        out = _sess.run(None, {k: v for k, v in enc.items() if k in _names})[0]
    v = out[0, 0].astype(np.float32)
    v /= np.linalg.norm(v)
    return v


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/embed":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 8192:
                self._send(400, {"error": "bad content length"})
                return
            text = json.loads(self.rfile.read(n)).get("text", "").strip()
            if not text:
                self._send(400, {"error": "missing text"})
                return
            self._send(200, {"values": [float(x) for x in embed_query(text)]})
        except Exception as exc:  # noqa: BLE001 - report, never take the process down
            self._send(500, {"error": str(exc)})

    def log_message(self, *_args):
        pass  # the demo app logs requests; this would only duplicate them


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=23200)
    ap.add_argument("--threads", type=int, default=1)
    args = ap.parse_args()

    load(args.model, args.threads)
    embed_query("warm up the graph so the first real query is not the slow one")
    print(f"embedserve on {args.host}:{args.port} ({args.model})", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
