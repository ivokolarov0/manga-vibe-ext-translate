# Manga Translate: the PC side

MangaVibe can show English over Japanese manga pages. The work is done by three small models on a
PC with a GPU. MangaVibe (on your NAS or wherever it runs) sends pages to that PC over your home
network and saves the results.

| Job | Model | Runs in |
|---|---|---|
| Find the text on the page | Manga-Bubble-YOLO (YOLO26 nano) | the vision server (this folder) |
| Read the Japanese (OCR) | manga-ocr-nar-small | the vision server (this folder) |
| Translate | Hy-MT2 1.8B (or any model you like) | Ollama |

Ollama only runs language models, so the first two need the vision server. Both together use about
2 GB of graphics memory; any NVIDIA, AMD or Intel GPU from the last few years is enough. Without a
GPU it still works, just slower.

## Setup (once)

**1. Ollama, for the translation.** Install it from [ollama.com](https://ollama.com), then in this folder:

```
ollama create manga-translate -f Modelfile
```

This downloads Hy-MT2 (1.1 GB) and fixes its chat template (the one inside the download doesn't work
in Ollama). Any other Ollama model works too, e.g. `gemma4` or `translategemma`; just use its name in
MangaVibe.

**2. Let MangaVibe reach Ollama.** Ollama only answers the PC itself by default. Set the Windows
environment variable `OLLAMA_HOST` to `0.0.0.0` (Start → "Edit environment variables for your
account"), then quit Ollama from the tray icon and start it again.

**3. uv, to run the vision server.** Install it from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)
(one command). uv sets up Python and everything the server needs by itself.

**4. Keep the PC's address fixed.** In your router, reserve the PC's IP address (often called
"DHCP reservation"). Otherwise it can change after a restart and MangaVibe loses the PC.

## Every time you want translations

```
uv run manga_vision_server.py
```

The first start downloads the two models (about 250 MB). It then prints the address to use, e.g.
`http://192.168.1.127:7865`. Leave the window open. When Windows Firewall asks, allow it on
**private** networks.

In MangaVibe → Settings → Plugins → Manga Translate, enter:

- **Vision server:** the address printed above
- **Ollama:** `http://<the same IP>:11434`
- **Model:** `manga-translate`

## Try it first

To see how good the translation is on your own pages before using MangaVibe, put a few pages in a
folder and run:

```
uv run try_pages.py path\to\folder
```

It writes `report.html` in that folder: each page with the English drawn over it, and a table of
what was read and how it was translated.

## Something doesn't work

- **MangaVibe can't reach the PC.** Open `http://<PC IP>:7865/health` on your phone. No answer means
  the firewall blocks it, the server isn't running, or the IP changed.
- **The vision server works but the translation doesn't.** `OLLAMA_HOST` isn't set (step 2), or Ollama
  wasn't restarted after setting it.
- **Slow.** The server prints `GPU (...)` or `CPU` when it starts. On Linux it uses the CPU for now.
- **Options:** `--port 7865` changes the port, `--cpu` skips the GPU.

## Licences

The OCR model's weights are CC BY-NC-SA 4.0: free for personal use, **not for commercial use**. The
text position model and Hy-MT2 are Apache-2.0. Models are downloaded from Hugging Face on first start
and aren't part of MangaVibe.
