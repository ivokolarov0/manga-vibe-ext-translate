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

The editor ("Edit translation" in the reader's options, MangaVibe 2.0.0 or newer) opens the page with
its boxes: change the text, move and resize boxes, style them, add your own, or mark text the finder
missed to have it read and translated. It's the plugin's own script and API (the end of this file);
what it saves is the same per-page data, with a fixed place ("rect") and a "style" per box.
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

try:
    from app.plugins import API_VERSION
except ImportError:  # MangaVibe before it had one, or try_pages.py
    API_VERSION = 0
HAS_EDITOR = API_VERSION >= 7  # a plugin's own API and script: MangaVibe 2.0.0
EDIT_LABEL = "Edit translation"  # the reader's button that opens the editor (the script looks for it)

VERSION = "0.6.0"
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
        SettingDef("threshold", "Bubble detection", default="normal",
                   options=[("normal", "Normal"),
                            ("0.2", "More (finds faint or odd-shaped bubbles)"),
                            ("0.1", "Most (also finds more wrong boxes)"),
                            ("0.45", "Fewer (only clear bubbles)")],
                   help="How sure the text finder must be before it marks text. If bubbles are missed, try "
                        "More, then in the reader Translate this chapter again. Remove wrong boxes with ×. "
                        "Normal uses the vision server's own setting (0.3 unless started with --threshold)."),
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
    book_actions = ([Action("edit", EDIT_LABEL)] if HAS_EDITOR else []) + [
        Action("chapter", "Translate this chapter"),
        Action("again", "Translate this chapter again")]
    # the editor's API, script and styles are added at the end of this file (router, js, css)
    css = """
.ov-box.ov-manga-translate { border-radius: 14px; box-shadow: 0 0 0 2px #fff; }
.ov-box.ov-manga-translate > span { white-space: pre-line; }  /* line breaks typed in the editor */
.ov-box.mt-bg-75 { background: rgba(255, 255, 255, .75); box-shadow: 0 0 0 2px rgba(255, 255, 255, .75); }
.ov-box.mt-bg-50 { background: rgba(255, 255, 255, .5); box-shadow: 0 0 0 2px rgba(255, 255, 255, .5); }
.ov-box.mt-bg-25 { background: rgba(255, 255, 255, .25); box-shadow: 0 0 0 2px rgba(255, 255, 255, .25); }
.ov-box.mt-bg-0 { background: transparent; box-shadow: none; }
.ov-box.mt-outline span {
  /* --mt-stroke: dark for boxes with light text (the editor's text colours) */
  -webkit-text-stroke: .16em var(--mt-stroke, #fff); paint-order: stroke fill;  /* stroke drawn under the letters */
  text-shadow: 0 0 .12em var(--mt-stroke, #fff), 0 0 .12em var(--mt-stroke, #fff);  /* halo where paint-order isn't supported */
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

    def ocr_forget(self, book_id):
        """Drops the remembered vision results of one book, so its pages are read again."""
        with self.ocr_lock:
            for key in [k for k in self.ocr if k[0] == book_id]:
                del self.ocr[key]

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
        if key == "edit":  # the plugin's script opens the editor instead; this only runs if it didn't
            return "The editor didn't open. Reload the page and try again."
        kept = 0
        if key == "again":
            # pages changed by hand in the editor stay: redoing them would throw that work away
            # (the editor's "Translate this page again" redoes one)
            for p, data in ctx.page_data(book).items():
                if data.get("edited"):
                    kept += 1
                else:
                    ctx.set_page_data(book, p, None)
            self.ocr_forget(book.id)  # a changed Bubble detection must take effect
            save_notes(ctx, book, None)
        saved = ctx.page_data(book)
        todo = [p for p in range(book.page_count) if p not in saved]
        self.enqueue(ctx, book.id, todo, CHAPTER)
        note = f" ({kept} you edited {'was' if kept == 1 else 'were'} kept)" if kept else ""
        return f"Translating {len(todo)} pages{note}" if todo else f"This chapter is already translated{note}"

    def run_action(self, ctx, key, series):
        books = (ctx.db.query(Book).filter(Book.series_id == series.id, Book.hidden.is_(False),
                                           Book.missing_since.is_(None), Book.error.is_(None))
                 .order_by(Book.sort_index).all())
        if key == "forget":
            for b in books:
                ctx.clear_page_data(b)
                self.ocr_forget(b.id)
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
            if (ctx.get_page_data(book, page) or {}).get("edited"):  # saved in the editor meanwhile: that wins
                self.release(key)
                continue
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


def detection_params(ctx) -> dict:
    """The Bubble detection setting as ?threshold=; nothing for Normal (the server decides).
    Vision servers older than 1.1.0 ignore it."""
    try:
        return {"threshold": str(min(max(float(ctx.setting("threshold")), 0.05), 0.95))}
    except (TypeError, ValueError):
        return {}


def read_text(ctx, image: bytes) -> dict:
    """Page image -> {"width", "height", "blocks": [{"box": [x1, y1, x2, y2, score], "text"}]}."""
    vision = _url(ctx.setting("vision_url"))
    ocr = _url(ctx.setting("ocr_url")) or vision
    params = detection_params(ctx)
    with httpx.Client(timeout=120) as http:
        if ocr == vision:  # one server does both: one call
            r = http.post(vision + "/page", content=image, params=params)
            r.raise_for_status()
            return r.json()
        r = http.post(vision + "/detect", content=image, params=params)
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
        previous = [b["src"] for b in before.get("blocks", []) if b["src"]][-8:]  # not the editor's own boxes
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
    face each other. A box placed in the editor ("rect") stays exactly there, with its "style" as
    CSS classes; one whose text was emptied there isn't drawn."""
    w, h = data["w"], data["h"]
    out = []
    for b, (x1, y1, x2, y2) in zip(data.get("blocks", []), display_rects(data)):
        if not b["text"]:
            continue
        who, src = b.get("who", ""), b["src"]
        title = f"{who}: {src}" if who and src else src or who
        classes = " ".join(c for c in (cls, style_classes(b.get("style"))) if c)
        out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, text=b["text"], title=title, cls=classes))
    return out


def display_rects(data) -> list:
    """Where each block's English box goes: [x1, y1, x2, y2] per block, in pixels of the saved page
    size (see boxes_for)."""
    w, h = data["w"], data["h"]
    blocks = data.get("blocks", [])
    rects = [b.get("rect") or b["box"] for b in blocks]
    grow = []  # per box: how far each side moves out [left, top, right, bottom]
    for b, (x1, y1, x2, y2) in zip(blocks, rects):
        if b.get("rect"):  # placed by hand
            grow.append([0, 0, 0, 0])
            continue
        gx = max((x2 - x1) * 0.25, ((y2 - y1) * 0.66 - (x2 - x1)) / 2)
        gy = (y2 - y1) * 0.08
        grow.append([min(gx, x1), min(gy, y1), min(gx, w - x2), min(gy, h - y2)])
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            _make_room(rects[i], grow[i], rects[j], grow[j])
    return [[x1 - gl, y1 - gt, x2 + gr, y2 + gb] for (x1, y1, x2, y2), (gl, gt, gr, gb) in zip(rects, grow)]


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


# --- the editor: box styles ---------------------------------------------------------------------------
# A box's "style" is saved with its block and reaches the reader as CSS classes (a Box only has
# `cls`): {"size": 24, "bold": True, "italic": True, "upper": True, "font", "align", "color", "bg"}.
# "size" is the font size in tenths of a percent of the page's width, so it's the same at every zoom;
# without it the reader fits the text to the box, as before.

SIZE_MIN, SIZE_MAX = 6, 80
FONTS = [("", "Comic (default)", ""),
         ("sans", "Plain", 'system-ui, "Segoe UI", Arial, sans-serif'),
         ("serif", "Serif", 'Georgia, "Times New Roman", serif'),
         ("hand", "Handwriting", '"Segoe Print", "Bradley Hand", "Comic Sans MS", cursive'),
         ("heavy", "Heavy", 'Impact, Haettenschweiler, "Arial Narrow Bold", sans-serif'),
         ("mono", "Typewriter", '"Courier New", ui-monospace, monospace')]
ALIGNS = [("", "Centre"), ("left", "Left"), ("right", "Right")]
COLORS = [("", "Black", "#111"), ("white", "White", "#fff"), ("red", "Red", "#c62828"),
          ("blue", "Blue", "#1e50c8"), ("green", "Green", "#1b7a3d"), ("purple", "Purple", "#7b2fbf"),
          ("orange", "Orange", "#c85a00"), ("gray", "Grey", "#6b7280")]
BACKGROUNDS = [("", "White", "#fff"), ("black", "Black", "#111"), ("yellow", "Yellow", "#fff2a8"),
               ("pink", "Pink", "#ffd9e6"), ("blue", "Blue", "#d9ecff"), ("none", "None", "transparent")]
STYLE_CHOICES = {"font": FONTS, "align": ALIGNS, "color": COLORS, "bg": BACKGROUNDS}
STYLE_PREFIX = {"font": "mt-f-", "align": "mt-al-", "color": "mt-c-", "bg": "mt-k-"}
STYLE_FLAGS = {"bold": "mt-b", "italic": "mt-i", "upper": "mt-u"}


def clean_style(style) -> dict:
    """A style as it's saved: only what is known, nothing for the defaults."""
    if not isinstance(style, dict):
        return {}
    out = {}
    try:
        size = int(style.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size:
        out["size"] = min(max(size, SIZE_MIN), SIZE_MAX)
    for key in STYLE_FLAGS:
        if style.get(key) is True:
            out[key] = True
    for key, options in STYLE_CHOICES.items():
        if style.get(key) and style[key] in {o[0] for o in options}:
            out[key] = style[key]
    return out


def style_classes(style) -> str:
    """A saved style as CSS classes (style_css() has the rules; the editor's script does the same)."""
    style = clean_style(style)
    out = [f"mt-fs-{style['size']}"] if style.get("size") else []
    out += [cls for key, cls in STYLE_FLAGS.items() if style.get(key)]
    out += [prefix + style[key] for key, prefix in STYLE_PREFIX.items() if style.get(key)]
    return " ".join(out)


def style_options() -> dict:
    """The choices the editor offers, so its script doesn't repeat the lists above."""
    return {"size": [SIZE_MIN, SIZE_MAX], "font": [[k, label] for k, label, _ in FONTS],
            "align": [list(a) for a in ALIGNS], "color": [list(c) for c in COLORS],
            "bg": [list(b) for b in BACKGROUNDS]}


def style_css() -> str:
    """The CSS for the style classes, on the reader's boxes and the editor's (which have the same
    classes). The font size is in cqw: percent of the layer's width, which is the page's."""
    box = ".ov-box.ov-manga-translate"
    rules = ["#reader .ov-layer, .mte-layer { container-type: inline-size; }",
             f"{box}.mt-b {{ font-weight: 800; }}",
             f"{box}.mt-i {{ font-style: italic; }}",
             f"{box}.mt-u {{ text-transform: uppercase; }}",
             f"{box}.mt-al-left {{ justify-content: flex-start; text-align: left; }}",
             f"{box}.mt-al-right {{ justify-content: flex-end; text-align: right; }}"]
    rules += [f"{box}.mt-f-{key} {{ font-family: {css}; }}" for key, _, css in FONTS if key]
    for key, _, css in COLORS:
        if key == "white":  # light text: the outline around it goes dark
            rules.append(f"{box}.mt-c-{key} {{ color: {css}; --ov-outline: #111; --mt-stroke: #111; }}")
        elif key:
            rules.append(f"{box}.mt-c-{key} {{ color: {css}; }}")
    for key, _, css in BACKGROUNDS:
        if key:
            shadow = "none" if key == "none" else f"0 0 0 2px {css}"
            rules.append(f"{box}.mt-k-{key} {{ background: {css}; box-shadow: {shadow}; }}")
    rules += [f"{box}.mt-fs-{n} > span {{ font-size: {n / 10:g}cqw; }}" for n in range(SIZE_MIN, SIZE_MAX + 1)]
    return "\n".join(rules) + "\n"


# --- the editor: what its API does --------------------------------------------------------------------
# The editor works in fractions of the page (0 to 1), like the reader's boxes; the saved data is in
# pixels of the page as it was sent to the vision server ("w", "h").

MAX_BLOCKS = 300   # boxes saved per page
MAX_TEXT = 2000    # characters per text


class EditError(Exception):
    """Something the editor asked for can't be done: the message is shown to the owner."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _frac(value) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        raise EditError("A box has a place that isn't a number") from None


def _corners(value) -> list:
    """[x1, y1, x2, y2] in fractions of the page, in order and not empty."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise EditError("A box needs four corners")
    x1, y1, x2, y2 = (_frac(v) for v in value)
    x1, x2, y1, y2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    if x2 - x1 < 0.004 or y2 - y1 < 0.004:
        raise EditError("That box is too small")
    return [x1, y1, x2, y2]


def box_id(x, y) -> str:
    """How MangaVibe names a box when the owner hides it with ×: its top-left corner."""
    return f"{min(max(x, 0.0), 1.0):.3f},{min(max(y, 0.0), 1.0):.3f}"


def hidden_boxes(ctx, book, page) -> set:
    """The ids of this page's boxes the owner hid in the reader."""
    try:
        from app.models import OverlayHidden
    except ImportError:
        return set()
    return {row.box for row in ctx.db.query(OverlayHidden).filter_by(book_id=book.id, plugin=ctx.plugin.id, page=page)}


def set_hidden_boxes(ctx, book, page, ids):
    try:
        from app.models import OverlayHidden
    except ImportError:
        return
    ctx.db.query(OverlayHidden).filter_by(book_id=book.id, plugin=ctx.plugin.id, page=page).delete()
    for box in set(ids):
        ctx.db.add(OverlayHidden(book_id=book.id, plugin=ctx.plugin.id, page=page, box=box))


def page_image(ctx, book, page):
    """(the page as it goes to the vision server, its width, its height)."""
    import io

    from PIL import Image
    image = ctx.read_page(book, page, max_width=SEND_WIDTH)
    with Image.open(io.BytesIO(image)) as im:
        return image, im.width, im.height


def editor_page(ctx, book, page) -> dict:
    """What the editor shows for a page. `state`: ready, none (not translated yet) or failed."""
    data = ctx.get_page_data(book, page)
    out = {"book": book.id, "page": page, "pages": book.page_count,
           "image": f"/api/books/{book.id}/pages/{page}?w=1440&v={int(getattr(book, 'mtime', 0) or 0)}",
           "cls": box_classes(ctx), "options": style_options(), "state": "ready", "message": "",
           "edited": False, "blocks": []}
    if data is None:
        out.update(state="none", message=getattr(ctx.plugin, "offline", ""))
        return out
    if data.get("error"):
        out.update(state="failed", message=str(data["error"]))
        return out
    w, h = data["w"], data["h"]
    hidden = hidden_boxes(ctx, book, page)
    for b, (x1, y1, x2, y2) in zip(data.get("blocks", []), display_rects(data)):
        bx1, by1, bx2, by2 = b["box"]
        out["blocks"].append({
            "x": round(x1 / w, 5), "y": round(y1 / h, 5), "w": round((x2 - x1) / w, 5), "h": round((y2 - y1) / h, 5),
            "src": b["src"], "text": b["text"], "who": b.get("who", ""), "style": clean_style(b.get("style")),
            # where the Japanese was read (for "Read again"); the same as the box for one drawn by hand
            "box": [round(bx1 / w, 5), round(by1 / h, 5), round(bx2 / w, 5), round(by2 / h, 5)],
            "hidden": box_id(x1 / w, y1 / h) in hidden})
    out["edited"] = bool(data.get("edited"))
    return out


def save_edit(ctx, book, page, items) -> dict:
    """Saves the page as the editor has it: every box at the place it has there, with its texts and
    style. Boxes without any text are left out. Returns the page as editor_page()."""
    if not isinstance(items, list) or len(items) > MAX_BLOCKS:
        raise EditError("That isn't a list of boxes")
    old = ctx.get_page_data(book, page)
    if old and not old.get("error"):
        data = {k: v for k, v in old.items() if k != "blocks"}
    else:  # a page that wasn't translated: the boxes are all the owner's
        _, w, h = page_image(ctx, book, page)
        data = {"v": DATA_VERSION, "w": w, "h": h, "model": ctx.setting("model") or "manga-translate", "mode": "page"}
    w, h = data["w"], data["h"]
    blocks, hidden = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise EditError("That isn't a list of boxes")
        src, text = (str(item.get(k) or "").strip()[:MAX_TEXT] for k in ("src", "text"))
        if not src and not text:
            continue
        x, y = _frac(item.get("x")), _frac(item.get("y"))
        x1, y1, x2, y2 = _corners([x, y, x + _frac(item.get("w")), y + _frac(item.get("h"))])
        rect = [round(x1 * w, 1), round(y1 * h, 1), round(x2 * w, 1), round(y2 * h, 1)]
        box = rect
        if item.get("box"):
            bx1, by1, bx2, by2 = _corners(item["box"])
            box = [round(bx1 * w, 1), round(by1 * h, 1), round(bx2 * w, 1), round(by2 * h, 1)]
        block = {"box": box, "rect": rect, "src": src, "text": text}
        who = str(item.get("who") or "").strip()[:80]
        if who:
            block["who"] = who
        style = clean_style(item.get("style"))
        if style:
            block["style"] = style
        blocks.append(block)
        if item.get("hidden"):
            hidden.add(box_id(rect[0] / w, rect[1] / h))
    data["blocks"] = blocks
    data["edited"] = True  # "Translate this chapter again" leaves the page alone
    ctx.set_page_data(book, page, data)
    set_hidden_boxes(ctx, book, page, hidden)
    ctx.db.commit()
    return editor_page(ctx, book, page)


def _asked(ctx, what, url, fn):
    """Runs a call to one of the PC's servers; when it fails, says so in words for the owner."""
    try:
        return fn()
    except httpx.TransportError:
        raise EditError(f"Can't reach the {what} ({url})", 502) from None
    except httpx.HTTPStatusError as e:
        raise EditError(f"The {what} answered with an error ({e.response.status_code})", 502) from None


def translate_one(ctx, src, background=()) -> str:
    """One text translated on its own, with the page's other texts as background."""
    src = clean_ocr(str(src or ""))[:MAX_TEXT]
    if not re.search(r"\w", src):
        return src  # only "…" or "!?": stays as it is
    if not ctx.setting("llm_url"):
        raise EditError("Set the LLM server's address in Settings → Plugins first")
    around = [str(t).strip()[:MAX_TEXT] for t in background if isinstance(t, str) and t.strip()][:12]
    return _asked(ctx, "LLM server", ctx.setting("llm_url"), lambda: translate_lines(ctx, [src], around)[0])


def scan_area(ctx, book, page, corners, translate=True, background=()) -> dict:
    """Reads the text in one part of the page (the text finder missed it, or read it badly) and
    translates it -> {"src", "text"}. Nothing is saved: the editor adds it to the page."""
    x1, y1, x2, y2 = _corners(corners)
    ocr = _url(ctx.setting("ocr_url")) or _url(ctx.setting("vision_url"))
    if not ocr:
        raise EditError("Set the vision server's address in Settings → Plugins first")
    image, w, h = page_image(ctx, book, page)

    def read():
        r = httpx.post(ocr + "/ocr", timeout=120, json={
            "image": base64.b64encode(image).decode(), "boxes": [[x1 * w, y1 * h, x2 * w, y2 * h]]})
        r.raise_for_status()
        return r.json()["texts"][0]
    src = clean_ocr(_asked(ctx, "vision server", ocr, read))
    return {"src": src, "text": translate_one(ctx, src, background) if translate and src else ""}


def reset_page(ctx, book, page) -> dict:
    """Forgets a page's translation (the owner's changes too) and puts it at the front of the queue."""
    if not ctx.setting("vision_url") or not ctx.setting("llm_url"):
        raise EditError("Set the server addresses in Settings → Plugins first")
    plugin, key = ctx.plugin, (book.id, page)
    ctx.set_page_data(book, page, None)
    ctx.db.commit()
    with plugin.ocr_lock:
        plugin.ocr.pop(key, None)  # read again too
    plugin.tries.pop(key, None)
    plugin.enqueue(ctx, book.id, [page], NOW)
    return editor_page(ctx, book, page)


def make_router():
    """The editor's API, at /api/plugins/manga-translate/api/... (admins only)."""
    from fastapi import APIRouter, Body, Depends, HTTPException

    from app.plugins.web import plugin_ctx
    try:
        from app.users import manage
        guards = [manage]
    except ImportError:  # MangaVibe before accounts: everyone is the owner
        guards = []
    router = APIRouter(dependencies=guards)

    def run(ctx, book_id, page, fn, *args):
        book = ctx.db.get(Book, book_id)
        if book is None or not 0 <= page < book.page_count:
            raise HTTPException(404, "No such page")
        try:
            return fn(ctx, book, page, *args)
        except EditError as e:
            raise HTTPException(e.status, str(e)) from None

    @router.get("/books/{book_id}/pages/{page}")
    def get_page(book_id: int, page: int, ctx=Depends(plugin_ctx)):
        return run(ctx, book_id, page, editor_page)

    @router.put("/books/{book_id}/pages/{page}")
    def put_page(book_id: int, page: int, body: dict = Body(...), ctx=Depends(plugin_ctx)):
        return run(ctx, book_id, page, save_edit, body.get("blocks"))

    @router.post("/books/{book_id}/pages/{page}/scan")
    def post_scan(book_id: int, page: int, body: dict = Body(...), ctx=Depends(plugin_ctx)):
        return run(ctx, book_id, page, scan_area, body.get("box"), body.get("translate", True),
                   body.get("background") or [])

    @router.post("/books/{book_id}/pages/{page}/reset")
    def post_reset(book_id: int, page: int, ctx=Depends(plugin_ctx)):
        return run(ctx, book_id, page, reset_page)

    @router.post("/translate")
    def post_translate(body: dict = Body(...), ctx=Depends(plugin_ctx)):
        try:
            return {"text": translate_one(ctx, body.get("src"), body.get("background") or [])}
        except EditError as e:
            raise HTTPException(e.status, str(e)) from None

    return router


# --- the editor: how it looks ---------------------------------------------------------------------------

EDITOR_CSS = """
#mte {
  position: fixed; inset: 0; z-index: 60; display: flex; flex-direction: column;
  padding: env(safe-area-inset-top, 0px) env(safe-area-inset-right, 0px) env(safe-area-inset-bottom, 0px) env(safe-area-inset-left, 0px);
  background: #101114; color: #eceef2; color-scheme: dark; font: 14px/1.4 system-ui, sans-serif;
  --mte-line: #2b303b; --mte-card: #1a1c22; --mte-accent: var(--accent, #ff7a59);
}
#mte[hidden], #mte [hidden] { display: none; }
#mte button, #mte select, #mte input, #mte textarea { font: inherit; color: inherit; }
#mte button, #mte select {
  min-height: 32px; padding: 4px 10px; background: var(--mte-card); border: 1px solid var(--mte-line);
  border-radius: 8px; cursor: pointer;
}
#mte button:hover { border-color: #596072; }
#mte button:disabled { opacity: .45; cursor: default; }
#mte button.on { border-color: var(--mte-accent); color: var(--mte-accent); }
#mte button.mte-primary { background: var(--mte-accent); border-color: var(--mte-accent); color: var(--accent-text, #1d0d07); font-weight: 600; }
#mte :focus-visible { outline: 2px solid var(--mte-accent); outline-offset: 1px; }
.mte-top, .mte-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--mte-line); }
.mte-top { padding-left: 14px; }
.mte-title { font-size: 15px; }
#mte button svg { display: block; flex: none; }
#mte button:has(> svg) { display: inline-flex; align-items: center; gap: 6px; }
#mte button.mte-icon { justify-content: center; width: 32px; padding: 0; }
#mte .mte-top button { background: transparent; border-color: transparent; }
#mte .mte-top button:hover { background: var(--mte-card); }
.mte-pages {
  display: flex; align-items: center; justify-content: center; gap: 10px; padding: 6px 10px;
  border-top: 1px solid var(--mte-line); font-variant-numeric: tabular-nums;
}
.mte-ask > .mte-keys { width: 100%; max-width: 460px; max-height: 100%; overflow: auto; }
.mte-keys table { width: 100%; border-collapse: collapse; }
.mte-keys th { padding: 12px 0 4px; color: #979eab; font-size: 12px; font-weight: 600; text-align: left; }
.mte-keys td { padding: 4px 0; vertical-align: top; }
.mte-keys td:first-child { padding-right: 14px; white-space: nowrap; }
.mte-keys kbd {
  display: inline-block; min-width: 22px; padding: 1px 6px; border: 1px solid #3a4050; border-bottom-width: 2px;
  border-radius: 6px; background: #0d0e11; font: 12px/18px system-ui, sans-serif; text-align: center;
}
.mte-space { flex: 1; }
.mte-main { flex: 1; display: flex; min-height: 0; }
.mte-work { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.mte-hint { padding: 5px 12px; color: #979eab; font-size: 12px; }
.mte-stage { flex: 1; display: flex; overflow: auto; padding: 14px; background: #000; }
.mte-page { position: relative; flex: none; margin: auto; }
.mte-page > img { display: block; width: 100%; height: auto; user-select: none; -webkit-user-drag: none; }
.mte-layer { position: absolute; inset: 0; }
.mte-layer.mte-draw { cursor: crosshair; touch-action: none; }
.mte-layer.mte-off .mte-box { visibility: hidden; }
.mte-box {
  position: absolute; display: flex; align-items: center; justify-content: center; background: #fff; color: #111;
  font-family: var(--overlay-font, "Comic Neue", "Comic Sans MS", "Segoe Print", system-ui, sans-serif);
  font-weight: 600; line-height: 1.12; text-align: center; overflow-wrap: normal;
  cursor: move; touch-action: none; user-select: none;
}
.mte-box > span { max-width: 100%; padding: 0 .15em; }
.mte-layer.mte-draw .mte-box { pointer-events: none; }
.mte-box.sel { z-index: 2; outline: 2px solid var(--mte-accent); outline-offset: 2px; }
.mte-box.mte-empty { background: rgba(255, 122, 89, .12); box-shadow: none; outline: 1px dashed var(--mte-accent); }
.mte-box.mte-hidden { opacity: .4; }
.mte-n {
  position: absolute; top: -9px; left: -9px; min-width: 18px; padding: 0 5px; border-radius: 9px; background: #14151a;
  color: #fff; font: 600 11px/18px system-ui, sans-serif; text-align: center; text-transform: none; pointer-events: none;
}
.mte-box.sel .mte-n { background: var(--mte-accent); color: #1d0d07; }
.mte-h {
  position: absolute; width: 12px; height: 12px; background: #fff; border: 2px solid var(--mte-accent); border-radius: 3px;
  transform: translate(-50%, -50%); touch-action: none;
}
.mte-h[data-h="nw"] { left: 0; top: 0; cursor: nwse-resize; }
.mte-h[data-h="n"] { left: 50%; top: 0; cursor: ns-resize; }
.mte-h[data-h="ne"] { left: 100%; top: 0; cursor: nesw-resize; }
.mte-h[data-h="e"] { left: 100%; top: 50%; cursor: ew-resize; }
.mte-h[data-h="se"] { left: 100%; top: 100%; cursor: nwse-resize; }
.mte-h[data-h="s"] { left: 50%; top: 100%; cursor: ns-resize; }
.mte-h[data-h="sw"] { left: 0; top: 100%; cursor: nesw-resize; }
.mte-h[data-h="w"] { left: 0; top: 50%; cursor: ew-resize; }
/* fingers: four larger handles outside the corners, so a small box can still be taken hold of */
@media (pointer: coarse) {
  .mte-h { width: 22px; height: 22px; border-radius: 50%; }
  .mte-h[data-h="n"], .mte-h[data-h="e"], .mte-h[data-h="s"], .mte-h[data-h="w"] { display: none; }
  .mte-h[data-h="nw"] { transform: translate(-95%, -95%); }
  .mte-h[data-h="ne"] { transform: translate(-5%, -95%); }
  .mte-h[data-h="se"] { transform: translate(-5%, -5%); }
  .mte-h[data-h="sw"] { transform: translate(-95%, -5%); }
}
.mte-rubber, .mte-pending {
  position: absolute; border: 2px dashed var(--mte-accent); background: rgba(255, 122, 89, .15); pointer-events: none;
}
.mte-pending { animation: mte-pulse .7s ease-in-out infinite alternate; }
@keyframes mte-pulse { to { background: rgba(255, 122, 89, .4); } }
.mte-side { display: flex; flex-direction: column; width: 370px; min-height: 0; border-left: 1px solid var(--mte-line); }
.mte-notice { margin: 10px 10px 0; padding: 10px; border: 1px solid var(--mte-line); border-radius: 10px; background: var(--mte-card); }
.mte-notice button { margin-top: 8px; }
.mte-list { flex: 1; overflow: auto; padding: 10px; }
.mte-none { color: #979eab; }
.mte-card { margin-bottom: 10px; padding: 10px; border: 1px solid var(--mte-line); border-radius: 10px; background: var(--mte-card); }
.mte-card.sel { border-color: var(--mte-accent); }
.mte-card.hid .mte-n { text-decoration: line-through; }
.mte-card .mte-n { position: static; display: inline-block; background: #343946; }
.mte-card.sel .mte-n { background: var(--mte-accent); color: #1d0d07; }
.mte-head, .mte-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.mte-head { margin-bottom: 6px; }
.mte-head input { flex: 1; min-width: 60px; }
.mte-row { margin-top: 6px; }
.mte-card label { display: block; margin-top: 6px; color: #979eab; font-size: 12px; }
#mte textarea, #mte input[type="text"] {
  display: block; width: 100%; padding: 6px 8px; background: #0d0e11; border: 1px solid var(--mte-line); border-radius: 8px;
  color: #eceef2; font-size: 14px;
}
#mte textarea { resize: vertical; }
.mte-style { margin-top: 10px; padding-top: 4px; border-top: 1px solid var(--mte-line); }
.mte-lab { width: 44px; color: #979eab; font-size: 12px; }
.mte-style input[type="range"] { flex: 1; min-width: 80px; accent-color: var(--mte-accent); }
.mte-style output { width: 34px; text-align: right; font-variant-numeric: tabular-nums; }
#mte button.mte-sw { width: 26px; min-height: 26px; padding: 0; border-radius: 50%; }
#mte button.mte-sw.on { box-shadow: 0 0 0 2px var(--mte-accent); }
/* "no background": stripes (over the colour the button gets in its style attribute) */
#mte button.mte-sw[data-v="none"] { background: repeating-linear-gradient(45deg, #777 0 4px, #222 4px 8px) !important; }
.mte-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 10px; border-top: 1px solid var(--mte-line); }
.mte-ask { position: absolute; inset: 0; z-index: 5; display: grid; place-items: center; padding: 16px; background: rgba(0, 0, 0, .6); }
.mte-ask > div { max-width: 360px; padding: 16px; border: 1px solid var(--mte-line); border-radius: 12px; background: var(--mte-card); }
.mte-ask p { margin: 0 0 12px; }
@media (max-width: 820px) {
  .mte-main { flex-direction: column; }
  .mte-side { width: auto; height: 46%; border-left: 0; border-top: 1px solid var(--mte-line); }
  .mte-hint { display: none; }
  .mte-pages { padding: 4px 8px; }
  .mte-stage { padding: 8px; }
  .mte-top, .mte-tools { padding: 6px 8px; }
}
"""


# --- the editor: its script ---------------------------------------------------------------------------
# Runs in the web app (Plugin.js). From the reader it needs three things: its "Edit translation" button
# (to open instead of asking the server), the page being read, and the changed boxes drawn again.
# MangaVibe with plugin API 12 gives all three (the "book-action" event and MangaVibe.reader); on
# older ones the script finds them in the reader's markup.

EDITOR_JS = r"""
(() => {
"use strict";
if (window.__mangaTranslateEditor) return;  // the script is loaded again when the plugin is switched on
window.__mangaTranslateEditor = true;

const PID = "manga-translate", LABEL = "__EDIT_LABEL__", API = `/api/plugins/${PID}/api`;
const MIN = 0.012;  // smallest box side, as a fraction of the page
const svg = (body) => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
  stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICON = {
  close: svg(`<path d="M6 6l12 12M18 6L6 18"/>`),
  plus: svg(`<circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5M8 11h6M11 8v6"/>`),
  minus: svg(`<circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5M8 11h6"/>`),
  left: svg(`<path d="M15 6l-6 6 6 6"/>`),
  right: svg(`<path d="M9 6l6 6-6 6"/>`),
  keys: svg(`<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>`),
};
/* the shortcuts window: [keys, what they do], or [heading]. In keys, words are keys; + / or stay as they are */
const SHORTCUTS = [
  ["The page"],
  ["+", "Zoom in"],
  ["−", "Zoom out"],
  ["0", "Fit the page to the space"],
  ["H", "Hide or show the boxes"],
  ["Shift + ←", "Next page (while no box is selected)"],
  ["Shift + →", "Previous page (while no box is selected)"],
  ["Boxes"],
  ["Shift + click", "A new text box at that place (drag for its size)"],
  ["Ctrl + drag", "Scan text: read and translate what is in the frame"],
  ["← → ↑ ↓", "Move the selected box a little"],
  ["Shift + ← → ↑ ↓", "Move the selected box more"],
  ["Delete", "Delete the selected box"],
  ["Double-click", "Edit the box's translation"],
  ["Editing"],
  ["Ctrl + Z", "Undo"],
  ["Ctrl + S", "Save"],
  ["Esc", "Leave a text field or a tool, then close the editor"],
  ["?", "This list"],
];
const HINTS = {
  move: "Click a box to edit it. Drag it to move it, drag its handles to resize it. Shift + click adds a text box, Ctrl + drag scans text.",
  text: "Drag on the page to draw a text box of your own.",
  scan: "Drag around Japanese text on the page: it is read and translated.",
};
const E = { root: null, book: 0, page: 0, pages: 0, info: null, blocks: [], sel: -1, tool: "move",
  dirty: false, changed: false, undo: [], zoom: 1, drag: null, poll: 0, pending: [], focusSnap: null };
const busy = new WeakSet();  // blocks waiting for the PC ("Read again", "Translate")

const api = (path, opts) => MangaVibe.api(API + path, opts);
const toast = (text, kind = "") => MangaVibe.toast(text, kind ? 6000 : 2500, kind);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const pct = (v) => `${(v * 100).toFixed(3)}%`;
const q = (sel) => E.root.querySelector(sel);

/* ---------------- opening: the reader's "Edit translation" button ----------------
   MangaVibe with plugin API 12 hands the button to this script (the "book-action" event) and says
   what is being read (MangaVibe.reader). Older ones (2.0.0 on) have neither: there the script takes
   the click on the button itself and reads the page from the reader's markup. */

const HOST = window.MangaVibe.reader || null;

if (HOST) {
  MangaVibe.on(PID, "book-action", (key, at) => {
    if (key !== "edit") return false;
    open(at);
    return true;
  });
} else {
  document.addEventListener("click", (e) => {
    const button = e.target.closest?.("#reader button");
    if (!button || button.textContent.trim() !== LABEL) return;
    e.preventDefault();
    e.stopPropagation();
    open(where());
  }, true);
}

/* older MangaVibe: the book and page the reader shows: the book from the address, the page from the
   page image (paged) or the page in the middle of the screen (vertical), else the page slider */
function where() {
  const m = location.hash.match(/^#\/read\/(\d+)/), reader = document.getElementById("reader");
  if (!m || !reader || reader.hidden) return null;
  let page = null;
  const image = reader.querySelector(".stage img");
  if (image) {
    const pm = (image.getAttribute("src") || "").match(/\/pages\/(\d+)/);
    if (pm) page = +pm[1];
  } else {
    const middle = innerHeight / 2;
    for (const el of reader.querySelectorAll(".vpage")) {
      const r = el.getBoundingClientRect();
      if (r.top <= middle && r.bottom > middle) { page = +el.dataset.p; break; }
    }
  }
  if (page === null || Number.isNaN(page)) page = (+reader.querySelector("#r-slider")?.value || 1) - 1;
  return { book: +m[1], page };
}

async function open(at) {
  if (!at) return toast("Open a book in the reader first", "error");
  build();
  Object.assign(E, { info: null, blocks: [], sel: -1, dirty: false, changed: false, undo: [], zoom: 1, pending: [] });
  q('[data-r="ask"]').hidden = q('[data-r="keys"]').hidden = true;
  E.root.hidden = false;
  await load(at.book, at.page);
}

function build() {
  if (E.root) return;
  const root = E.root = document.createElement("div");
  root.id = "mte";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "Edit the translation");
  root.innerHTML = `
    <div class="mte-top">
      <b class="mte-title">Edit translation</b>
      <span class="mte-space"></span>
      <button data-a="keys" title="Keyboard and mouse shortcuts (?)">${ICON.keys}<span>Shortcuts</span></button>
      <button data-a="close" class="mte-icon" title="Close the editor (Esc)" aria-label="Close the editor">${ICON.close}</button>
    </div>
    <div class="mte-main">
      <section class="mte-work">
        <div class="mte-tools">
          <button data-tool="move">Move</button>
          <button data-tool="text" title="Draw a text box of your own (or Shift + click on the page)">+ Text box</button>
          <button data-tool="scan" title="Mark text that wasn't found: it is read and translated (or Ctrl + drag on the page)">Scan text</button>
          <span class="mte-space"></span>
          <button data-a="boxes" title="See the page without the boxes (H)">Hide boxes</button>
          <button data-a="zoomout" class="mte-icon" title="Zoom out (−)" aria-label="Zoom out">${ICON.minus}</button>
          <button data-a="fit" title="Fit the page to the space (0)">Fit</button>
          <button data-a="zoomin" class="mte-icon" title="Zoom in (+)" aria-label="Zoom in">${ICON.plus}</button>
        </div>
        <div class="mte-hint" data-r="hint"></div>
        <div class="mte-stage"><div class="mte-page"><img alt="The page"><div class="mte-layer"></div></div></div>
        <div class="mte-pages">
          <button data-a="prev" class="mte-icon" title="Previous page (Shift + →)" aria-label="Previous page">${ICON.left}</button>
          <span data-r="pageno"></span>
          <button data-a="next" class="mte-icon" title="Next page (Shift + ←)" aria-label="Next page">${ICON.right}</button>
        </div>
      </section>
      <aside class="mte-side" aria-label="The texts of this page">
        <div class="mte-notice" data-r="notice" hidden></div>
        <div class="mte-list" data-r="list"></div>
        <div class="mte-foot">
          <button data-a="reset" title="Throw away this page's translation and your changes to it, and translate it again">Translate this page again</button>
          <span class="mte-space"></span>
          <button data-a="undo" title="Undo (Ctrl+Z)">Undo</button>
          <button data-a="save" class="mte-primary" title="Save (Ctrl+S)">Save</button>
        </div>
      </aside>
    </div>
    <div class="mte-ask" data-r="ask" hidden><div role="alertdialog" aria-label="Unsaved changes">
      <p>This page has changes that aren't saved.</p>
      <div class="mte-row"><button data-a="saveclose" class="mte-primary">Save and close</button>
        <button data-a="discard">Discard</button><button data-a="stay">Keep editing</button></div>
    </div></div>
    <div class="mte-ask" data-r="keys" hidden><div class="mte-keys" role="dialog" aria-label="Shortcuts">
      <div class="mte-head"><b>Shortcuts</b><span class="mte-space"></span>
        <button data-a="keys" class="mte-icon" title="Close (Esc)" aria-label="Close">${ICON.close}</button></div>
      <table>${SHORTCUTS.map(([keys, what]) => !what ? `<tr><th colspan="2">${keys}</th></tr>`
        : `<tr><td>${keys.split(" ").map((k, n, all) => (k === "+" && all.length > 1 ? k : `<kbd>${k}</kbd>`)).join(" ")}</td><td>${what}</td></tr>`).join("")}</table>
    </div></div>`;
  document.body.append(root);
  E.layer = q(".mte-layer");
  E.img = q(".mte-page > img");
  E.list = q('[data-r="list"]');
  E.img.addEventListener("load", layout);
  new ResizeObserver(() => { layout(); fitAll(); }).observe(q(".mte-stage"));
  root.addEventListener("click", onClick);
  E.layer.addEventListener("pointerdown", onDown);
  E.layer.addEventListener("pointermove", onMove);
  E.layer.addEventListener("pointerup", onUp);
  E.layer.addEventListener("pointercancel", onUp);
  E.layer.addEventListener("dblclick", () => focusText(E.sel));
  E.list.addEventListener("focusin", onFocus);
  E.list.addEventListener("input", onInput);
  E.list.addEventListener("change", onChange);
}

/* ---------------- loading and saving ---------------- */

async function load(book, page, quiet = false) {
  clearTimeout(E.poll);
  let info;
  try { info = await api(`/books/${book}/pages/${page}`); }
  catch (e) {
    if (quiet) return void (E.poll = setTimeout(wait, 4000));
    toast(e.message, "error");
    if (!E.info) finish();  // nothing to show: e.g. not an admin
    return;
  }
  if (E.root.hidden) return;
  show(info);
}

/* put a page as the server has it on the screen */
function show(info) {
  clearTimeout(E.poll);
  const same = E.info && E.info.book === info.book && E.info.page === info.page;
  Object.assign(E, { book: info.book, page: info.page, pages: info.pages, info, blocks: info.blocks,
    dirty: false, pending: [], drag: null });
  if (!same) Object.assign(E, { sel: -1, undo: [], tool: "move" });
  if (E.sel >= E.blocks.length) E.sel = -1;
  if (E.img.getAttribute("src") !== info.image) E.img.src = info.image;
  renderAll();
  if (info.state === "none") E.poll = setTimeout(wait, 2500);
}

/* a page that isn't translated yet: look again until it is (not once the owner started on it) */
function wait() {
  if (E.root.hidden || E.dirty || E.blocks.length || E.pending.length) return;
  load(E.book, E.page, true);
}

async function save() {
  if (!E.dirty) return true;
  let info;
  try { info = await api(`/books/${E.book}/pages/${E.page}`, { method: "PUT", body: { blocks: E.blocks } }); }
  catch (e) { toast(e.message, "error"); return false; }
  E.changed = true;
  show(info);
  toast("Saved");
  return true;
}

async function go(step) {
  const page = E.page + step;
  if (page < 0 || page >= E.pages) return;
  if (E.dirty && !(await save())) return;
  E.info = { ...E.info, page: -1 };  // another page: selection and undo start over
  load(E.book, page);
}

function close() {
  if (E.dirty) q(".mte-ask").hidden = false; else finish();
}

function finish() {
  clearTimeout(E.poll);
  E.root.hidden = true;
  E.dirty = false;
  if (E.changed) refreshReader();
  E.changed = false;
}

/* the reader draws the changed boxes. Older MangaVibe: it does when its Translation switch is used,
   so off and on again (or just on, so the change shows) */
function refreshReader() {
  if (HOST) return HOST.redraw();
  const sw = document.querySelector(`#reader [data-ov="${PID}"]`);
  if (!sw) return;
  if (sw.classList.contains("on")) sw.click();
  sw.click();
}

async function reset() {
  if (E.info.state === "ready" && !confirm("Translate this page again? Its boxes and your changes to them are replaced.")) return;
  try {
    const info = await api(`/books/${E.book}/pages/${E.page}/reset`, { method: "POST" });
    E.changed = true;
    E.asked = `${E.book}:${E.page}`;
    show(info);
  } catch (e) { toast(e.message, "error"); }
}

/* ---------------- undo ---------------- */

const snap = () => JSON.stringify(E.blocks);

function pushUndo(before) {
  if (before === null || before === snap()) return;
  E.undo.push(before);
  if (E.undo.length > 60) E.undo.shift();
  E.dirty = true;
  top();
}

function change(fn) {
  const before = snap();
  fn();
  pushUndo(before);
}

function undo() {
  const before = E.undo.pop();
  if (before === undefined) return;
  E.blocks = JSON.parse(before);
  if (E.sel >= E.blocks.length) E.sel = -1;
  E.dirty = true;
  renderAll();
}

/* ---------------- drawing the page ---------------- */

function renderAll() {
  top();
  notice();
  renderBoxes();
  renderList();
}

function top() {
  q('[data-r="pageno"]').textContent = `Page ${E.page + 1} / ${E.pages}`;
  q('[data-a="prev"]').disabled = E.page <= 0;
  q('[data-a="next"]').disabled = E.page >= E.pages - 1;
  q('[data-a="undo"]').disabled = !E.undo.length;
  q('[data-a="save"]').disabled = !E.dirty;  // lit up: there is something to save
  q('[data-r="hint"]').textContent = HINTS[E.tool];
  for (const b of E.root.querySelectorAll("[data-tool]")) b.classList.toggle("on", b.dataset.tool === E.tool);
  E.layer.classList.toggle("mte-draw", E.tool !== "move");
  q('[data-a="reset"]').hidden = !E.info || E.info.state !== "ready";
}

function notice() {
  const el = q('[data-r="notice"]'), info = E.info;
  el.hidden = !info || info.state === "ready";
  if (el.hidden) return;
  const asked = E.asked === `${E.book}:${E.page}`;
  el.innerHTML = info.state === "failed"
    ? `<div>This page couldn't be translated: ${esc(info.message)}</div><button data-a="reset">Try again</button>`
    : `<div>${esc(info.message || (asked ? "Translating this page…" : "This page isn't translated yet."))}</div>
       <div class="mte-none">You can also add boxes yourself with “Scan text” and “+ Text box”.</div>
       ${asked ? "" : `<button data-a="reset">Translate it now</button>`}`;
}

/* the page's size on the screen: fitted into the space, times the zoom */
function layout() {
  const stage = q(".mte-stage"), w = E.img.naturalWidth, h = E.img.naturalHeight;
  if (!w || !h || !stage.clientWidth) return;
  const pad = 2 * parseFloat(getComputedStyle(stage).paddingLeft);
  const fit = Math.min(stage.clientWidth - pad, (stage.clientHeight - pad) * w / h);
  q(".mte-page").style.width = `${Math.max(120, Math.round(fit * E.zoom))}px`;
  fitAll();
}

function styleClasses(style) {
  const s = style || {}, out = [];
  if (s.size) out.push(`mt-fs-${s.size}`);
  if (s.bold) out.push("mt-b");
  if (s.italic) out.push("mt-i");
  if (s.upper) out.push("mt-u");
  if (s.font) out.push(`mt-f-${s.font}`);
  if (s.align) out.push(`mt-al-${s.align}`);
  if (s.color) out.push(`mt-c-${s.color}`);
  if (s.bg) out.push(`mt-k-${s.bg}`);
  return out.join(" ");
}

const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"].map((h) => `<i class="mte-h" data-h="${h}"></i>`).join("");

function boxClass(b, i) {
  return `ov-box ov-${PID} mte-box ${E.info.cls} ${styleClasses(b.style)}${i === E.sel ? " sel" : ""}`
    + `${b.hidden ? " mte-hidden" : ""}${b.text ? "" : " mte-empty"}`;
}

function place(el, r) {
  Object.assign(el.style, { left: pct(r.x), top: pct(r.y), width: pct(r.w), height: pct(r.h) });
}

function renderBoxes() {
  const layer = E.layer;
  layer.innerHTML = "";
  E.blocks.forEach((b, i) => {
    const el = document.createElement("div");
    el.className = boxClass(b, i);
    el.dataset.i = i;
    el.innerHTML = `<span></span><em class="mte-n">${i + 1}</em>${i === E.sel ? HANDLES : ""}`;
    el.firstChild.textContent = b.text;
    place(el, b);
    layer.append(el);
  });
  for (const r of E.pending) {
    const el = document.createElement("div");
    el.className = "mte-pending";
    place(el, r);
    layer.append(el);
  }
  fitAll();
}

const boxEl = (i) => E.layer.querySelector(`.mte-box[data-i="${i}"]`);

/* the largest font size at which the text fits its box, as the reader does it; a box with a size
   of its own keeps that */
function fit(el) {
  const b = E.blocks[+el.dataset.i], span = el.firstChild;
  el.style.fontSize = "";
  if (!b || (b.style && b.style.size)) return;
  const h = el.clientHeight, w = el.clientWidth;
  if (!h || !w) return;
  let lo = 5, hi = Math.max(6, Math.min(h * 0.6, 64));
  for (let n = 0; n < 7; n++) {
    const mid = (lo + hi) / 2;
    el.style.fontSize = `${mid}px`;
    if (span.offsetHeight <= h && span.scrollWidth <= w) lo = mid; else hi = mid;
  }
  el.style.fontSize = `${lo}px`;
}

function fitAll() {
  if (E.layer) E.layer.querySelectorAll(".mte-box").forEach(fit);
}

/* one box again after its text, style or state changed */
function redraw(i) {
  const el = boxEl(i), b = E.blocks[i];
  if (!el || !b) return;
  el.className = boxClass(b, i);
  el.firstChild.textContent = b.text;
  fit(el);
}

/* ---------------- the list of texts ---------------- */

function renderList() {
  const scrolled = E.list.scrollTop;  // kept: the list is shorter for a moment while it's rebuilt
  E.list.innerHTML = E.blocks.map(card).join("") || (E.info.state === "ready"
    ? `<p class="mte-none">No text boxes on this page. “Scan text” reads a bubble you mark; “+ Text box” adds one you write yourself.</p>` : "");
  styleRow();
  E.list.scrollTop = scrolled;
}

function card(b, i) {
  const wait = busy.has(b);
  return `<div class="mte-card${i === E.sel ? " sel" : ""}${b.hidden ? " hid" : ""}" data-i="${i}">
    <div class="mte-head"><em class="mte-n">${i + 1}</em>
      <input type="text" data-f="who" value="${esc(b.who)}" placeholder="Who says it" aria-label="Who says it">
      <button data-c="hide" title="${b.hidden ? "Show this box in the reader again" : "Keep the box but don't show it in the reader"}">${b.hidden ? "Show" : "Hide"}</button>
      <button data-c="del" title="Delete this box">Delete</button></div>
    <label>Japanese (as read)
      <textarea data-f="src" rows="2" lang="ja" spellcheck="false">${esc(b.src)}</textarea></label>
    <div class="mte-row">
      <button data-c="ocr" ${wait ? "disabled" : ""} title="Read the Japanese in this box's place again">Read again</button>
      <button data-c="tr" ${wait || !b.src.trim() ? "disabled" : ""} title="Translate the Japanese above again">${wait ? "Working…" : "Translate"}</button></div>
    <label>Translation
      <textarea data-f="text" rows="3">${esc(b.text)}</textarea></label>
  </div>`;
}

/* the look of the selected box: a row of controls at the end of its card */
function styleRow() {
  E.list.querySelector(".mte-style")?.remove();
  const b = E.blocks[E.sel], el = E.list.querySelector(`.mte-card[data-i="${E.sel}"]`);
  if (!b || !el) return;
  const s = b.style || {}, o = E.info.options;
  const swatch = (key, [v, label, css]) => `<button class="mte-sw${(s[key] || "") === v ? " on" : ""}" data-o="${key}"
    data-v="${esc(v)}" style="background:${esc(css)}" title="${esc(label)}" aria-label="${esc(label)}"></button>`;
  const flag = (key, label, title) => `<button data-t="${key}" class="${s[key] ? "on" : ""}" title="${title}">${label}</button>`;
  el.insertAdjacentHTML("beforeend", `<div class="mte-style">
    <div class="mte-row"><span class="mte-lab">Size</span>
      <button data-c="auto" class="${s.size ? "" : "on"}" title="As large as fits the box">Auto</button>
      <input type="range" data-f="size" min="${o.size[0]}" max="${o.size[1]}" step="1" value="${s.size || fitted(E.sel)}" aria-label="Font size">
      <output>${s.size || "Auto"}</output></div>
    <div class="mte-row"><span class="mte-lab">Style</span>
      ${flag("bold", "<b>B</b>", "Bold")}${flag("italic", "<i>I</i>", "Italic")}${flag("upper", "AA", "Capital letters")}
      <select data-f="font" aria-label="Font">${o.font.map(([v, label]) =>
        `<option value="${esc(v)}"${(s.font || "") === v ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></div>
    <div class="mte-row"><span class="mte-lab">Align</span>
      ${o.align.map(([v, label]) => `<button data-o="align" data-v="${esc(v)}" class="${(s.align || "") === v ? "on" : ""}">${esc(label)}</button>`).join("")}</div>
    <div class="mte-row"><span class="mte-lab">Text</span>${o.color.map((c) => swatch("color", c)).join("")}</div>
    <div class="mte-row"><span class="mte-lab">Box</span>${o.bg.map((c) => swatch("bg", c)).join("")}</div>
    <div class="mte-row"><button data-c="all" title="Give every box on this page this size and style">Use this look for all boxes</button></div>
  </div>`);
}

/* the size a box's fitted text has now, in the editor's size steps (tenths of a percent of the page's width) */
function fitted(i) {
  const el = boxEl(i), o = E.info.options.size;
  const px = el ? parseFloat(getComputedStyle(el.firstChild).fontSize) : 0;
  return clamp(Math.round(px / (E.layer.clientWidth || 1) * 1000) || 20, o[0], o[1]);
}

function select(i, { scroll = false, list = true } = {}) {
  if (i === E.sel) return;
  E.sel = i;
  renderBoxes();
  if (!list) return;
  for (const el of E.list.querySelectorAll(".mte-card")) el.classList.toggle("sel", +el.dataset.i === i);
  styleRow();
  if (scroll) E.list.querySelector(".mte-card.sel")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function focusText(i) {
  const ta = E.list.querySelector(`.mte-card[data-i="${i}"] [data-f="text"]`);
  if (!ta) return;
  ta.focus();
  ta.select();
}

/* ---------------- typing in the list ---------------- */

const cardIndex = (el) => +(el.closest(".mte-card")?.dataset.i ?? -1);

function onFocus(e) {
  if (!e.target.dataset.f) return;
  E.focusSnap = snap();
  select(cardIndex(e.target));
}

function onInput(e) {
  const f = e.target.dataset.f, i = cardIndex(e.target), b = E.blocks[i];
  if (!f || !b) return;
  if (f === "size") {
    b.style = { ...b.style, size: +e.target.value };
    e.target.nextElementSibling.textContent = e.target.value;
    e.target.previousElementSibling.classList.remove("on");
  } else if (f === "font") {
    b.style = { ...b.style, font: e.target.value };
  } else {
    b[f] = e.target.value;
    if (f === "src") {
      const tr = e.target.closest(".mte-card").querySelector('[data-c="tr"]');
      if (!busy.has(b)) tr.disabled = !b.src.trim();
    }
  }
  E.dirty = true;
  top();
  redraw(i);
}

/* a field was left (or a slider let go): what changed since it was entered is one step to undo */
function onChange(e) {
  if (!e.target.dataset.f) return;
  pushUndo(E.focusSnap);
  E.focusSnap = snap();
}

/* ---------------- buttons ---------------- */

function onClick(e) {
  const b = e.target.closest("button");
  if (!b || b.disabled) {
    const el = e.target.closest(".mte-card");
    if (el && !e.target.closest("input, textarea, select")) select(+el.dataset.i);
    return;
  }
  const d = b.dataset, i = cardIndex(b);
  if (d.tool) { E.tool = d.tool; return top(); }
  if (d.a) return actions[d.a]?.();
  if (d.c) {
    if (d.c !== "del") select(i);  // the box the button is about
    return cardActions[d.c]?.(i, E.blocks[i]);
  }
  const block = E.blocks[i];
  if (!block) return;
  if (d.t) change(() => { block.style = { ...block.style, [d.t]: !(block.style || {})[d.t] }; });
  else if (d.o) change(() => { block.style = { ...block.style, [d.o]: d.v }; });
  else return;
  redraw(i);
  styleRow();
}

const actions = {
  close, save, undo, reset,
  prev: () => go(-1),
  next: () => go(1),
  saveclose: async () => { q(".mte-ask").hidden = true; if (await save()) finish(); },
  discard: () => { q(".mte-ask").hidden = true; E.dirty = false; finish(); },
  stay: () => { q(".mte-ask").hidden = true; },
  boxes() {
    const off = E.layer.classList.toggle("mte-off");
    q('[data-a="boxes"]').textContent = off ? "Show boxes" : "Hide boxes";
  },
  keys() {
    const el = q('[data-r="keys"]');
    el.hidden = !el.hidden;
    if (!el.hidden) el.querySelector("button").focus();
  },
  zoomin: () => { E.zoom = Math.min(4, E.zoom * 1.25); layout(); },
  zoomout: () => { E.zoom = Math.max(0.5, E.zoom / 1.25); layout(); },
  fit: () => { E.zoom = 1; layout(); },
};

const cardActions = {
  del(i) {
    change(() => { E.blocks.splice(i, 1); E.sel = -1; });
    renderAll();
  },
  hide(i, b) {
    change(() => { b.hidden = !b.hidden; });
    renderAll();
  },
  auto(i, b) {
    const size = (b.style || {}).size ? 0 : fitted(i);
    change(() => { b.style = { ...b.style, size }; });
    redraw(i);
    styleRow();
  },
  all(i, b) {
    change(() => { for (const other of E.blocks) other.style = { ...b.style }; });
    renderBoxes();
    toast("Every box on this page has this look now");
  },
  /* read the Japanese again, where it was read before (or where the box is) */
  ocr: (i, b) => ask(b, `/books/${E.book}/pages/${E.page}/scan`,
    { box: b.box || [b.x, b.y, b.x + b.w, b.y + b.h], translate: false },
    (r) => { if (r.src) b.src = r.src; else toast("No text was read there", "error"); }),
  tr: (i, b) => ask(b, "/translate", { src: b.src, background: around(b) }, (r) => { b.text = r.text; }),
};

/* the page's other Japanese texts: they tell the model what the talk is about */
const around = (b) => E.blocks.filter((x) => x !== b && x.src).map((x) => x.src).slice(0, 12);

/* ask the PC something about block b, and put the answer in with `fill` (one step to undo) */
async function ask(b, path, body, fill) {
  if (busy.has(b)) return;
  const at = `${E.book}:${E.page}`;
  busy.add(b);
  renderList();
  let answer = null;
  try { answer = await api(path, { method: "POST", body }); }
  catch (e) { toast(e.message, "error"); }
  busy.delete(b);
  if (at !== `${E.book}:${E.page}` || !E.blocks.includes(b)) return;  // another page, or undone meanwhile
  if (answer) change(() => fill(answer));
  renderAll();
}

/* ---------------- the mouse or finger on the page ---------------- */

function point(e) {
  const r = E.layer.getBoundingClientRect();
  return { x: clamp((e.clientX - r.left) / r.width, 0, 1), y: clamp((e.clientY - r.top) / r.height, 0, 1) };
}

const rectOf = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });

function onDown(e) {
  if (e.button || E.drag) return;
  const handle = e.target.closest(".mte-h"), el = e.target.closest(".mte-box"), start = point(e);
  // without picking a tool first: Shift + click (or drag) is a text box, Ctrl + drag scans text
  const tool = E.tool !== "move" ? E.tool : e.shiftKey ? "text" : e.ctrlKey || e.metaKey ? "scan" : "";
  if (tool) {
    const rubber = document.createElement("div");
    rubber.className = "mte-rubber";
    E.layer.append(rubber);
    E.drag = { kind: "draw", start, rubber, tool };
  } else if (handle) {
    E.drag = { kind: "size", i: E.sel, h: handle.dataset.h, start, from: { ...E.blocks[E.sel] }, before: snap() };
  } else if (el) {
    const i = +el.dataset.i;
    select(i, { scroll: true });
    E.drag = { kind: "move", i, start, from: { ...E.blocks[i] }, before: snap() };
  } else {
    return select(-1);
  }
  E.drag.pointer = e.pointerId;
  E.layer.setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onMove(e) {
  const d = E.drag;
  if (!d || e.pointerId !== d.pointer) return;
  const p = point(e), dx = p.x - d.start.x, dy = p.y - d.start.y;
  if (d.kind === "draw") return place(d.rubber, rectOf(d.start, p));
  if (!d.moved && Math.abs(dx) + Math.abs(dy) < 0.004) return;  // a click, not a drag
  d.moved = true;
  const b = E.blocks[d.i], f = d.from;
  if (d.kind === "move") {
    b.x = clamp(f.x + dx, 0, 1 - f.w);
    b.y = clamp(f.y + dy, 0, 1 - f.h);
  } else {
    let x1 = f.x, y1 = f.y, x2 = f.x + f.w, y2 = f.y + f.h;
    if (d.h.includes("w")) x1 = clamp(x1 + dx, 0, x2 - MIN);
    if (d.h.includes("e")) x2 = clamp(x2 + dx, x1 + MIN, 1);
    if (d.h.includes("n")) y1 = clamp(y1 + dy, 0, y2 - MIN);
    if (d.h.includes("s")) y2 = clamp(y2 + dy, y1 + MIN, 1);
    Object.assign(b, { x: x1, y: y1, w: x2 - x1, h: y2 - y1 });
  }
  const el = boxEl(d.i);
  place(el, b);
  if (d.kind === "size") fit(el);
}

function onUp(e) {
  const d = E.drag;
  if (!d || e.pointerId !== d.pointer) return;
  E.drag = null;
  if (d.kind !== "draw") return pushUndo(d.before);
  d.rubber.remove();
  let r = rectOf(d.start, point(e));
  E.tool = "move";
  top();
  if (e.type === "pointercancel") return;
  if (r.w < MIN || r.h < MIN) {
    if (d.tool !== "text") return;
    // a click, not a drag: a text box of a usual size around that place
    const w = 0.2, h = 0.07;
    r = { x: clamp(d.start.x - w / 2, 0, 1 - w), y: clamp(d.start.y - h / 2, 0, 1 - h), w, h };
  }
  if (d.tool === "text") addText(r); else addScan(r);
}

function addBlock(r, src, text, box) {
  change(() => {
    E.blocks.push({ x: r.x, y: r.y, w: r.w, h: r.h, src, text, who: "", style: {}, box, hidden: false });
    E.sel = E.blocks.length - 1;
  });
  renderAll();
  E.list.querySelector(".mte-card.sel")?.scrollIntoView({ block: "nearest" });
}

function addText(r) {
  addBlock(r, "", "Text", null);
  focusText(E.sel);
}

/* text the finder missed: the marked part of the page is read and translated, and becomes a box */
async function addScan(r) {
  const at = `${E.book}:${E.page}`, box = [r.x, r.y, r.x + r.w, r.y + r.h];
  E.pending.push(r);
  renderBoxes();
  let answer = null;
  try {
    answer = await api(`/books/${E.book}/pages/${E.page}/scan`,
      { method: "POST", body: { box, translate: true, background: around(null) } });
  } catch (e) { toast(e.message, "error"); }
  if (at !== `${E.book}:${E.page}`) return;
  E.pending = E.pending.filter((x) => x !== r);
  if (answer && !answer.src) toast("No text was read there. Try a closer frame around the text.", "error");
  if (answer && answer.src) addBlock(r, answer.src, answer.text, box); else renderBoxes();
}

/* ---------------- keys: all of them stay in the editor while it's open ---------------- */

window.addEventListener("keydown", (e) => {
  if (!E.root || E.root.hidden) return;
  e.stopPropagation();  // not the reader's (arrows turn its pages, Escape leaves it)
  const slider = e.target.type === "range";  // takes the arrow keys itself, nothing else
  const typing = !slider && /^(TEXTAREA|INPUT|SELECT)$/.test(e.target.tagName || "");
  const ctrl = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
  const ask = q('[data-r="ask"]'), keys = q('[data-r="keys"]');
  if (e.key === "Escape") {
    e.preventDefault();
    if (!keys.hidden) keys.hidden = true;
    else if (!ask.hidden) ask.hidden = true;
    else if (E.tool !== "move") { E.tool = "move"; top(); }
    else if (typing) e.target.blur();
    else close();
  } else if (ctrl && key === "s") {
    e.preventDefault();
    if (typing) e.target.blur();  // what is being typed counts
    save();
  } else if (ctrl && key === "z" && !typing) {
    e.preventDefault();
    undo();
  } else if (typing || ctrl || e.altKey || !ask.hidden) {
    // typing, the browser's own shortcuts, or the question about unsaved changes: no hotkeys
  } else if (HOTKEYS[key]) {
    e.preventDefault();
    actions[HOTKEYS[key]]();
  } else if (!keys.hidden || slider) {
    // the shortcuts list is up, or a slider has the arrow keys
  } else if (E.blocks[E.sel]) {
    const b = E.blocks[E.sel], step = e.shiftKey ? 0.01 : 0.002;
    const by = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (by) {
      e.preventDefault();
      change(() => { b.x = clamp(b.x + by[0], 0, 1 - b.w); b.y = clamp(b.y + by[1], 0, 1 - b.h); });
      place(boxEl(E.sel), b);
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      cardActions.del(E.sel);
    }
  } else if (e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    // no box selected: the pages, in the direction a manga is read (the next page is to the left)
    e.preventDefault();
    go(e.key === "ArrowLeft" ? 1 : -1);
  }
}, true);

/* keys that work whenever nothing is being typed -> what they do (in `actions`) */
const HOTKEYS = { "+": "zoomin", "=": "zoomin", "-": "zoomout", "_": "zoomout", "0": "fit", "h": "boxes", "?": "keys" };

/* the reader was left under the editor (the back button): nothing to edit any more */
window.addEventListener("hashchange", () => {
  if (!E.root || E.root.hidden || location.hash.startsWith(`#/read/${E.book}`)) return;
  if (E.dirty) save();
  E.changed = false;
  finish();
});
})();
""".replace("__EDIT_LABEL__", EDIT_LABEL)


MangaTranslate.css += style_css()
if HAS_EDITOR:
    MangaTranslate.css += EDITOR_CSS
    MangaTranslate.js = EDITOR_JS
    MangaTranslate.router = make_router()
