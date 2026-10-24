# Manga Translate for MangaVibe

Read Japanese manga in English. While you read in [MangaVibe](https://github.com/ivokolarov0/manga-vibe),
English text appears on top of the Japanese speech bubbles.

**How it works, in one sentence:** MangaVibe (wherever it runs) sends each page to your **PC**, your PC
finds the text, reads it and translates it, and sends the English back. Each page is translated only
once and then saved.

So you need:

- **MangaVibe 1.7.0 or newer**, on any machine: a NAS, a home server, or the same PC
- **A Windows PC with a graphics card** (NVIDIA, AMD or Intel, 4 GB or more), on the same home network.
  It must be switched on while you read new chapters.

Setting it up takes about 15 minutes, once.

---

## Part 1: on your PC

You will install two programs and download one file.

### Step 1: Download this plugin

1. At the top of this page, click the green **Code** button → **Download ZIP**.
2. Unzip it, for example to `C:\MangaTranslate`. You'll use this folder in the next steps.

### Step 2: Install Ollama (it translates)

1. Go to [ollama.com](https://ollama.com), click **Download**, run the installer.
2. When it's done, you see a small llama icon in the system tray (bottom right, near the clock).

### Step 3: Download the translation model

1. Press the **Windows key**, type `cmd`, press **Enter**. A black window opens.
2. How much memory does your graphics card have? (Not sure? Press Ctrl+Shift+Esc → **Performance** →
   **GPU** → "Dedicated GPU memory".)
   - **8 GB or more:** type this and press Enter. It downloads about 6 GB.
     ```
     ollama pull gemma4
     ```
   - **Less than 8 GB:** type these two lines (Enter after each). It downloads about 1 GB.
     ```
     cd C:\MangaTranslate
     ollama create manga-translate -f Modelfile
     ```
3. Wait until it says **success**. Remember which one you picked: `gemma4` or `manga-translate`.

### Step 4: Let MangaVibe talk to Ollama

Ollama only talks to programs on the PC itself, until you change one setting (you can skip this step
if MangaVibe runs on this same PC):

1. Press the **Windows key**, type `environment`, click **Edit environment variables for your account**.
2. Under the top list, click **New…**
3. **Variable name:** `OLLAMA_HOST`  **Variable value:** `0.0.0.0`  → **OK** → **OK**.
4. Right-click the llama icon in the tray → **Quit Ollama**. Then start **Ollama** again from the Start menu.

### Step 5: Install uv (it runs the text reader)

1. Press the **Windows key**, type `powershell`, press **Enter**. A blue window opens.
2. Paste this line and press **Enter**:
   ```
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. Close the blue window when it's done.

### Step 6: Start the text reader

This is the part that finds the speech bubbles and reads the Japanese.

1. Open the `C:\MangaTranslate` folder.
2. Click the address bar at the top of the folder window, type `cmd`, press **Enter**. A black window
   opens, already in the right folder.
3. Type this and press **Enter**:
   ```
   uv run manga_vision_server.py
   ```
4. The first time, it downloads what it needs (a few minutes). Then it shows something like:
   ```
   Manga vision server 1.0.0 is running on GPU (DirectML).
   In MangaVibe, use this address:  http://192.168.1.50:7865
   ```
   **Write down that address.** The numbers in the middle (`192.168.1.50`) are your PC's address.
5. If Windows asks about the firewall, tick **Private networks** and click **Allow**.
6. **Leave this window open.** Closing it stops the translations. (Next time you only do this step 6.)

### Step 7 (recommended): Keep your PC's address the same

Your router may give the PC a different address after a restart, and then MangaVibe can't find it.
Open your router's settings page, find the list of devices, and choose **reserve IP** /
**static lease** / **DHCP reservation** for your PC. (Every router names it a little differently.)

---

## Part 2: in MangaVibe

### Step 8: Add the plugin

1. Copy `manga_translate.py` from `C:\MangaTranslate` into MangaVibe's **plugins** folder. It's
   inside MangaVibe's data folder: if you run MangaVibe in Docker, that's the folder you mounted as
   `/data`, so the file goes into `<that folder>/plugins`.
2. In MangaVibe, open **Settings → Plugins** and click **Reload plugins**.
   **Manga Translate** appears in the list.

### Step 9: Tell it where your PC is

Click **Settings** next to Manga Translate and fill in (use your own numbers from step 6):

| Box | What to type | Example |
|---|---|---|
| **Vision server** | the address from step 6 | `http://192.168.1.50:7865` |
| **LLM server** | the same numbers, but `:11434` at the end | `http://192.168.1.50:11434` |
| **Model** | what you picked in step 3 | `gemma4` or `manga-translate` |

Leave the other boxes as they are. Click **Save**, then click **Test connection**.
It should say *Vision server OK · LLM server OK, model found*. If not, see
[Something doesn't work](#something-doesnt-work).

---

## Part 3: reading

1. Open a Japanese chapter.
2. Tap the middle of the page, then the **☰** button (bottom corner) to open the reading options.
3. Switch **Translation** to **On**.

The English appears after a second or two ("Translating…" shows at the top meanwhile). The next 8
pages are translated in the background, so turning pages doesn't wait. Translation stays on for this
series until you switch it off.

**Good to know:**

- **See the Japanese:** with a mouse, point at an English box.
- **Remove a wrong box:** tap the middle of the page; each box gets a small **×**. Tap it.
  *Show hidden boxes* in the reading options brings them back.
- **Translate ahead of time:** in the reading options, **Translate this chapter**. Or on the series
  page, **Plugin tools → Translate the whole series** (chapter after chapter; the page you're reading
  always goes first).
- **See what's done:** chapters show **Translated** or **Translated 12/40** in the series' book list.
- **Better or different translation:** change the **Model** in the plugin's settings, then in the
  reader **Translate this chapter again**.
- **Names:** if a name keeps coming out wrong, add it under **Names and terms**, like
  `炭治郎=Tanjiro; 先輩=senpai` (Japanese = English, separated by `;`).
- **PC switched off?** Already translated pages still show. New pages wait, and the reader says
  *Can't reach the vision server*. When the PC and step 6 run again, it carries on by itself.

### Every time you want to read new chapters

On the PC: open the `C:\MangaTranslate` folder → type `cmd` in the address bar → `uv run manga_vision_server.py`.
Ollama starts by itself with Windows.

---

## Something doesn't work

| MangaVibe says | What to do |
|---|---|
| *Can't reach the vision server* | Is the black window from step 6 still open? On your phone, open `http://<PC address>:7865/health`. If nothing loads: allow it in the Windows firewall (step 6.5), or the PC's address changed (step 7). |
| *Can't reach the LLM server* | Step 4 wasn't done, or Ollama wasn't restarted after it. |
| *has no model called …* | The **Model** box must match step 3 exactly. In a black window, `ollama list` shows the names. |
| *set the server addresses in Settings* | Step 9. |
| No **Translation** switch in the reader | Step 8: is the plugin in the list, and switched on? MangaVibe must be 1.7.0 or newer. |
| `uv` is not recognized | Close the window and open a new one (step 5 only works in new windows). |
| Very slow | The black window from step 6 says `GPU` or `CPU` at the start. `CPU` works, but slowly. |

## Try it without MangaVibe

Put a few manga pages in a folder, then in the black window from step 6 (with the server running in
another window):

```
uv run try_pages.py C:\path\to\the\folder --model gemma4
```

It makes `report.html` in that folder: every page with the English on top, and a list of what was
read and how it was translated.

## What's in this folder

| File | What it is |
|---|---|
| `manga_translate.py` | the plugin; goes into MangaVibe's plugins folder |
| `manga_vision_server.py` | the text reader that runs on your PC (step 6) |
| `Modelfile` | settings for the small translation model (step 3) |
| `try_pages.py` | the test tool above |

**The three models:** Manga-Bubble-YOLO finds the text, manga-ocr-nar reads the Japanese, and Gemma 4
or Hy-MT2 translates. They are downloaded on first use. The text reading model is free for personal
use only (CC BY-NC-SA 4.0), **not for commercial use**.
