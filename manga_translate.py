"""Manga Translate: English over Japanese manga pages in MangaVibe (plugin API 3).

Copy this file into MangaVibe's plugins folder, then Settings -> Plugins -> Reload plugins.
The work happens on a PC with a GPU (see README.md):

  - the vision server (manga_vision_server.py) finds the text on a page and reads it
  - an LLM server with an OpenAI-style API (Ollama, LM Studio, llama.cpp) translates it

How it works: while you read with "Translation" switched on, the reader asks for each page. A page
that isn't translated yet goes to the front of the queue, with the next few pages behind it. Two
threads work through the queue: one sends pages to the vision server, the other sends the text to
the LLM, so both run at the same time. Results are saved per page in MangaVibe's database.
"""
import base64
import heapq
import itertools
import queue
import re
import threading
import time

import httpx

from app.models import Book
from app.plugins import Action, Box, Overlay, Plugin, SettingDef

VERSION = "0.1.0"
DATA_VERSION = 1          # the format of what is saved per page
SEND_WIDTH = 1600         # pages are shrunk to this width before they go to the PC
OFFLINE_RETRY = 15        # seconds between tries while the PC can't be reached
PAGE_TRIES = 2            # a page that fails this often is saved as failed (not tried forever)

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
        SettingDef("target", "Translate into", default="English"),
        SettingDef("ahead", "Pages to translate ahead", default="8",
                   help="Pages after the one you're reading that are translated in advance"),
        SettingDef("glossary", "Names and terms",
                   help="Fixed translations, separated by ;  e.g.  先輩=senpai; 春香=Haruka"),
    ]
    global_actions = [Action("test", "Test connection"), Action("stop", "Stop the queue")]
    actions = [Action("series", "Translate the whole series"),
               Action("forget", "Forget this series' translations",
                      confirm="Delete the saved translations of every book in this series?")]
    book_actions = [Action("chapter", "Translate this chapter"),
                    Action("again", "Translate this chapter again")]
    css = """
.ov-box.ov-manga-translate { border-radius: 14px; box-shadow: 0 0 0 2px #fff; }
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
            return Overlay(pending=True, message=self.offline or "Translating…")
        if data.get("error"):
            return Overlay(message=f"Couldn't translate this page: {data['error']}")
        return Overlay(boxes=boxes_for(data))

    def book_badges(self, ctx, books):
        done = ctx.page_data_counts(books)
        out = {}
        for b in books:
            n = done.get(b.id, 0)
            if n:
                out[b.id] = "Translated" if n >= b.page_count else f"Translated {n}/{b.page_count}"
        return out

    def run_book_action(self, ctx, key, book):
        if key == "again":
            ctx.clear_page_data(book)
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
                image = ctx.read_page(book, page, max_width=SEND_WIDTH)
                result = read_text(ctx, image)
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
                    self.handoff.put((key, result), timeout=1)
                    break
                except queue.Full:
                    pass

    # --- worker 2: text -> translation (LLM) -------------------------------------------------------
    def llm_worker(self, ctx):
        while not ctx.should_stop():
            try:
                key, result = self.handoff.get(timeout=1)
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
                    data = translate_page(ctx, book, page, result)
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


def chat(ctx, prompt: str) -> str:
    with httpx.Client(timeout=180) as http:
        r = http.post(llm_base(ctx) + "/chat/completions", json={
            "model": ctx.setting("model") or "manga-translate",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "reasoning_effort": "none",  # thinking models: answer straight away
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


def glossary_lines(ctx):
    pairs = []
    for item in str(ctx.setting("glossary") or "").split(";"):
        if "=" in item:
            src, dst = (x.strip() for x in item.split("=", 1))
            if src and dst:
                pairs.append(f"{src} translates to {dst}")
    return pairs


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


def translate_page(ctx, book, page, result) -> dict:
    blocks = [b for b in result["blocks"] if b["text"].strip()]
    previous = []
    if page > 0:
        before = ctx.get_page_data(book, page - 1) or {}
        previous = [b["src"] for b in before.get("blocks", [])][-8:]
    english = translate_lines(ctx, [b["text"] for b in blocks], previous) if blocks else []
    return {"v": DATA_VERSION, "w": result["width"], "h": result["height"],
            "model": ctx.setting("model") or "manga-translate",
            "blocks": [{"box": [round(v, 1) for v in b["box"][:4]], "src": b["text"], "text": en}
                       for b, en in zip(blocks, english)]}


def boxes_for(data):
    """Saved page -> boxes for the reader. The text box is grown a little: English needs more room
    than a column of Japanese, and the bubble around it is bigger anyway. Tall thin boxes (vertical
    Japanese) are widened to at least 2/3 of their height, so English lines aren't one word each."""
    w, h = data["w"], data["h"]
    out = []
    for b in data.get("blocks", []):
        x1, y1, x2, y2 = b["box"]
        grow_x = max((x2 - x1) * 0.25, ((y2 - y1) * 0.66 - (x2 - x1)) / 2)
        grow_y = (y2 - y1) * 0.08
        x1, x2 = max(0, x1 - grow_x), min(w, x2 + grow_x)
        y1, y2 = max(0, y1 - grow_y), min(h, y2 + grow_y)
        out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, text=b["text"], title=b["src"]))
    return out


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
    return f"{found[0]} · LLM server OK, model '{model}' found"
