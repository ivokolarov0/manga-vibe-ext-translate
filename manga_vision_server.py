# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#   "numpy",
#   "pillow",
#   "huggingface_hub",
#   "onnxruntime-directml; sys_platform == 'win32'",
#   "onnxruntime; sys_platform != 'win32'",
# ]
# ///
"""MangaVibe vision server: finds the text on a manga page and reads it (Japanese).

Run it on the PC with the GPU:

    uv run manga_vision_server.py

The first start downloads the two models (about 250 MB). Then put the address it prints into
MangaVibe -> Settings -> Plugins -> Manga Translate.

Models:
  - text position: Kiuyha/Manga-Bubble-YOLO (YOLO26 nano, ONNX)
  - OCR:           muscgab/manga-ocr-nar-small (ONNX). Weights are CC BY-NC-SA 4.0: non-commercial use only.

API (all answers are JSON):
  GET  /health   -> {"ok": true, "device": ..., ...}
  POST /detect   body: the page image -> {"width", "height", "boxes": [[x1, y1, x2, y2, score], ...]}
                 boxes are in pixels, in Japanese reading order (right to left, top to bottom)
  POST /ocr      body: {"image": "<base64>", "boxes": [[x1, y1, x2, y2], ...]} -> {"texts": [...]}
  POST /page     body: the page image -> {"width", "height", "blocks": [{"box": [...], "text": "..."}]}
                 (/detect and /ocr in one call)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download, snapshot_download
from PIL import Image

VERSION = "1.0.0"
DETECTOR_REPO, DETECTOR_FILE = "Kiuyha/Manga-Bubble-YOLO", "onnx/yolo26n.onnx"
OCR_REPO = "muscgab/manga-ocr-nar-small"
MAX_BODY = 40 * 1024 * 1024


# --- running ONNX models on the best device there is ---------------------------------------------

def make_session(path: str, cpu: bool) -> ort.InferenceSession:
    available = ort.get_available_providers()
    wanted = [] if cpu else [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider",
                                          "CoreMLExecutionProvider") if p in available]
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if "DmlExecutionProvider" in wanted:  # DirectML needs these two
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(path, so, providers=wanted + ["CPUExecutionProvider"])


def device_name(sess: ort.InferenceSession) -> str:
    first = sess.get_providers()[0]
    return {"CUDAExecutionProvider": "GPU (CUDA)", "DmlExecutionProvider": "GPU (DirectML)",
            "CoreMLExecutionProvider": "Apple (CoreML)"}.get(first, "CPU")


# --- text position --------------------------------------------------------------------------------

class Detector:
    SIZE = 1280  # the model was trained at 1280 x 1280

    def __init__(self, cpu: bool, threshold: float = 0.3):
        self.sess = make_session(hf_hub_download(DETECTOR_REPO, DETECTOR_FILE), cpu)
        self.input = self.sess.get_inputs()[0].name
        self.threshold = threshold

    def __call__(self, image: Image.Image) -> list[list[float]]:
        """-> [[x1, y1, x2, y2, score], ...] in page pixels, in reading order."""
        w, h = image.size
        scale = self.SIZE / max(w, h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        canvas = Image.new("RGB", (self.SIZE, self.SIZE), (114, 114, 114))  # YOLO letterbox
        canvas.paste(image.convert("RGB").resize((nw, nh), Image.Resampling.BILINEAR), (0, 0))
        x = np.asarray(canvas, np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self.sess.run(None, {self.input: x})[0][0]  # [300, 6]: x1 y1 x2 y2 score class
        boxes = []
        for x1, y1, x2, y2, score, _cls in out:
            if score < self.threshold:
                continue
            x1, x2 = sorted((min(max(x1 / scale, 0), w), min(max(x2 / scale, 0), w)))
            y1, y2 = sorted((min(max(y1 / scale, 0), h), min(max(y2 / scale, 0), h)))
            if x2 - x1 >= 4 and y2 - y1 >= 4:
                boxes.append([round(float(v), 1) for v in (x1, y1, x2, y2)] + [round(float(score), 3)])
        return reading_order(drop_overlaps(boxes))


def drop_overlaps(boxes, limit=0.5):
    """Keeps the best box where several cover the same text (the model sometimes repeats one)."""
    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1])
    kept = []
    for b in sorted(boxes, key=lambda b: -b[4]):
        for k in kept:
            inter = max(0, min(b[2], k[2]) - max(b[0], k[0])) * max(0, min(b[3], k[3]) - max(b[1], k[1]))
            # mostly the same box, or one almost inside the other
            if inter / (area(b) + area(k) - inter) > limit or inter / min(area(b), area(k)) > 0.85:
                break
        else:
            kept.append(b)
    return kept


def reading_order(boxes):
    """Japanese order: rows from the top; inside a row, right to left.
    A box joins a row when it overlaps the row's height by at least half of its own."""
    rows = []
    for b in sorted(boxes, key=lambda b: b[1]):
        for row in rows:
            top, bottom = row["top"], row["bottom"]
            overlap = min(bottom, b[3]) - max(top, b[1])
            if overlap >= 0.5 * min(b[3] - b[1], bottom - top):
                row["boxes"].append(b)
                row["bottom"] = max(bottom, b[3])
                break
        else:
            rows.append({"top": b[1], "bottom": b[3], "boxes": [b]})
    return [b for row in rows for b in sorted(row["boxes"], key=lambda b: -(b[0] + b[2]))]


# --- OCR ------------------------------------------------------------------------------------------
# Image preparation and decoding follow manga_ocr_nar_small (Apache-2.0, github.com/muscgab/manga-ocr-nar).

class Ocr:
    BUDGET, PATCH, FEAT = 324, 14, 588
    MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)

    def __init__(self, cpu: bool):
        folder = Path(snapshot_download(OCR_REPO, allow_patterns=["onnx/*", "*.json", "LICENSE*"]))
        self.sess = make_session(str(folder / "onnx/mangaocr_nar_small_fp32.onnx"), cpu)
        vocab = json.loads((folder / "nar_vocabulary_6817.json").read_text(encoding="utf-8"))
        self.chars = vocab["characters"]

    def _grid(self, height, width):
        """Largest scale where the crop fits in 324 patches of 14 x 14 px -> (scale, rows, columns)."""
        lo, hi = 1e-4, 1000.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if math.ceil(height * mid / self.PATCH) * math.ceil(width * mid / self.PATCH) <= self.BUDGET:
                lo = mid
            else:
                hi = mid
        return lo, math.ceil(height * lo / self.PATCH), math.ceil(width * lo / self.PATCH)

    def _inputs(self, crop: Image.Image) -> dict:
        s, gh, gw = self._grid(crop.height, crop.width)
        rw = max(1, min(gw * self.PATCH, round(crop.width * s)))
        rh = max(1, min(gh * self.PATCH, round(crop.height * s)))
        small = np.asarray(crop.convert("RGB").resize((rw, rh), Image.Resampling.BICUBIC), np.uint8)
        border = np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]])
        canvas = np.empty((gh * self.PATCH, gw * self.PATCH, 3), np.uint8)
        canvas[:] = np.median(border, axis=0).astype(np.uint8)
        canvas[:rh, :rw] = small
        arr = (canvas.astype(np.float32) / 255.0 - self.MEAN) / self.STD
        n = gh * gw
        P = np.zeros((self.BUDGET, self.FEAT), np.float32)
        P[:n] = arr.reshape(gh, self.PATCH, gw, self.PATCH, 3).transpose(0, 2, 4, 1, 3).reshape(n, self.FEAT)
        V = np.zeros(self.BUDGET, np.float32)
        V[:n] = 1
        R = np.zeros(self.BUDGET, np.int32)
        C = np.zeros(self.BUDGET, np.int32)
        idx = np.arange(n)
        R[:n], C[:n] = idx // gw, idx % gw
        return {"P": P, "V": V, "R": R, "C": C,
                "H": np.array([gh], np.int32), "W": np.array([gw], np.int32)}

    def __call__(self, image: Image.Image, boxes) -> list[str]:
        texts = []
        for b in boxes:
            x1, y1, x2, y2 = (int(round(v)) for v in b[:4])
            if x2 - x1 < 2 or y2 - y1 < 2:
                texts.append("")
                continue
            ids = self.sess.run(None, self._inputs(image.crop((x1, y1, x2, y2))))[0].reshape(-1).tolist()
            eos = len(self.chars)
            ids = ids[: ids.index(eos)] if eos in ids else ids
            texts.append("".join(self.chars[i] for i in ids if 0 <= i < eos))
        return texts


# --- web server -----------------------------------------------------------------------------------

class App:
    def __init__(self, cpu: bool):
        print("Loading the text position model...", flush=True)
        self.detector = Detector(cpu)
        print("Loading the OCR model...", flush=True)
        self.ocr = Ocr(cpu)
        self.lock = threading.Lock()  # one page at a time keeps GPU memory low
        self.info = {"ok": True, "name": "manga-vision", "version": VERSION,
                     "device": device_name(self.detector.sess),
                     "detector": f"{DETECTOR_REPO}/{DETECTOR_FILE}", "ocr": OCR_REPO, "language": "ja"}


def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = f"manga-vision/{VERSION}"

    def log_message(self, fmt, *args):  # quiet: we print our own short lines
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/health"):
            return self._send(200, self.app.info)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/detect", "/ocr", "/page"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "send one page image (at most 40 MB)"})
        data = self.rfile.read(length)
        started = time.perf_counter()
        try:
            if path == "/ocr":
                req = json.loads(data)
                image = open_image(base64.b64decode(req["image"]))
                boxes = req["boxes"]
            else:
                image = open_image(data)
            with self.app.lock:
                if path == "/ocr":
                    result = {"texts": self.app.ocr(image, boxes)}
                else:
                    boxes = self.app.detector(image)
                    result = {"width": image.width, "height": image.height, "boxes": boxes}
                    if path == "/page":
                        texts = self.app.ocr(image, boxes)
                        result = {"width": image.width, "height": image.height,
                                  "blocks": [{"box": b, "text": t} for b, t in zip(boxes, texts)]}
        except Exception as e:  # a bad request must never stop the server
            print(f"  {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        ms = (time.perf_counter() - started) * 1000
        n = len(result.get("boxes") or result.get("blocks") or result.get("texts") or [])
        print(f"  {path}: {image.width}x{image.height}, {n} text boxes, {ms:.0f} ms", flush=True)
        self._send(200, result)


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv4 and IPv6 together. With IPv4 only, "localhost" on Windows first tries IPv6
    and every request waits about 2 seconds."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(host: str, port: int):
    if host in ("", "0.0.0.0", "::") and socket.has_ipv6:
        try:
            return DualStackServer(("::", port), Handler)
        except OSError:
            pass
    return ThreadingHTTPServer((host, port), Handler)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))  # no packet is sent; this only picks the network card
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="MangaVibe vision server (text position + Japanese OCR)")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: the whole network)")
    ap.add_argument("--port", type=int, default=7865)
    ap.add_argument("--cpu", action="store_true", help="don't use the GPU")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a console that can't show Japanese must not crash us
        stream.reconfigure(errors="replace")

    Handler.app = App(args.cpu)
    server = make_server(args.host, args.port)
    print(f"\nManga vision server {VERSION} is running on {Handler.app.info['device']}.")
    print(f"In MangaVibe, use this address:  http://{lan_ip()}:{args.port}")
    print("Leave this window open. Press Ctrl+C to stop.\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
