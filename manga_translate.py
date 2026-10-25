"""Manga Translate: English over Japanese manga pages in MangaVibe (plugin API 3).

Copy this file into MangaVibe's plugins folder, then Settings -> Plugins -> Reload plugins.
The work happens on a PC with a GPU (see README.md):

  - the vision server (manga_vision_server.py) finds the text on a page and reads it
  - an LLM server with an OpenAI-style API (Ollama, LM Studio, llama.cpp) translates it

How it works: while you read with "Translation" switched on, the reader asks for each page. A page
that isn't translated yet goes to the front of the queue, with the next few pages behind it. Two
threads work through the queue: one sends pages to the vision server, the other sends the text to
the LLM, so both run at the same time. Results are saved per page in MangaVibe's database.

Two ways to translate (the "Translation" setting):
  - page by page: each page on its own, with the previous page's Japanese as background. Fast, and
    works with the small manga-translate model.
  - full context: each page is sent with the story around it: the series' description, notes on the
    characters and the story so far, the two pages before (with who said what) and the page after.
    The model also says who speaks each bubble. When a chapter is done, the model updates the notes,
    and the next chapter starts from them. The notes are saved with the chapter's last page.
"""
import base64
import heapq
import itertools
import json
import queue
import re
import threading
import time
from collections import OrderedDict

import httpx

from app.models import Book, Series
from app.plugins import Action, Box, Overlay, Plugin, SettingDef

# MangaVibe with plugin API 4 shows Overlay.note in the reader and keeps data per series; older
# versions don't, and then the notes are saved with each chapter's last page instead
HAS_NOTE = "note" in getattr(Overlay, "__dataclass_fields__", {})

VERSION = "0.3.0"
DATA_VERSION = 1          # the format of what is saved per page
SEND_WIDTH = 1600         # pages are shrunk to this width before they go to the PC
OFFLINE_RETRY = 15        # seconds between tries while the PC can't be reached
PAGE_TRIES = 2            # a page that fails this often is saved as failed (not tried forever)
OCR_KEEP = 400            # pages whose read text is kept in memory (full context reads neighbours)
PAGES_BEFORE = 2          # full context: translated pages sent before the page
MAX_CHARACTERS = 30       # full context: characters kept in the notes

# queue priorities: lower goes first
NOW, AHEAD, CHAPTER, SERIES = 0, 1, 2, 3


class MangaTranslate(Plugin):
    id = "manga-translate"
    name = "Manga Translate"
    version = VERSION
    description = ("Shows English over Japanese manga pages. The text is found, read and translated "
                   "by small models on your own PC (see the plugin's README).")
    repo = "ivokolarov0/manga-vibe-ext-translate"
    permissions = ["Reads page images", "Sends page images and text to the servers you set (your PC)"]
    overlay_label = "Translation"
    settings_fields = [
        SettingDef("vision_url", "Vision server",
                   help="Address the vision server prints when it starts, e.g. http://192.168.1.50:7865"),
        SettingDef("ocr_url", "OCR server (optional)",
                   help="Only if the OCR runs somewhere else than the text finder. Empty: the vision server."),
        SettingDef("llm_url", "LLM server",
                   help="Ollama: http://<PC address>:11434. LM Studio: http://<PC address>:1234"),
        SettingDef("model", "Model", default="manga-translate",
                   help="Name of the model in Ollama / LM Studio"),
        SettingDef("mode", "Translation", default="page",
                   options=[("page", "Page by page (fast, every model)"),
                            ("context", "Full context (knows the story and who speaks)")],
                   help="Full context sends each page with the pages around it and notes on the characters "
                        "and story, kept from chapter to chapter. Needs a model like gemma4: the small "
                        "manga-translate model can only do page by page."),
        SettingDef("show_page", "Show the page to the model", default="off",
                   options=[("off", "Off (text only)"), ("on", "On (better guesses of who speaks)")],
                   help="Full context only: the page picture goes with the text, each bubble marked with its "
                        "number, so the model sees who each bubble belongs to. Needs a model that can see "
                        "pictures, like gemma4. A little slower."),
        SettingDef("target", "Translate into", default="English"),
        SettingDef("ahead", "Pages to translate ahead", default="8",
                   help="Pages after the one you're reading that are translated in advance"),
        SettingDef("glossary", "Names and terms",
                   help="Fixed translations, separated by ;  e.g.  先輩=senpai; 春香=Haruka"),
        SettingDef("box_bg", "Box background", default="100",
                   options=[("100", "Solid white"), ("75", "A little see-through"),
                            ("50", "Half see-through"), ("25", "Mostly see-through"),
                            ("0", "None (only the text)")],
                   help="How much of the page shows through the English boxes"),
        SettingDef("outline", "White outline around the text", default="off",
                   options=[("off", "Off"), ("on", "On")],
                   help="Keeps the English readable over the art when the box is see-through"),
    ]
    global_actions = [Action("test", "Test connection"), Action("stop", "Stop the queue")]
    actions = [Action("series", "Translate the whole series"),
               Action("forget", "Forget this series' translations",
                      confirm="Delete the saved translations of every book in this series?")]
    book_actions = [Action("chapter", "Translate this chapter"),
                    Action("again", "Translate this chapter again")]
    css = """
.ov-box.ov-manga-translate { border-radius: 14px; box-shadow: 0 0 0 2px #fff; }
.ov-box.mt-bg-75 { background: rgba(255, 255, 255, .75); box-shadow: 0 0 0 2px rgba(255, 255, 255, .75); }
.ov-box.mt-bg-50 { background: rgba(255, 255, 255, .5); box-shadow: 0 0 0 2px rgba(255, 255, 255, .5); }
.ov-box.mt-bg-25 { background: rgba(255, 255, 255, .25); box-shadow: 0 0 0 2px rgba(255, 255, 255, .25); }
.ov-box.mt-bg-0 { background: transparent; box-shadow: none; }
.ov-box.mt-outline span {
  -webkit-text-stroke: .16em #fff; paint-order: stroke fill;  /* stroke drawn under the letters */
  text-shadow: 0 0 .12em #fff, 0 0 .12em #fff;                /* halo where paint-order isn't supported */
}
.ov-box.mt-failed { background: rgba(0, 0, 0, .55); color: #fff; border-radius: 6px; }
"""

    def __init__(self):
        self.lock = threading.Lock()
        self.heap = []                      # (priority, order, book_id, page)
        self.best = {}                      # (book_id, page) -> best priority queued
        self.busy = set()                   # (book_id, page) taken by a worker, not saved yet
        self.order = itertools.count()
        self.handoff = queue.Queue(maxsize=2)  # vision results waiting for the LLM
        self.threads = {}                   # "vision" / "llm" -> thread
        self.offline = ""                   # why the PC can't be reached, shown in the reader
        self.last = ""                      # last thing done, for the Settings status line
        self.tries = {}                     # (book_id, page) -> failed attempts
        self.ocr = OrderedDict()            # (book_id, page) -> vision result, newest last
        self.ocr_lock = threading.Lock()

    def ocr_get(self, key):
        with self.ocr_lock:
            return self.ocr.get(key)

    def ocr_put(self, key, result):
        with self.ocr_lock:
            self.ocr[key] = result
            self.ocr.move_to_end(key)
            while len(self.ocr) > OCR_KEEP:
                self.ocr.popitem(last=False)

    # --- what MangaVibe asks ------------------------------------------------------------------
    def page_overlay(self, ctx, book, page):
        saved = ctx.page_data(book)
        data = saved.get(page)
        if not ctx.setting("vision_url") or not ctx.setting("llm_url"):
            return Overlay(message="Manga Translate: set the server addresses in Settings → Plugins")
        if data is None:
            self.enqueue(ctx, book.id, [page], NOW)
        ahead = _int(ctx.setting("ahead"), 8)
        self.enqueue(ctx, book.id, [p for p in range(page + 1, min(page + 1 + ahead, book.page_count))
                                    if p not in saved], AHEAD)
        if data is None:
            busy = "Translating with full context…" if full_context(ctx) else "Translating…"
            return Overlay(pending=True, message=self.offline or busy)
        if data.get("error"):
            return Overlay(message=f"Couldn't translate this page: {data['error']}")
        boxes = boxes_for(data, box_classes(ctx))
        if HAS_NOTE:
            return Overlay(boxes=boxes, note=page_note(data))
        return Overlay(boxes=boxes)

    def book_badges(self, ctx, books):
        done = ctx.page_data_counts(books)
        out, per_series = {}, {}
        for b in books:
            n = done.get(b.id, 0)
            if n >= b.page_count:
                if hasattr(ctx, "series_data") and b.series_id not in per_series:
                    per_series[b.series_id] = ctx.series_data(b.series_id)  # one query per series
                full = (chapter_notes(ctx, b, per_series.get(b.series_id)) or {}).get("full")
                out[b.id] = "Translated · full context" if full else "Translated"
            elif n:
                out[b.id] = f"Translated {n}/{b.page_count}"
        return out

    def run_book_action(self, ctx, key, book):
        if key == "again":
            ctx.clear_page_data(book)
            save_notes(ctx, book, None)
        saved = ctx.page_data(book)
        todo = [p for p in range(book.page_count) if p not in saved]
        self.enqueue(ctx, book.id, todo, CHAPTER)
        return f"Translating {len(todo)} pages" if todo else "This chapter is already translated"

    def run_action(self, ctx, key, series):
        books = (ctx.db.query(Book).filter(Book.series_id == series.id, Book.hidden.is_(False),
                                           Book.missing_since.is_(None), Book.error.is_(None))
                 .order_by(Book.sort_index).all())
        if key == "forget":
            for b in books:
                ctx.clear_page_data(b)
            if hasattr(ctx, "clear_series_data"):
                ctx.clear_series_data(series)
            return "Translations deleted"
        done = ctx.page_data_counts(books)
        count = 0
        for b in books:
            if done.get(b.id, 0) < b.page_count:
                saved = ctx.page_data(b)
                todo = [p for p in range(b.page_count) if p not in saved]
                self.enqueue(ctx, b.id, todo, SERIES)
                count += len(todo)
        return f"Translating {count} pages" if count else "The whole series is already translated"

    def run_global_action(self, ctx, key):
        if key == "stop":
            with self.lock:
                n = len(self.best)
                self.heap.clear()
                self.best.clear()
            return f"Removed {n} pages from the queue"
        return test_connection(ctx)

    def status(self, ctx):
        with self.lock:
            waiting = len(self.best)
        parts = [f"Queue: {waiting} pages" if waiting else "Queue empty"]
        if self.offline:
            parts.append(self.offline)
        elif self.last:
            parts.append(self.last)
        return " · ".join(parts)

    # --- the queue ------------------------------------------------------------------------------
    def enqueue(self, ctx, book_id, pages, priority):
        if not pages:
            return
        with self.lock:
            for p in pages:
                key = (book_id, p)
                if key in self.busy or self.best.get(key, 99) <= priority:
                    continue
                self.best[key] = priority
                # the newest request of the page you're reading goes first; the rest in page order
                order = -next(self.order) if priority == NOW else next(self.order)
                heapq.heappush(self.heap, (priority, order, book_id, p))
            self.start_workers(ctx)

    def start_workers(self, ctx):
        """Start the two workers that aren't running (call with self.lock held). When both were
        stopped (plugin switched off and on), pages they held are put back in the queue."""
        alive = {name: t for name, t in self.threads.items() if t.is_alive()}
        if not alive:
            for key in self.busy:
                self.best[key] = NOW
                heapq.heappush(self.heap, (NOW, next(self.order), *key))
            self.busy.clear()
            self.handoff = queue.Queue(maxsize=2)
        for name, fn in (("vision", self.vision_worker), ("llm", self.llm_worker)):
            if name not in alive:
                alive[name] = ctx.run_in_thread(fn)
        self.threads = alive

    def take(self):
        """The next page to work on, or None."""
        with self.lock:
            while self.heap:
                priority, _, book_id, page = heapq.heappop(self.heap)
                key = (book_id, page)
                if self.best.get(key) != priority:
                    continue  # stale: queued again with a better priority, or the queue was stopped
                del self.best[key]
                self.busy.add(key)
                return key
        return None

    def release(self, key, again=False):
        with self.lock:
            self.busy.discard(key)
            if again and key not in self.best:  # put it back in front (the PC was away)
                self.best[key] = NOW
                heapq.heappush(self.heap, (NOW, -next(self.order), *key))

    def failed(self, ctx, book, page, key, error):
        """A page went wrong. Try it again later, or save it as failed after PAGE_TRIES."""
        self.tries[key] = self.tries.get(key, 0) + 1
        ctx.log.warning("Page %s of %s failed: %s", page + 1, book.file_name, error)
        if self.tries[key] >= PAGE_TRIES:
            ctx.set_page_data(book, page, {"v": DATA_VERSION, "error": str(error)[:200]})
            ctx.db.commit()
            self.tries.pop(key, None)
            self.release(key)
        else:
            self.release(key, again=True)

    # --- worker 1: page -> text boxes (vision server) ---------------------------------------------
    def vision_worker(self, ctx):
        while not ctx.should_stop():
            key = self.take()
            if key is None:
                time.sleep(0.3)
                continue
            book_id, page = key
            ctx.db.expire_all()
            book = ctx.db.get(Book, book_id)
            if book is None or ctx.get_page_data(book, page) is not None:
                self.release(key)
                continue
            try:
                result = self.read_page_text(ctx, book, page)
                picture = None
                if full_context(ctx):
                    self.read_neighbours(ctx, book, page)
                    if show_page(ctx):
                        picture = ctx.read_page(book, page, max_width=SEND_WIDTH)  # what the boxes fit
            except httpx.TransportError as e:
                self.went_offline(ctx, "vision server", ctx.setting("vision_url"), e)
                self.release(key, again=True)
                continue
            except Exception as e:
                self.failed(ctx, book, page, key, e)
                continue
            self.offline = ""
            while not ctx.should_stop():  # wait for room: the LLM is behind
                try:
                    self.handoff.put((key, result, picture), timeout=1)
                    break
                except queue.Full:
                    pass

    def read_page_text(self, ctx, book, page):
        """The vision result of one page: from memory when a neighbour's turn already read it."""
        key = (book.id, page)
        result = self.ocr_get(key)
        if result is None:
            result = read_text(ctx, ctx.read_page(book, page, max_width=SEND_WIDTH))
            self.ocr_put(key, result)
        return result

    def read_neighbours(self, ctx, book, page):
        """Full context: also read the pages around this one, so its translation can see them.
        Pages already translated aren't needed (their saved text is used); a page that can't be
        read is left out (its own turn will report it)."""
        for p in range(page - PAGES_BEFORE, page + 2):
            if p == page or not 0 <= p < book.page_count or self.ocr_get((book.id, p)) is not None:
                continue
            if ctx.get_page_data(book, p) is not None:
                continue
            try:
                self.read_page_text(ctx, book, p)
            except httpx.TransportError:
                raise
            except Exception as e:
                ctx.log.info("Couldn't read page %s of %s for context: %s", p + 1, book.file_name, e)

    # --- worker 2: text -> translation (LLM) -------------------------------------------------------
    def llm_worker(self, ctx):
        while not ctx.should_stop():
            try:
                key, result, picture = self.handoff.get(timeout=1)
            except queue.Empty:
                continue
            book_id, page = key
            ctx.db.expire_all()
            book = ctx.db.get(Book, book_id)
            if book is None:
                self.release(key)
                continue
            while not ctx.should_stop():
                try:
                    data = translate_page(ctx, book, page, result, self.ocr_get, picture)
                    break
                except httpx.TransportError as e:
                    self.went_offline(ctx, "LLM server", ctx.setting("llm_url"), e)
                    _sleep(ctx, OFFLINE_RETRY)
                except Exception as e:
                    data = None
                    self.failed(ctx, book, page, key, e)
                    break
            else:
                self.release(key, again=True)
                return
            if data is None:
                continue
            self.offline = ""
            ctx.set_page_data(book, page, data)
            ctx.db.commit()  # the reader shows it at its next look
            self.tries.pop(key, None)
            self.release(key)
            self.last = f"last: page {page + 1} of {book.file_name}"
            if full_context(ctx) and can_do_context(ctx):
                self.finish_chapter(ctx, book)

    def finish_chapter(self, ctx, book):
        """Full context: once every page of the chapter is saved (the page just saved was the last
        one missing), the model updates the notes on the characters and the story for the next
        chapter. If that fails, the earlier notes are carried over, so the next chapter still has them."""
        saved = ctx.page_data(book)
        if len(saved) < book.page_count:
            return
        pages = [saved[p] for p in sorted(saved) if not saved[p].get("error")]
        notes = previous_notes(ctx, book)
        try:
            new = update_notes(ctx, series_info(ctx.db.get(Series, book.series_id)), notes,
                               chapter_names(pages), transcript(pages), chapter_looks(pages))
        except Exception as e:
            ctx.log.warning("Couldn't update the notes after %s: %s", book.file_name, e)
            new = None
        new = new or {"characters": notes.get("characters", []), "summary": notes.get("summary", "")}
        new["full"] = all(d.get("mode") == "context" for d in pages)
        save_notes(ctx, book, new)
        ctx.db.commit()
        self.last = f"last: notes after {book.file_name}"

    def went_offline(self, ctx, what, url, error):
        msg = f"Can't reach the {what} ({url})"
        if self.offline != msg:
            ctx.log.warning("%s: %s", msg, error)
        self.offline = msg
        _sleep(ctx, OFFLINE_RETRY)


# --- talking to the PC ------------------------------------------------------------------------------

def _int(value, default):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _sleep(ctx, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not ctx.should_stop():
        time.sleep(0.5)


def _url(value):
    value = str(value or "").strip().rstrip("/")
    if value and not re.match(r"^https?://", value):
        value = "http://" + value
    return value


def llm_base(ctx):
    """The OpenAI-style base address: accepts http://pc:11434 as well as http://pc:11434/v1."""
    base = _url(ctx.setting("llm_url"))
    return base if base.endswith("/v1") else base + "/v1"


def read_text(ctx, image: bytes) -> dict:
    """Page image -> {"width", "height", "blocks": [{"box": [x1, y1, x2, y2, score], "text"}]}."""
    vision = _url(ctx.setting("vision_url"))
    ocr = _url(ctx.setting("ocr_url")) or vision
    with httpx.Client(timeout=120) as http:
        if ocr == vision:  # one server does both: one call
            r = http.post(vision + "/page", content=image)
            r.raise_for_status()
            return r.json()
        r = http.post(vision + "/detect", content=image)
        r.raise_for_status()
        found = r.json()
        texts = []
        if found["boxes"]:
            r = http.post(ocr + "/ocr", json={"image": base64.b64encode(image).decode(),
                                             "boxes": found["boxes"]})
            r.raise_for_status()
            texts = r.json()["texts"]
        return {"width": found["width"], "height": found["height"],
                "blocks": [{"box": b, "text": t} for b, t in zip(found["boxes"], texts)]}


_NO_JSON_MODE = set()  # LLM servers that refused response_format (asked once, then left out)


def chat(ctx, prompt: str, json_mode=False, picture: bytes | None = None) -> str:
    """One question to the LLM. `json_mode` asks for a JSON answer (lower temperature, and
    response_format where the server supports it). `picture` (JPEG) goes along for a model that can
    see pictures; a server that refuses it gets the question again without it."""
    base = llm_base(ctx)
    content = prompt
    if picture:
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(picture).decode()}}]
    body = {
        "model": ctx.setting("model") or "manga-translate",
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "reasoning_effort": "none",  # thinking models: answer straight away
    }
    if json_mode:
        body["temperature"] = 0.3
        if base not in _NO_JSON_MODE:
            body["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=180) as http:
        r = http.post(base + "/chat/completions", json=body)
        if r.status_code == 400 and "response_format" in body:  # e.g. LM Studio wants a schema
            _NO_JSON_MODE.add(base)
            del body["response_format"]
            r = http.post(base + "/chat/completions", json=body)
        if r.status_code >= 400 and picture:  # e.g. a model that can't see pictures
            ctx.log.warning("The LLM server refused the page picture (%s): asking without it", r.status_code)
            return chat(ctx, prompt, json_mode)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


def show_page(ctx):
    return ctx.setting("show_page") == "on" and can_do_context(ctx)


PICTURE_SIZE = 1280  # longest side of the page picture the model gets


def mark_bubbles(image: bytes, boxes) -> bytes | None:
    """The page as a JPEG for the model, each bubble in `boxes` (page pixels) framed in red with its
    number (1, 2, ...) in the corner, so the model can tell which line is which. None if the image
    can't be read."""
    import io

    from PIL import Image, ImageDraw, ImageFont
    try:
        with Image.open(io.BytesIO(image)) as im:
            im = im.convert("RGB")
    except Exception:
        return None
    scale = min(1.0, PICTURE_SIZE / max(im.size))
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(im)
    size = max(14, round(max(im.size) / 45))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1
        font = ImageFont.load_default()
    line = max(2, size // 8)
    for n, (x1, y1, x2, y2) in enumerate(((v * scale for v in b[:4]) for b in boxes), 1):
        draw.rectangle([x1, y1, x2, y2], outline=(230, 30, 30), width=line)
        label = str(n)
        w = draw.textlength(label, font=font)
        lx, ly = max(0, x1 - size * 0.4), max(0, y1 - size * 0.4)
        draw.rounded_rectangle([lx, ly, lx + w + size * 0.6, ly + size * 1.25], radius=size // 3, fill=(230, 30, 30))
        draw.text((lx + size * 0.3, ly + size * 0.1), label, font=font, fill="white")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def full_context(ctx):
    return ctx.setting("mode") == "context"


def can_do_context(ctx):
    """The small translation-only model can't answer the full-context questions (it translates
    whatever it gets); those go page by page."""
    return not re.search(r"manga-translate|hy-?mt", str(ctx.setting("model") or "manga-translate"), re.I)


def glossary(ctx):
    """The owner's fixed translations: [(japanese, english)]."""
    pairs = []
    for item in str(ctx.setting("glossary") or "").split(";"):
        if "=" in item:
            src, dst = (x.strip() for x in item.split("=", 1))
            if src and dst:
                pairs.append((src, dst))
    return pairs


def glossary_lines(ctx):
    return [f"{src} translates to {dst}" for src, dst in glossary(ctx)]


def translate_lines(ctx, lines, previous):
    """All bubbles of a page in one request, each wrapped in <bN></bN> (Hy-MT2's "keep the
    delimiters" prompt), with the previous page as background. Tried twice; then one request per
    bubble, which reads worse because the model loses the context."""
    target = ctx.setting("target") or "English"
    terms = glossary_lines(ctx)
    prompt = ""
    if terms:
        prompt += "Reference the following translations:\n" + "\n".join(terms) + "\n\n"
    if previous:
        prompt += "[Background Information]\n" + "\n".join(previous) + "\n\n"
    prompt += (f"Please accurately translate the following text into {target}"
               + (", taking the provided background information into consideration" if previous else "")
               + ".\nYou must retain the exact same number of delimiters in the translation.\n"
               "Strictly do not omit, escape, or translate these symbols, and pay close attention to "
               "their placement.\n\n" + ("[Source Text]\n" if previous else "")
               + "".join(f"<b{i}>{t}</b{i}>" for i, t in enumerate(lines, 1)))
    for _ in range(2):
        got = dict(re.findall(r"<b(\d+)>(.*?)</b\1>", chat(ctx, prompt), re.S))
        if sorted(map(int, got)) == list(range(1, len(lines) + 1)):
            return [got[str(i)].strip() for i in range(1, len(lines) + 1)]
    single = (f"Translate the following text into {target}.\nNote that you should only output the "
              "translated result without any additional explanation:\n\n")
    return [chat(ctx, single + t).strip() for t in lines]


def clean_ocr(text: str) -> str:
    """The OCR reads a vertical "…" as a colon (試合:? for 試合…?). Japanese dialogue hardly ever
    has a real colon, and a model takes "試合:" for a speaker's name, so it goes back to "…"."""
    return re.sub(r"[:：]+", "…", text.strip())


def translate_page(ctx, book, page, result, cached_ocr=lambda key: None, picture=None) -> dict:
    """A vision result -> the data saved for the page. `cached_ocr((book_id, page))` gives the read
    text of pages around it that aren't translated yet (full context). `picture`: the page image the
    vision result was read from, to show the model (full context with "Show the page")."""
    context = page_context(ctx, book, page, cached_ocr) if full_context(ctx) and can_do_context(ctx) else None
    previous = []
    if page > 0:
        before = ctx.get_page_data(book, page - 1) or {}
        previous = [b["src"] for b in before.get("blocks", [])][-8:]
    return translate_blocks(ctx, result, page + 1, previous, context, picture)


def translate_blocks(ctx, result, page_no, previous, context=None, picture=None) -> dict:
    """The translation itself (also used by try_pages.py). `previous`: the Japanese of the page
    before, for page by page. `context`: what page_context() returns, for full context; without
    it (or when the model doesn't answer in the right form) the page goes page by page.
    `picture`: with full context, the page image (the size the boxes were found on), shown to the
    model with each bubble framed and numbered."""
    blocks = [{**b, "text": clean_ocr(b["text"])} for b in result["blocks"] if b["text"].strip()]
    # bubbles with only punctuation ("…", "!?") stay as they are; the rest goes to the model
    todo = [i for i, b in enumerate(blocks) if re.search(r"\w", b["text"])]
    lines = [blocks[i]["text"] for i in todo]
    data = {"v": DATA_VERSION, "w": result["width"], "h": result["height"],
            "model": ctx.setting("model") or "manga-translate", "mode": "page"}
    got = None
    if context is not None:
        data["mode"] = "context"
        if lines:
            marked = mark_bubbles(picture, [blocks[i]["box"] for i in todo]) if picture else None
            got, names, looks, saw = translate_in_context(ctx, *context, page_no, lines, picture=marked)
            if got is None:  # the model didn't answer in the right form: page by page instead
                data["mode"] = "page"
            else:
                if names:
                    data["names"] = names
                if looks:
                    data["looks"] = looks
                if saw:
                    data["picture"] = True
    if got is None:
        got = [("", en) for en in translate_lines(ctx, lines, previous)] if lines else []
    rows = [("", b["text"]) for b in blocks]
    for i, row in zip(todo, got):
        rows[i] = row
    data["blocks"] = []
    for b, (who, en) in zip(blocks, rows):
        block = {"box": [round(v, 1) for v in b["box"][:4]], "src": b["text"], "text": en}
        if who:
            block["who"] = who
        data["blocks"].append(block)
    return data


# --- full context ------------------------------------------------------------------------------------

def series_info(series) -> str:
    """A few lines about the series from its metadata: the title and the description (which often
    has the characters' names in English)."""
    if series is None:
        return ""
    title = series.title_english or series.title_romaji or series.folder_name
    if series.title_native and series.title_native != title:
        title += f" ({series.title_native})"
    text = f"Series: {title}."
    desc = re.sub(r"<[^>]+>", " ", series.description or "")
    desc = re.sub(r"\(Source:[^)]*\)", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    if desc:
        text += "\nAbout it: " + (desc if len(desc) <= 700 else desc[:700].rsplit(" ", 1)[0] + "…")
    return text


def save_notes(ctx, book, notes):
    """Keeps the notes after a chapter (None forgets them): in the series' own data where MangaVibe
    has it (plugin API 4), else with the chapter's last page."""
    if hasattr(ctx, "set_series_data"):
        ctx.set_series_data(book.series_id, f"notes:{book.id}", notes)
        return
    if book.page_count:
        last = ctx.get_page_data(book, book.page_count - 1)
        if last is not None and not last.get("error"):
            last = {k: v for k, v in last.items() if k != "notes"}
            ctx.set_page_data(book, book.page_count - 1, {**last, "notes": notes} if notes else last)


def chapter_notes(ctx, book, series_data=None):
    """The notes saved after this chapter, or None. `series_data`: ctx.series_data() when the
    caller already has it."""
    if hasattr(ctx, "series_data"):
        data = series_data if series_data is not None else ctx.series_data(book.series_id)
        return data.get(f"notes:{book.id}")
    return (ctx.get_page_data(book, book.page_count - 1) or {}).get("notes") if book.page_count else None


def previous_notes(ctx, book) -> dict:
    """The notes saved after the nearest earlier chapter of the series, or {} for the first one."""
    earlier = (ctx.db.query(Book).filter(Book.series_id == book.series_id, Book.sort_index < book.sort_index,
                                         Book.hidden.is_(False), Book.missing_since.is_(None))
               .order_by(Book.sort_index.desc()).limit(50).all())
    data = ctx.series_data(book.series_id) if hasattr(ctx, "series_data") else None
    for b in earlier:
        notes = chapter_notes(ctx, b, data)
        if notes:
            return notes
    return {}


def page_note(data) -> str:
    """The line in the reader's top bar: how the page was translated, and who speaks on it."""
    if data.get("mode") != "context":
        return "Page by page"
    label = "Full context + picture" if data.get("picture") else "Full context"
    who = []
    for b in data.get("blocks", []):
        name = b.get("who", "")
        # names only: a description ("the boy with black hair") would make the line too long
        if name and len(name) <= 24 and name.lower() not in ("narration", "sound effect") and name not in who:
            who.append(name)
    if not who:
        return label
    return f"{label} · {', '.join(who[:4])}{'…' if len(who) > 4 else ''}"


def chapter_names(pages) -> list:
    """Names the model found on the chapter's pages so far: [[japanese, english]], first one wins."""
    seen = {}
    for d in pages:
        for ja, en in d.get("names") or []:
            seen.setdefault(ja, en)
    return [[ja, en] for ja, en in seen.items()]


def chapter_looks(pages) -> list:
    """How the characters look in the page pictures (full context with the picture), seen so far in
    the chapter: [[name, looks]]. A name seen with different looks keeps the one seen most often (one
    wrong match doesn't stick)."""
    seen = {}
    for d in pages:
        for name, looks in d.get("looks") or []:
            seen.setdefault(name, {}).setdefault(looks, 0)
            seen[name][looks] += 1
    return [[name, max(counts, key=counts.get)] for name, counts in seen.items()]


def page_rows(data) -> list:
    """A saved page as [(speaker, japanese, english)]."""
    return [(b.get("who", ""), b["src"], b["text"]) for b in data.get("blocks", [])]


def page_context(ctx, book, page, cached_ocr):
    """What the model gets next to the page: (series info, notes, names, looks, glossary, pages
    before, page after). A page before that isn't translated yet is sent in Japanese only."""
    saved = ctx.page_data(book)
    before = []
    for p in range(max(0, page - PAGES_BEFORE), page):
        if p in saved and not saved[p].get("error"):
            before.append((p + 1, page_rows(saved[p])))
        elif (r := cached_ocr((book.id, p))) is not None:
            before.append((p + 1, [("", b["text"], "") for b in r["blocks"] if b["text"].strip()]))
    after = []
    if page + 1 < book.page_count:
        r = saved.get(page + 1)
        if r and not r.get("error"):
            after = [b["src"] for b in r.get("blocks", [])]
        elif (r := cached_ocr((book.id, page + 1))) is not None:
            after = [b["text"] for b in r["blocks"] if b["text"].strip()]
    pages = [saved[p] for p in sorted(saved)]
    return (series_info(ctx.db.get(Series, book.series_id)), previous_notes(ctx, book), chapter_names(pages),
            chapter_looks(pages), glossary(ctx), before, after)


def notes_text(notes, names=(), looks=()) -> str:
    """The notes (and names and looks found in this chapter) as lines for a prompt; "" when there
    are none."""
    chars = [(c.get("ja", ""), c.get("en", ""), c.get("notes", ""), c.get("looks", ""))
             for c in notes.get("characters", []) if isinstance(c, dict) and c.get("en")]
    known = {ja for ja, _, _, _ in chars if ja} | {en for _, en, _, _ in chars}
    chars += [(ja, en, "", "") for ja, en in names if ja not in known and en not in known]
    seen = dict(looks)
    known |= {ja for ja, _ in names} | {en for _, en in names}
    chars += [("", name, "", desc) for name, desc in seen.items() if name not in known]
    out = []
    if chars:
        out.append("Characters:")
        for ja, en, note, desc in chars:
            desc = desc or seen.get(en, "")
            out.append(f"- {ja + ' = ' if ja else ''}{en}{': ' + note if note else ''}"
                       f"{' (looks: ' + desc + ')' if desc else ''}")
    if notes.get("summary"):
        out.append(f"Story so far: {notes['summary']}")
    return "\n".join(out)


def context_prompt(target, info, notes, names, looks, terms, before, after, page_no, lines, owners=None) -> str:
    """The full-context question for one page. `owners`: per line, how the person the bubble belongs
    to looks in the page picture ("" when no one), from bubble_owners()."""
    parts = [f"You are translating a Japanese manga into {target}, one page at a time, like a professional "
             f"scanlation translator: natural, in-character {target} that fits the scene."]
    if info:
        parts.append(info)
    if known := notes_text(notes, names, looks):
        parts.append("Notes from earlier pages:\n" + known)
    if terms:
        parts.append("Always translate these the same way:\n" + "\n".join(f"- {s} = {d}" for s, d in terms))
    after = [ja for ja in after if re.search(r"\w", ja)]  # bubbles of only "…" or "!?" say nothing here
    for no, rows in before:
        rows = [r for r in rows if re.search(r"\w", r[1])]
        if any(en for _, _, en in rows):
            parts.append(f"Page {no} (already translated):\n" + "\n".join(
                f"  {who or '?'}: 「{ja}」 -> \"{en}\"" for who, ja, en in rows))
        elif rows:
            parts.append(f"Page {no} (Japanese, only for context):\n" + "\n".join(f"  「{ja}」" for _, ja, _ in rows))
    if after:
        parts.append("After this page comes (only so you know where the scene goes; these lines are said "
                     "later, and people named in them may not be here yet):\n"
                     + "\n".join(f"  「{ja}」" for ja in after))
    seen = owners or [""] * len(lines)
    parts.append(f"Translate page {page_no}. Its text bubbles, in reading order (read by OCR, which may make "
                 "small mistakes: fix obvious ones from the context)"
                 + (", each with whom it belongs to in the page's picture" if owners else "") + ":\n"
                 + "\n".join(f"  {i}. 「{t}」" + (f"  (in the picture: {o})" if o else "")
                             for i, (t, o) in enumerate(zip(lines, seen), 1)))
    if owners:
        parts.append("Where a bubble says whom it belongs to in the picture, that decides the speaker, more than "
                     "anything in the text: give the character who looks like that (their looks in the notes, or "
                     "whether they're a girl or a boy, young or old). The same look means the same person.")
    parts.append("For each bubble, decide who most likely says it. Use what you know: who is in the scene, who "
                 "they talk to, how each character talks (polite or rough, who calls whom senpai or by name), "
                 "and that a conversation usually goes back and forth between the same people, also across "
                 "pages: the characters talking on the page before are most likely still talking, unless the "
                 "text shows someone else. Give the English name of a character from the notes or the pages "
                 "before whenever the line fits one of them; otherwise a short description of who it is. "
                 "Use \"narration\" or "
                 "\"sound effect\" where that's what it is, and \"?\" if you can't tell. If a character's name "
                 'appears that isn\'t in the notes, add it to "names".\n\n'
                 "Answer with JSON only, in this form:\n"
                 + ('{"lines": [{"n": 1, "seen": "whom it belongs to in the picture", "speaker": "...", '
                    '"en": "..."}], ' if owners else
                    '{"lines": [{"n": 1, "to": "who it is said to", "speaker": "...", "en": "..."}], ')
                 + '"names": [{"ja": "...", "en": "..."}]}')
    return "\n\n".join(parts)


def _json(text: str):
    """The JSON object in a model's answer (which may wrap it in ``` or add words around it)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


NO_SPEAKER = {"?", "unknown", "n/a", "none", "nobody", ""}


def parse_lines(text, count):
    """The model's answer -> ([(speaker, english) or None per line 1..count], [[ja, en]] new names);
    (None, []) when the answer isn't JSON in the asked form. A line the model left out is None."""
    data = _json(text)
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None, []
    got = {}
    for item in data["lines"]:
        try:
            n = int(item["n"])
        except (TypeError, ValueError, KeyError):
            continue
        en = str(item.get("en") or "").strip()
        if not en or not 1 <= n <= count:
            continue
        who = str(item.get("speaker") or "").strip()
        got[n] = ("" if who.lower() in NO_SPEAKER else who, en)
    names = []
    for item in data.get("names") or []:
        if isinstance(item, dict):
            ja, en = str(item.get("ja") or "").strip(), str(item.get("en") or "").strip()
            if ja and en and ja != en:
                names.append([ja, en])
    return [got.get(i) for i in range(1, count + 1)], names


def translate_in_context(ctx, info, notes, names, looks, terms, before, after, page_no, lines, picture=None):
    """One page with the story around it -> ([(speaker, english)], new names, new looks, whether the
    picture was used). Asked twice when the model leaves a line out; the best answer is kept and a
    line still missing is translated on its own. (None, [], [], False) when the model didn't answer
    with most of the lines. `picture`: the page with numbered bubbles (mark_bubbles): first the model
    is asked whom each bubble belongs to, and that goes into the question as text."""
    target = ctx.setting("target") or "English"
    owners = bubble_owners(ctx, picture, len(lines)) if picture else None
    prompt = context_prompt(target, info, notes, names, looks, terms, before, after, page_no, lines, owners)
    best, best_names = None, []
    for _ in range(2):
        rows, found = parse_lines(chat(ctx, prompt, json_mode=True), len(lines))
        if rows is not None and (best is None or rows.count(None) < best.count(None)):
            best, best_names = rows, found
        if best is not None and None not in best:
            break
    if best is None or best.count(None) > len(lines) / 2:
        return None, [], [], False
    if None in best:
        single = (f"Translate the following text into {target}.\nNote that you should only output the "
                  "translated result without any additional explanation:\n\n")
        best = [row or ("", chat(ctx, single + text).strip()) for row, text in zip(best, lines)]
    new_looks = []  # a character and how they looked here: kept, so later pages know them by sight
    if owners:
        characters = {c.get("en") for c in notes.get("characters", []) if isinstance(c, dict)}
        characters |= {en for _, en in names} | {en for _, en in best_names}
        for (who, _), desc in zip(best, owners):
            if who in characters and desc and [who, desc[:150]] not in new_looks:
                new_looks.append([who, desc[:150]])
    return best, best_names, new_looks, owners is not None


def bubble_owners(ctx, picture, count):
    """Shows the model the page with numbered bubbles and asks whom each one belongs to -> per bubble
    how that person looks ("" for no one), or None when the answer is no use. A separate, short
    question: in one big question the model goes by the text and hardly looks at the picture."""
    prompt = (f"This manga page has its text bubbles framed in red, numbered 1 to {count}. Go panel by panel. "
              "For each numbered bubble, find whom it belongs to: the person its tail points to, or else the "
              "person right next to it in the same panel. Describe that person by how they look (hair, "
              'clothes, face, girl or boy), or say "no one" for narration, a sound or someone off-panel.\n\n'
              "Answer with JSON only, in this form:\n"
              '{"bubbles": [{"n": 1, "panel": "which panel", "person": "how they look"}]}')
    data = _json(chat(ctx, prompt, json_mode=True, picture=picture))
    if not isinstance(data, dict) or not isinstance(data.get("bubbles"), list):
        return None
    out = [""] * count
    for item in data["bubbles"]:
        try:
            n = int(item["n"])
        except (TypeError, ValueError, KeyError):
            continue
        person = str(item.get("person") or "").strip()
        if 1 <= n <= count and person.lower() not in ("no one", "none", "nobody", "n/a", "?"):
            out[n - 1] = person
    return out if any(out) else None


def transcript(pages, limit=8000) -> str:
    """The chapter's lines for the notes, with the Japanese (it shows how each character talks).
    A long chapter keeps its start and end, which say the most about where the story goes."""
    lines = [f"{who or '?'}: 「{ja}」 -> \"{en}\"" for d in pages for who, ja, en in page_rows(d) if en]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    head, tail = text[:limit // 2], text[-limit // 2:]
    return head.rsplit("\n", 1)[0] + "\n[…]\n" + tail.split("\n", 1)[-1]


def update_notes(ctx, info, notes, names, chapter, looks=()) -> dict | None:
    """The notes after one more chapter: {"characters": [{"ja", "en", "notes", "looks"}], "summary"}."""
    prompt = ("You keep the notes a translator uses for a Japanese manga, so names, voices and the story "
              "stay consistent from chapter to chapter.\n\n" + (info + "\n\n" if info else "")
              + "The notes so far:\n" + (notes_text(notes, names, looks) or "(none yet: this is the first chapter)")
              + "\n\nThe chapter just finished (speaker: Japanese -> English; the speakers are guesses and "
              "may be wrong):\n" + (chapter or "(no text)")
              + "\n\nWrite the updated notes: every named character (Japanese name, English name, who they are, "
              "how they talk and relate to the others, and their looks if the notes give them; never guess "
              "looks), and the story so far in at most 150 words, covering everything up to now (not only "
              f"this chapter). At most {MAX_CHARACTERS} characters; drop minor ones first.\n\nAnswer with JSON only, in this form:\n"
              '{"characters": [{"ja": "...", "en": "...", "notes": "...", "looks": "..."}], "summary": "..."}')
    for _ in range(2):
        data = _json(chat(ctx, prompt, json_mode=True))
        if isinstance(data, dict) and isinstance(data.get("characters"), list):
            chars = [{"ja": str(c.get("ja") or "").strip(), "en": str(c.get("en") or "").strip(),
                      "notes": str(c.get("notes") or "").strip()[:200],
                      "looks": str(c.get("looks") or "").strip()[:150]}
                     for c in data["characters"] if isinstance(c, dict) and c.get("en")]
            return {"characters": chars[:MAX_CHARACTERS], "summary": str(data.get("summary") or "").strip()[:1500]}
    return None


def box_classes(ctx):
    """The look the owner picked in Settings, as CSS classes on each box (styled in `css` above)."""
    out = []
    bg = ctx.setting("box_bg") or "100"
    if bg in ("75", "50", "25", "0"):
        out.append(f"mt-bg-{bg}")
    if ctx.setting("outline") == "on":
        out.append("mt-outline")
    return " ".join(out)


GAP = 4  # pixels (of the saved page size) left between two grown boxes


def boxes_for(data, cls=""):
    """Saved page -> boxes for the reader. The text box is grown a little: English needs more room
    than a column of Japanese, and the bubble around it is bigger anyway. Tall thin boxes (vertical
    Japanese) are widened to at least 2/3 of their height, so English lines aren't one word each.
    A box never grows into a neighbour: where two would overlap, both grow less on the sides that
    face each other."""
    w, h = data["w"], data["h"]
    blocks = data.get("blocks", [])
    rects = [b["box"] for b in blocks]
    grow = []  # per box: how far each side moves out [left, top, right, bottom]
    for x1, y1, x2, y2 in rects:
        gx = max((x2 - x1) * 0.25, ((y2 - y1) * 0.66 - (x2 - x1)) / 2)
        gy = (y2 - y1) * 0.08
        grow.append([min(gx, x1), min(gy, y1), min(gx, w - x2), min(gy, h - y2)])
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            _make_room(rects[i], grow[i], rects[j], grow[j])
    out = []
    for b, (x1, y1, x2, y2), (gl, gt, gr, gb) in zip(blocks, rects, grow):
        x1, y1, x2, y2 = x1 - gl, y1 - gt, x2 + gr, y2 + gb
        title = f"{b['who']}: {b['src']}" if b.get("who") else b["src"]
        out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, text=b["text"], title=title, cls=cls))
    return out


def _make_room(a, ga, b, gb):
    """If boxes a and b would overlap once grown, cut the growth of the sides facing each other
    (both in proportion) on the axis where that takes the least away. Only ever shrinks, so pairs
    fixed earlier stay apart. Boxes whose text already overlaps are left alone."""
    best = None
    for lo, hi in ((0, 2), (1, 3)):  # x axis: left/right sides; y axis: top/bottom
        if a[hi] <= b[lo]:           # a before b on this axis
            gap, sa, sb = b[lo] - a[hi], hi, lo
        elif b[hi] <= a[lo]:         # b before a
            gap, sa, sb = a[lo] - b[hi], lo, hi
        else:
            continue                 # the text overlaps on this axis: growth on it can't be the cause
        need = ga[sa] + gb[sb] - max(0, gap - GAP)
        if need <= 0:
            return                   # apart on this axis even when grown
        if best is None or need < best[0]:
            best = (need, gap, sa, sb)
    if best is None:
        return
    _, gap, sa, sb = best
    room = max(0, gap - GAP)
    scale = room / (ga[sa] + gb[sb])
    ga[sa] *= scale
    gb[sb] *= scale


def test_connection(ctx) -> str:
    """Checks both servers and that the model exists. Returns a message for the owner."""
    found = []
    vision = _url(ctx.setting("vision_url"))
    if not vision:
        return "Set the vision server's address first"
    try:
        info = httpx.get(vision + "/health", timeout=5).json()
        found.append(f"Vision server OK ({info.get('device', '?')})")
    except Exception as e:
        return f"Can't reach the vision server at {vision}: {type(e).__name__}. Is it running, and allowed through the firewall?"
    if not ctx.setting("llm_url"):
        return found[0] + ". Now set the LLM server's address"
    model = ctx.setting("model") or "manga-translate"
    try:
        names = [m["id"] for m in httpx.get(llm_base(ctx) + "/models", timeout=5).json().get("data", [])]
    except Exception as e:
        return (f"{found[0]}, but can't reach the LLM server at {llm_base(ctx)}: {type(e).__name__}. "
                "For Ollama on another PC, set OLLAMA_HOST=0.0.0.0 there and restart it.")
    if model not in names and f"{model}:latest" not in names:
        return f"{found[0]}. LLM server OK, but it has no model called '{model}'. It has: {', '.join(names[:8])}"
    if full_context(ctx) and not can_do_context(ctx):
        return (f"{found[0]} · LLM server OK, model '{model}' found. Note: '{model}' can't do full context, "
                "so pages are translated page by page. For full context use a model like gemma4.")
    return f"{found[0]} · LLM server OK, model '{model}' found"
