# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "pillow"]
# ///
"""Try the translation on a few pages before using it in MangaVibe.

    uv run try_pages.py PAGE_OR_FOLDER [...] [--vision URL] [--llm URL] [--model NAME]
                        [--context] [--picture] [--notes FILE] [--threshold N]

Sends each page to the vision server and the text to the LLM, then writes report.html next to
the first page: every page with the English drawn over it, and a table of Japanese -> English.
Pages are treated as one chapter in file-name order.

  --context      full context, as the plugin's "Full context" setting: each page gets the pages
                 around it, and the model says who speaks. Needs a model like gemma4.
  --picture      with --context: the page picture goes along too, each bubble framed and numbered,
                 as the plugin's "Show the page to the model" setting. Needs a model that can see
                 pictures, like gemma4.
  --notes FILE   with --context: the notes on the characters and story to start from (if the file
                 exists), and where the updated notes are written afterwards. Run chapter after
                 chapter with the same file to see how the notes carry over.
  --threshold N  how sure the text finder must be, as the plugin's "Bubble detection" setting
                 (default: the server's, 0.3). Lower, like 0.2, finds more bubbles and more wrong boxes.

The translation is the plugin's own code (manga_translate.py, next to this file).
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import sys
import time
import types
import urllib.request
from pathlib import Path

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp"}


def load_plugin():
    """manga_translate.py without MangaVibe: the few MangaVibe names it imports are stand-ins."""
    class Anything:
        def __init__(self, *args, **kwargs):
            pass
    app, models, plugins = (types.ModuleType(n) for n in ("app", "app.models", "app.plugins"))
    models.Book = models.Series = object
    plugins.Plugin = object
    plugins.Action = plugins.Box = plugins.Overlay = plugins.SettingDef = Anything
    sys.modules.update({"app": app, "app.models": models, "app.plugins": plugins})
    spec = importlib.util.spec_from_file_location("manga_translate", Path(__file__).with_name("manga_translate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Settings:
    """Stands in for the plugin's ctx: only ctx.setting() is used by the translation."""
    def __init__(self, values):
        self.values = values

    def setting(self, key):
        return self.values.get(key)


def how(data) -> str:
    return data["mode"] + (" + picture" if data.get("picture") else "")


def read_page(vision: str, path: Path, threshold: float | None = None) -> dict:
    url = vision.rstrip("/") + "/page" + (f"?threshold={threshold}" if threshold else "")
    req = urllib.request.Request(url, path.read_bytes(),
                                 {"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--vision", default="http://localhost:7865")
    ap.add_argument("--llm", default="http://localhost:11434")
    ap.add_argument("--model", default="manga-translate")
    ap.add_argument("--context", action="store_true", help="full context (needs a model like gemma4)")
    ap.add_argument("--picture", action="store_true", help="with --context: show the page to the model")
    ap.add_argument("--notes", help="with --context: notes file to start from and to write")
    ap.add_argument("--threshold", type=float,
                    help="how sure the text finder must be, as the plugin's Bubble detection setting "
                         "(default: the server's, 0.3). Lower finds more bubbles")
    ap.add_argument("--out", help="where to write the report (default: report.html next to the pages)")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    mt = load_plugin()
    ctx = Settings({"llm_url": args.llm, "model": args.model, "target": "English",
                    "mode": "context" if args.context else "page"})
    if args.context and not mt.can_do_context(ctx):
        sys.exit(f"'{args.model}' can't do full context: use a model like gemma4 (--model gemma4).")

    files = []
    for p in map(Path, args.pages):
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_TYPES) if p.is_dir() else [p]
    if not files:
        sys.exit("No images found.")
    out = Path(args.out) if args.out else files[0].parent / "report.html"
    notes_file = Path(args.notes) if args.notes else None
    notes = json.loads(notes_file.read_text(encoding="utf-8")) if notes_file and notes_file.exists() else {}

    total = time.perf_counter()
    read, vision_ms = [], []
    for f in files:  # all pages first: full context looks at the next page too
        t0 = time.perf_counter()
        read.append(read_page(args.vision, f, args.threshold))
        vision_ms.append(1000 * (time.perf_counter() - t0))

    sections, done = [], []
    for i, (f, page) in enumerate(zip(files, read)):
        t0 = time.perf_counter()
        previous = [b["src"] for b in done[-1]["blocks"]][-8:] if done else []
        context = None
        if args.context:
            before = [(n + 1, mt.page_rows(done[n])) for n in range(max(0, i - mt.PAGES_BEFORE), i)]
            after = [mt.clean_ocr(b["text"]) for b in read[i + 1]["blocks"]] if i + 1 < len(read) else []
            context = ("", notes, mt.chapter_names(done), mt.chapter_looks(done), [], before, after)
        picture = f.read_bytes() if args.context and args.picture else None
        data = mt.translate_blocks(ctx, page, i + 1, previous, context, picture)
        done.append(data)
        llm_ms = 1000 * (time.perf_counter() - t0)
        print(f"{f.name}: {len(data['blocks'])} bubbles, vision {vision_ms[i]:.0f} ms, "
              f"translation {llm_ms:.0f} ms ({how(data)})")

        w, h = data["w"], data["h"]
        boxes, rows = [], []
        found = [b for b in page["blocks"] if b["text"].strip()]  # the ones translate_blocks keeps
        for n, (b, block) in enumerate(zip(found, data["blocks"]), 1):
            x1, y1, x2, y2 = block["box"]
            style = (f"left:{100 * x1 / w:.2f}%;top:{100 * y1 / h:.2f}%;"
                     f"width:{100 * (x2 - x1) / w:.2f}%;height:{100 * (y2 - y1) / h:.2f}%")
            boxes.append(f'<div class="box" style="{style}" title="{html.escape(block["src"])}">'
                         f'<span>{html.escape(block["text"])}</span></div>')
            rows.append(f"<tr><td>{n}</td><td>{html.escape(block.get('who', ''))}</td>"
                        f"<td lang=ja>{html.escape(block['src'])}</td><td>{html.escape(block['text'])}</td>"
                        f"<td>{b['box'][4]:.2f}</td></tr>")
        src = html.escape(f.resolve().as_uri())
        sections.append(f"""<section><h2>{html.escape(f.name)} <small>{len(data['blocks'])} bubbles · vision
{vision_ms[i]:.0f} ms · translation {llm_ms:.0f} ms ({how(data)})</small></h2>
<div class="pair"><div class="page"><img src="{src}">{''.join(boxes)}</div>
<div class="page"><img src="{src}"></div></div>
<table><tr><th>#</th><th>Speaker</th><th>Japanese (OCR)</th><th>English</th><th>score</th></tr>{''.join(rows)}</table></section>""")

    notes_html = ""
    if args.context:
        t0 = time.perf_counter()
        new = mt.update_notes(ctx, "", notes, mt.chapter_names(done), mt.transcript(done),
                              mt.chapter_looks(done))
        print(f"notes: {1000 * (time.perf_counter() - t0):.0f} ms" + ("" if new else " (the model didn't answer in the right form)"))
        if new:
            text = mt.notes_text(new)
            print("\n" + text)
            notes_html = f"<h2>Notes after these pages</h2><pre>{html.escape(text)}</pre>"
            if notes_file:
                notes_file.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"\nNotes saved: {notes_file}")

    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>Translation test</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#222;color:#eee}}
.pair{{display:flex;gap:12px;flex-wrap:wrap}} .page{{position:relative;max-width:min(48vw,700px)}}
.page img{{width:100%;display:block}}
.box{{position:absolute;background:#fff;border-radius:6px;display:flex;align-items:center;justify-content:center;
     text-align:center;color:#000;font:600 clamp(8px,1.1vw,15px)/1.15 "Comic Sans MS",system-ui,sans-serif;
     overflow:hidden;padding:2px;box-sizing:border-box;outline:2px solid #e44}}
table{{border-collapse:collapse;margin:12px 0 40px}} td,th{{border:1px solid #555;padding:4px 8px;text-align:left}}
small{{color:#aaa;font-weight:normal}} pre{{white-space:pre-wrap;max-width:900px}}
</style><h1>Translation test · {html.escape(args.model)}{' · full context' if args.context else ''}{' + picture' if args.context and args.picture else ''} ·
{time.perf_counter() - total:.1f} s</h1>
{''.join(sections)}{notes_html}""", encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
