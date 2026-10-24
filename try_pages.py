# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Try the translation on a few pages before using it in MangaVibe.

    uv run try_pages.py PAGE_OR_FOLDER [...] [--vision URL] [--llm URL] [--model NAME]

Sends each page to the vision server and the text to the LLM, then writes report.html next to
the first page: every page with the English drawn over it, and a table of Japanese -> English.
Pages are treated as one chapter in file-name order, so each page gets the one before as context.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp"}


def post(url: str, body: bytes, content_type: str, timeout=120) -> dict:
    req = urllib.request.Request(url, body, {"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chat(llm: str, model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "stream": False}).encode()
    return post(llm.rstrip("/") + "/v1/chat/completions", body, "application/json")["choices"][0]["message"]["content"]


def translate_page(llm, model, lines, previous):
    """All bubbles of a page in one request, each wrapped in <bN></bN> (Hy-MT2's "keep the
    delimiters" prompt). Tried twice; then one request per bubble, which is worse because the model
    loses the context."""
    if not lines:
        return [], "empty"
    prompt = ""
    if previous:
        prompt += "[Background Information]\n" + "\n".join(previous) + "\n\n"
    prompt += ("Please accurately translate the following text into English"
               + (", taking the provided background information into consideration" if previous else "")
               + ".\nYou must retain the exact same number of delimiters in the translation.\n"
               "Strictly do not omit, escape, or translate these symbols, and pay close attention to their placement.\n\n"
               + ("[Source Text]\n" if previous else "")
               + "".join(f"<b{i}>{t}</b{i}>" for i, t in enumerate(lines, 1)))
    for _ in range(2):
        got = dict(re.findall(r"<b(\d+)>(.*?)</b\1>", chat(llm, model, prompt), re.S))
        if sorted(map(int, got)) == list(range(1, len(lines) + 1)):
            return [got[str(i)].strip() for i in range(1, len(lines) + 1)], "page"
    out = []
    for t in lines:
        out.append(chat(llm, model, "Translate the following text into English.\nNote that you should only "
                                   "output the translated result without any additional explanation:\n\n" + t).strip())
    return out, "line by line"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--vision", default="http://localhost:7865")
    ap.add_argument("--llm", default="http://localhost:11434")
    ap.add_argument("--model", default="manga-translate")
    ap.add_argument("--out", help="where to write the report (default: report.html next to the pages)")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    files = []
    for p in map(Path, args.pages):
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_TYPES) if p.is_dir() else [p]
    if not files:
        sys.exit("No images found.")
    out = Path(args.out) if args.out else files[0].parent / "report.html"

    sections, previous, total = [], [], time.perf_counter()
    for f in files:
        t0 = time.perf_counter()
        page = post(args.vision.rstrip("/") + "/page", f.read_bytes(), "application/octet-stream")
        t1 = time.perf_counter()
        lines = [b["text"] for b in page["blocks"]]
        english, how = translate_page(args.llm, args.model, lines, previous)
        t2 = time.perf_counter()
        previous = lines[-8:]
        print(f"{f.name}: {len(lines)} bubbles, vision {1000 * (t1 - t0):.0f} ms, "
              f"translation {1000 * (t2 - t1):.0f} ms ({how})")

        w, h = page["width"], page["height"]
        boxes, rows = [], []
        for i, (b, ja, en) in enumerate(zip(page["blocks"], lines, english), 1):
            x1, y1, x2, y2 = b["box"][:4]
            style = (f"left:{100 * x1 / w:.2f}%;top:{100 * y1 / h:.2f}%;"
                     f"width:{100 * (x2 - x1) / w:.2f}%;height:{100 * (y2 - y1) / h:.2f}%")
            boxes.append(f'<div class="box" style="{style}" title="{html.escape(ja)}">'
                         f'<span>{html.escape(en)}</span></div>')
            rows.append(f"<tr><td>{i}</td><td lang=ja>{html.escape(ja)}</td><td>{html.escape(en)}</td>"
                        f"<td>{b['box'][4]:.2f}</td></tr>")
        src = html.escape(f.resolve().as_uri())
        sections.append(f"""<section><h2>{html.escape(f.name)} <small>{len(lines)} bubbles · vision
{1000 * (t1 - t0):.0f} ms · translation {1000 * (t2 - t1):.0f} ms ({how})</small></h2>
<div class="pair"><div class="page"><img src="{src}">{''.join(boxes)}</div>
<div class="page"><img src="{src}"></div></div>
<table><tr><th>#</th><th>Japanese (OCR)</th><th>English</th><th>score</th></tr>{''.join(rows)}</table></section>""")

    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>Translation test</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#222;color:#eee}}
.pair{{display:flex;gap:12px;flex-wrap:wrap}} .page{{position:relative;max-width:min(48vw,700px)}}
.page img{{width:100%;display:block}}
.box{{position:absolute;background:#fff;border-radius:6px;display:flex;align-items:center;justify-content:center;
     text-align:center;color:#000;font:600 clamp(8px,1.1vw,15px)/1.15 "Comic Sans MS",system-ui,sans-serif;
     overflow:hidden;padding:2px;box-sizing:border-box;outline:2px solid #e44}}
table{{border-collapse:collapse;margin:12px 0 40px}} td,th{{border:1px solid #555;padding:4px 8px;text-align:left}}
small{{color:#aaa;font-weight:normal}}
</style><h1>Translation test · {html.escape(args.model)} · {time.perf_counter() - total:.1f} s</h1>
{''.join(sections)}""", encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
