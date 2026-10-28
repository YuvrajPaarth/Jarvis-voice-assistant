"""
Jarvis — local voice assistant with RAG-based file context injection.

Architecture (why this shape, for a Mac / Apple Silicon target):
- Ollama does BOTH generation and embeddings locally -> no cloud calls, no API keys.
- ChromaDB (local, persistent, on-disk) stores chunk embeddings so we don't
  re-embed your whole folder every time you ask a question -> saves unified
  memory and CPU on every turn after the first index build.
- We inject only the top-k relevant chunks into the prompt instead of every
  file's full text. This is the actual fix for "local files not working":
  the old version silently overflowed Ollama's ~2k-4k token context window
  and got truncated. Retrieval keeps the prompt small and relevant regardless
  of how many files you have.
- Chunking is word-based with overlap (no heavy LangChain/LlamaIndex dep tree)
  to keep the footprint light — good tradeoff on constrained Mac memory
  when Ollama + Whisper + Chroma are all resident at once.

NEW FEATURES:
- Online mode: Uses Gemini API (gemini-1.5-flash) with RAG context injection.
- Offline mode: Uses local Ollama (qwen2.5-coder:1.5b). Default.
- Voice switch: Say "switch to online" / "switch to offline" to toggle.
- Config: API key stored in ~/.jarvis/config.json (never hardcoded).
- PIN lock: Jarvis asks for your PIN on every wake. Wrong PIN = goes back to sleep.
"""

import requests, wave, tempfile, os, subprocess, threading, math, time, json, hashlib
import pyaudio
import whisper
import tkinter as tk

# ---------------- Config ----------------
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
MODEL            = "llama3.2:3b"
EMBED_MODEL      = "nomic-embed-text"     # pull with: ollama pull nomic-embed-text
GEMINI_API_URL   = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
WAKE_WORD        = "jarvis"
WAKE_SECS        = 4       # seconds to listen for the wake word — 2 was too short
SAMPLE_RATE      = 16000
CHANNELS         = 1
CHUNK            = 1024
MAX_RETRIES      = 3
VOICE            = "Samantha"
FILES_FOLDER     = os.path.expanduser("~/jarvis_brain")
CHROMA_PATH      = os.path.expanduser("~/.jarvis/chroma_db")
INDEX_CACHE      = os.path.expanduser("~/.jarvis/index_cache.json")
CONFIG_PATH      = os.path.expanduser("~/.jarvis/config.json")
W, H             = 420, 420
SPEAK_RATE       = 120

# Chunking / retrieval tuning
CHUNK_WORDS   = 220
CHUNK_OVERLAP = 40
TOP_K         = 4
NUM_CTX       = 8192
MAX_FILE_MB   = 10

# ---------------- Secure Config (API key + PIN) ----------------
# Config is stored in ~/.jarvis/config.json.
# Structure: {"gemini_api_key": "...", "pin_hash": "<sha256>"}
# The PIN is stored as a SHA-256 hash — never in plain text.
# On first run, Jarvis prints setup instructions to terminal.

# Wake detection tuning
# Records WAKE_CLIP_SECS clips, only runs Whisper if RMS > WAKE_RMS_THRESHOLD (speech detected).
# Lower WAKE_RMS_THRESHOLD if it's missing your voice; raise it if ambient noise
# is triggering false wakes.
WAKE_CLIP_SECS     = 2
WAKE_RMS_THRESHOLD = 80   # skip Whisper entirely on silent clips — saves CPU

# Smart recording tuning
SILENCE_THRESHOLD  = 100
SILENCE_WAIT_SECS  = 2
MAX_RECORD_SECS    = 15

def load_config() -> dict:
    """Load ~/.jarvis/config.json. Returns empty dict if missing."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    # Write with restricted permissions (owner read/write only)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()

def check_pin(entered: str, stored_hash: str) -> bool:
    return hash_pin(entered) == stored_hash

# Load config at startup
_config = load_config()

def get_gemini_key() -> str:
      return _config.get("gemini_api_key", "")

def get_pin_hash() -> str:
    return _config.get("pin_hash", "")

# Warn if no API key is configured
if not get_gemini_key():
    print("[warn] No Gemini API key found in ~/.jarvis/config.json")
    print("       To enable online mode, run this once in your terminal:")
    print("         python3 setup_jarvis.py")

if not get_pin_hash():
    print("[warn] No PIN set. Voice PIN lock is DISABLED.")
    print("       Add 'pin_hash' to ~/.jarvis/config.json to enable it.")

shutdown_event = threading.Event()

# ---------------- Online / Offline mode ----------------
# mode is a mutable dict so the background thread can toggle it
# and the GUI can read it without raw globals.
mode = {"online": False}  # default: offline (Ollama)

def is_online() -> bool:
    return mode["online"]

def set_mode_online():
    mode["online"] = True
    update_mode_indicator()

def set_mode_offline():
    mode["online"] = False
    update_mode_indicator()


# ---------------- Optional file-format readers (guarded) ----------------
try:
    import pdfplumber
except ImportError:
    pdfplumber = None
    print("[warn] pdfplumber not installed -> PDFs will be skipped (pip install pdfplumber)")

try:
    import docx
except ImportError:
    docx = None
    print("[warn] python-docx not installed -> .docx will be skipped (pip install python-docx)")

try:
    import openpyxl
except ImportError:
    openpyxl = None
    print("[warn] openpyxl not installed -> spreadsheets will be skipped (pip install openpyxl)")

try:
    import chromadb
except ImportError:
    chromadb = None
    print("[FATAL] chromadb not installed. Run: pip install chromadb")

print("Loading Whisper...")
whisper_model = whisper.load_model("base")


# ---------------- Embeddings ----------------
def embed_text(text: str):
    """Call Ollama's local embedding endpoint. Returns a list[float] or None on failure."""
    try:
        resp = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except requests.exceptions.RequestException as e:
        print(f"[embed error] {e}")
        return None


# ---------------- Chunking ----------------
def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP):
    words = text.split()
    if not words:
        return []
    chunks = []
    step = max(size - overlap, 1)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + size])
        if chunk.strip():
            chunks.append(chunk)
        if i + size >= len(words):
            break
    return chunks


def file_hash(path: str) -> str:
    st = os.stat(path)
    return hashlib.md5(f"{path}:{st.st_mtime}:{st.st_size}".encode()).hexdigest()


def extract_text(path: str, fname: str) -> str:
    try:
        if fname.endswith((".txt", ".py", ".md", ".json", ".yaml", ".yml")):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

        elif fname.endswith(".pages"):
            # macOS built-in textutil converts .pages to plain text
            result = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", path],
                capture_output=True, text=True, timeout=15
            )
            return result.stdout

        elif fname.endswith(".pdf") and pdfplumber:
            with pdfplumber.open(path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)

        elif fname.endswith(".docx") and docx:
            d = docx.Document(path)
            return "\n".join(p.text for p in d.paragraphs)

        elif fname.endswith((".xlsx", ".xls")) and openpyxl:
            wb = openpyxl.load_workbook(path, data_only=True)
            text = ""
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                text += f"\nSheet: {sheet}\n"
                for row in ws.iter_rows(values_only=True):
                    row_text = " | ".join(str(c) for c in row if c is not None)
                    if row_text.strip():
                        text += row_text + "\n"
            return text
    except Exception as e:
        print(f"[read error] {fname}: {e}")
    return ""


# ---------------- Vector index (ChromaDB) ----------------
_chroma_client = None
_collection = None

def get_collection():
    global _chroma_client, _collection
    if _collection is not None:
        return _collection
    os.makedirs(CHROMA_PATH, exist_ok=True)
    _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    _collection = _chroma_client.get_or_create_collection(
        name="jarvis_files", metadata={"hnsw:space": "cosine"}
    )
    return _collection


def load_index_cache() -> dict:
    if os.path.exists(INDEX_CACHE):
        try:
            with open(INDEX_CACHE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_index_cache(cache: dict):
    os.makedirs(os.path.dirname(INDEX_CACHE), exist_ok=True)
    with open(INDEX_CACHE, "w") as f:
        json.dump(cache, f)


def build_index():
    if chromadb is None:
        print("[FATAL] chromadb missing, cannot index.")
        return

    collection = get_collection()
    cache = load_index_cache()
    SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".jarvis"}

    indexed, skipped = 0, 0
    for root_dir, dirs, files in os.walk(FILES_FOLDER):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            path = os.path.join(root_dir, fname)
            try:
                size_mb = os.path.getsize(path) / (1024 * 1024)
                if size_mb > MAX_FILE_MB:
                    print(f"[skip] {fname} is too large ({size_mb:.1f} MB)")
                    continue
            except OSError:
                continue

            if not fname.endswith((".txt", ".py", ".md", ".json", ".yaml",
                                    ".yml", ".pdf", ".docx", ".xlsx", ".xls", ".pages")):
                continue

            h = file_hash(path)
            if cache.get(path) == h:
                skipped += 1
                continue

            text = extract_text(path, fname)
            if not text.strip():
                continue

            try:
                  collection.delete(where={"source": path})
            except Exception as e:
                print(f"[index] Warning: could not delete old chunks for {fname}: {e}")

            chunks = chunk_text(text)
            for i, chunk in enumerate(chunks):
                vec = embed_text(chunk)
                if vec is None:
                    continue
                collection.upsert(
                    ids=[f"{path}::{i}"],
                    embeddings=[vec],
                    documents=[chunk],
                    metadatas=[{"source": path}],
                )
            cache[path] = h
            indexed += 1

    save_index_cache(cache)
    print(f"[index] {indexed} files (re)indexed, {skipped} unchanged/skipped.")


def retrieve_context(question: str, k: int = TOP_K) -> str:
    if chromadb is None:
        return ""
    collection = get_collection()
    qvec = embed_text(question)
    if qvec is None:
        return ""
    try:
        results = collection.query(query_embeddings=[qvec], n_results=k)
    except Exception as e:
        print(f"[query error] {e}")
        return ""

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    if not docs:
        return ""

    blocks = []
    for doc, meta in zip(docs, metas):
        src = os.path.basename(meta.get("source", "unknown"))
        blocks.append(f"--- {src} ---\n{doc}")
    return "\n\n".join(blocks)


# ---------------- GUI ----------------
root = tk.Tk()
root.title("Jarvis")
root.geometry(f"{W}x{H}")
root.configure(bg="#0a0a14")
root.attributes("-topmost", True)
root.attributes("-alpha", 0.93)
root.overrideredirect(True)

sw = root.winfo_screenwidth()
sh = root.winfo_screenheight()
root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

canvas = tk.Canvas(root, width=W, height=H, bg="#0a0a14", highlightthickness=0)
canvas.pack()

status_var = tk.StringVar(value="Sleeping")
status_lbl = tk.Label(root, textvariable=status_var,
    fg="#6655cc", bg="#0a0a14", font=("Arial", 13, "bold"))
status_lbl.place(x=W//2, y=H-25, anchor="center")

subtitle = tk.Text(root, height=3, width=46, bg="#0a0a14", fg="#aaaacc",
    font=("Arial", 11), relief="flat", wrap="word", state="disabled",
    insertbackground="#0a0a14")
subtitle.place(x=W//2, y=H-75, anchor="center")
subtitle.tag_config("jarvis_word", foreground="#ffffff")
subtitle.tag_config("jarvis_spoken", foreground="#6655cc")
subtitle.tag_config("user_text", foreground="#5dcaa5")
subtitle.tag_config("label", foreground="#444466")

# Mode indicator — top-left corner. Click to toggle. Green = online, grey = offline.
mode_var = tk.StringVar(value="● OFFLINE")
mode_lbl = tk.Label(root, textvariable=mode_var,
    fg="#555577", bg="#0a0a14", font=("Arial", 9, "bold"), cursor="hand2")
mode_lbl.place(x=12, y=12)

def toggle_mode_click(event=None):
    """Click the mode pill to toggle online/offline (in addition to voice command)."""
    if is_online():
        set_mode_offline()
        # Speak on a thread so the GUI doesn't freeze
        threading.Thread(target=lambda: speak("Switched to offline mode."), daemon=True).start()
    else:
        if not get_gemini_key():
            threading.Thread(
                target=lambda: speak("No Gemini API key configured. Please add it to your config file."),
                daemon=True
            ).start()
            return
        set_mode_online()
        threading.Thread(target=lambda: speak("Switched to online mode."), daemon=True).start()

mode_lbl.bind("<Button-1>", toggle_mode_click)

def update_mode_indicator():
    """Always schedule the GUI update on the main thread."""
    def _update():
        if is_online():
            mode_var.set("● ONLINE")
            mode_lbl.config(fg="#2ecc71")
        else:
            mode_var.set("● OFFLINE")
            mode_lbl.config(fg="#555577")
    root.after(0, _update)

def set_subtitle(speaker, text, highlight_word=None):
      def _do():
          subtitle.config(state="normal")
          subtitle.delete("1.0", "end")
          if speaker == "jarvis":
              subtitle.insert("end", "Jarvis: ", "label")
              for word in text.split():
                  tag = "jarvis_spoken" if word == highlight_word else "jarvis_word"
                  subtitle.insert("end", word + " ", tag)
          elif speaker == "user":
              subtitle.insert("end", "You: ", "label")
              subtitle.insert("end", text, "user_text")
          elif speaker == "idle":
              subtitle.insert("end", text, "jarvis_word")
          subtitle.config(state="disabled")
      root.after(0, _do)

def on_close():
      shutdown_event.set()
      root.destroy()
  
close_btn = tk.Button(root, text="✕", command=on_close,
    fg="#555", bg="#0a0a14", relief="flat", font=("Arial", 14),
cursor="hand2")
close_btn.place(x=W-30, y=10)

cx, cy = W//2, H//2 - 20
state = {"current": "sleeping"}

COLORS = {
    "sleeping":  [(0x22,0x22,0x44),(0x33,0x22,0x66),(0x11,0x11,0x33)],
    "listening": [(0x53,0x4a,0xb7),(0x7f,0x77,0xdd),(0x37,0x30,0x90)],
    "thinking":  [(0x1d,0x9e,0x75),(0x5d,0xca,0xa5),(0x0f,0x6e,0x56)],
    "answering": [(0xd8,0x5a,0x30),(0xf0,0x99,0x7b),(0x99,0x3c,0x1d)],
    "locked":    [(0x88,0x11,0x11),(0xcc,0x22,0x22),(0x55,0x00,0x00)],
}

blobs = []
for i in range(8):
    b = canvas.create_oval(0,0,1,1, fill="#222244", outline="")
    blobs.append(b)
core_oval = canvas.create_oval(0,0,1,1, fill="#334466", outline="")
core_text = canvas.create_text(cx, cy, text="J", fill="white",
    font=("Arial", 36, "bold"))

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i]-c1[i])*t) for i in range(3))

def to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)

def animate():
    t = time.time()
    cur = state["current"]
    cols = COLORS.get(cur, COLORS["sleeping"])
    speed = {"sleeping":0.4,"listening":1.8,"thinking":2.5,"answering":1.2,"locked":0.6}.get(cur, 0.4)
    for i, blob in enumerate(blobs):
        angle = (2 * math.pi * i / 8) + t * speed
        wobble = 1 + 0.22 * math.sin(t * speed * 1.3 + i * 1.1)
        rx = (75 + 18 * math.sin(t * speed * 0.7 + i)) * wobble
        ry = (75 + 18 * math.cos(t * speed * 0.9 + i * 1.3)) * wobble
        bx = cx + rx * math.cos(angle) * 0.55
        by = cy + ry * math.sin(angle) * 0.55
        br = 55 + 20 * math.sin(t * speed + i * 0.8)
        ci = i % len(cols)
        frac = 0.5 + 0.5 * math.sin(t * speed * 0.6 + i)
        col = lerp_color(cols[ci], cols[(ci+1)%len(cols)], frac)
        canvas.coords(blob, bx-br, by-br, bx+br, by+br)
        canvas.itemconfig(blob, fill=to_hex(col))
    cr = 62 + 8 * math.sin(t * speed * 1.1)
    canvas.coords(core_oval, cx-cr, cy-cr, cx+cr, cy+cr)
    canvas.itemconfig(core_oval, fill=to_hex(cols[0]))
    canvas.tag_raise(core_text)
    root.after(40, animate)

def set_state(s, msg=""):
    def _do():
          state["current"] = s
          labels = {
              "sleeping":  "Sleeping",
              "listening": "Listening...",
              "thinking":  "Thinking...",
              "answering": "Speaking...",
              "locked":    "🔒 PIN Required",
          }
          status_var.set(labels.get(s, s))
    root.after(0, _do)

def speak(text):
    print(f"Jarvis: {text}")
    set_state("answering")
    words = text.split()
    ms_per_word = int(60000 / SPEAK_RATE)

    def highlight_words():
        for i, word in enumerate(words):
            lines = " ".join(words[max(0,i-4):i+5])
            set_subtitle("jarvis", lines, highlight_word=word)
            time.sleep(ms_per_word / 1000)

    def run_say():
        subprocess.run(["say", "-v", VOICE, "-r", str(SPEAK_RATE), text])

    t1 = threading.Thread(target=highlight_words)
    t2 = threading.Thread(target=run_say)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


# ---------------- Audio capture ----------------

def record_audio(seconds=MAX_RECORD_SECS, smart=False):
    import audioop
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS,
        rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK)

    frames = []
    silence_chunks = 0
    spoke_once = False
    max_chunks = int(SAMPLE_RATE / CHUNK * (MAX_RECORD_SECS if smart else seconds))
    silence_limit = int(SAMPLE_RATE / CHUNK * SILENCE_WAIT_SECS)

    for _ in range(max_chunks):
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)
        if smart:
            rms = audioop.rms(data, 2)
            if rms > SILENCE_THRESHOLD:
                spoke_once = True
                silence_chunks = 0
            else:
                if spoke_once:
                    silence_chunks += 1
                    if silence_chunks >= silence_limit:
                        break

    stream.stop_stream()
    stream.close()
    sample_width = pa.get_sample_size(pyaudio.paInt16)
    pa.terminate()

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(sample_width)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return tmp.name

def transcribe(audio_path):
    result = whisper_model.transcribe(audio_path, language="en")
    os.unlink(audio_path)
    return result["text"].strip().lower()


# ---------------- PIN lock ----------------
# PIN digits are spoken as individual words ("1 2 3 4").
# Whisper transcribes them as words ("one two three four") or as numerals.
# We normalise both to a digit string before comparing the hash.

_WORD_TO_DIGIT = {
    "zero":"0", "one":"1", "two":"2",  "three":"3", "four":"4",
    "five":"5", "six":"6", "seven":"7","eight":"8", "nine":"9",
    # common Whisper mishearings
    "to":"2", "too":"2", "for":"4", "ate":"8", "won":"1", "tree":"3",
}

def normalize_pin(text: str) -> str:
    """Convert spoken PIN ('one two three four' or '1 2 3 4') to digit string."""
    digits = []
    for token in text.strip().split():
        token = token.strip(".,!?")
        if token in _WORD_TO_DIGIT:
            digits.append(_WORD_TO_DIGIT[token])
        elif token.isdigit():
            digits.extend(list(token))   # e.g. "1234" -> ["1","2","3","4"]
    return "".join(digits)

def verify_pin() -> bool:
    """
    Ask user to speak their PIN.
    Returns True if correct (or if no PIN is configured).
    Gives 2 attempts then locks out.
    """
    stored = get_pin_hash()
    if not stored:
        return True   # PIN lock not configured → always allow

    set_state("locked")
    speak("Please say your PIN.")

    for attempt in range(2):
        set_subtitle("idle", "🔒 Say your PIN...")
        audio = record_audio(smart=True)
        spoken = transcribe(audio)
        print(f"[PIN] Whisper heard: {spoken}")
        pin_digits = normalize_pin(spoken)
        print(f"[PIN] Normalised: {pin_digits}")

        if check_pin(pin_digits, stored):
            speak("Access granted.")
            return True
        else:
            if attempt == 0:
                speak("Incorrect. Try again.")
            else:
                speak("Wrong PIN. Going back to sleep.")

    set_state("sleeping")
    return False

def build_prompt(question: str, retrieved: str) -> str:
      if retrieved:
          return (
              "You are Jarvis, a helpful AI voice assistant. "
              "Below are relevant excerpts from the user's personal files.\n"
              "If the question is about the files, answer using the excerpts.\n"
              "If the excerpts don't contain the answer, say: "
              "\"I couldn't find that in your files.\"\n"
              "If the question is general conversation (like 'how are you' "
              "or 'tell me a joke'), ignore the excerpts and respond naturally.\n"
              "Keep answers short and conversational — 1 to 3 sentences.\n\n"
              f"FILE EXCERPTS:\n{retrieved}\n\n"
              f"USER: {question}\n\nJARVIS:"
          )
      return (
          "You are Jarvis, a helpful AI voice assistant. "
          "Respond naturally and conversationally. "
          "Keep it short — 1 to 3 sentences.\n\n"
          f"USER: {question}\n\nJARVIS:"
      )
# ---------------- Gemini (online) generation ----------------
def ask_gemini(question: str) -> str:
    """
    Call Gemini 1.5 Flash with RAG context injection.
    Returns the response text, or an error string on failure.
    Gemini API is stateless — no turn history is passed (unlike Ollama's context array).
    """
    api_key = get_gemini_key()
    if not api_key:
        return "Online mode is not configured. Please add your Gemini API key to the config file."

    retrieved = retrieve_context(question)
    prompt = build_prompt(question, retrieved)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 256,
        }
    }

    try:
        resp = requests.post(
            f"{GEMINI_API_URL}?key={api_key}",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "").strip()
        return "I got a response from Gemini but couldn't parse it."
    except requests.exceptions.ConnectionError:
        return "I can't reach the internet. Try switching to offline mode."
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code
        if code == 400:
            return "Bad API request. Check your Gemini API key in the config file."
        elif code == 429:
            return "Gemini rate limit hit. Please wait a moment and try again."
        return f"Gemini API returned error {code}."
    except requests.exceptions.RequestException as e:
        print(f"[gemini error] {e}")
        return "Online mode failed. Try switching to offline mode."


# ---------------- Ollama (offline) generation ----------------
def ask_ollama(question: str, ollama_context: list):
    retrieved = retrieve_context(question)
    prompt = build_prompt(question, retrieved)
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "context": ollama_context,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.2},
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", ""), data.get("context", ollama_context)
    except requests.exceptions.RequestException as e:
        print(f"[ollama error] {e}")
        return "Sorry, I couldn't reach the local model. Is Ollama running?", ollama_context


# ---------------- Unified answer function ----------------
def ask(question: str, ollama_context: list):
    """
    Route to Gemini (online) or Ollama (offline) based on current mode.
    Always returns (answer_text, updated_ollama_context).
    In online mode, ollama_context is returned unchanged (Gemini is stateless).
    """
    if is_online():
        answer = ask_gemini(question)
        return answer, ollama_context
    else:
        return ask_ollama(question, ollama_context)


# ---------------- Voice command detection ----------------
_SWITCH_ONLINE_PHRASES  = {"switch to online", "go online", "use online", "online mode"}
_SWITCH_OFFLINE_PHRASES = {"switch to offline", "go offline", "use offline", "offline mode"}

def detect_mode_switch(text: str):
    """
    Returns "online", "offline", or None depending on the utterance.
    """
    t = text.lower().strip()
    for phrase in _SWITCH_ONLINE_PHRASES:
        if phrase in t:
            return "online"
    for phrase in _SWITCH_OFFLINE_PHRASES:
        if phrase in t:
            return "offline"
    return None

# ---------------- Main loop ----------------
def jarvis_loop():
    print("Indexing local files (first run may take a while)...")
    build_index()

    ollama_context = []
    set_state("sleeping")
    set_subtitle("idle", 'Say "Jarvis" to wake up')

    while not shutdown_event.is_set():
        # ---- Wake word detection ----
        # Record a short clip; skip Whisper entirely if the audio is silent.
        # This saves CPU — Whisper only runs when there's actual speech.
        audio = record_audio(WAKE_CLIP_SECS)

        # RMS pre-check: read the wav and compute energy before calling Whisper
        import audioop
        with wave.open(audio, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        clip_rms = audioop.rms(raw, 2)
        if clip_rms < WAKE_RMS_THRESHOLD:
            os.unlink(audio)
            continue

        heard = transcribe(audio)
        if WAKE_WORD not in heard:
            continue

        # ---- PIN verification ----
        if not verify_pin():
            set_state("sleeping")
            set_subtitle("idle", 'Say "Jarvis" to wake up')
            continue

        # ---- Greet and listen ----
        speak("Yes?")
        time.sleep(0.4)
        for attempt in range(MAX_RETRIES):
            set_state("listening")
            set_subtitle("idle", f"Listening... ({attempt+1}/{MAX_RETRIES})")
            audio = record_audio(smart=True)
            question = transcribe(audio)
            print(f"[DEBUG] Whisper heard: {question}")

            if not question or len(question) <= 3:
                if attempt < MAX_RETRIES - 1:
                    speak("Didn't catch that, try again.")
                    time.sleep(0.3)
                else:
                    speak("Going back to sleep.")
                continue

            print(f"You: {question}")
            set_subtitle("user", question)

            # ---- Mode switch command? ----
            switch = detect_mode_switch(question)
            if switch == "online":
                if not get_gemini_key():
                    speak("Online mode needs a Gemini API key. Please add it to your config file.")
                else:
                    set_mode_online()
                    speak("Switched to online mode. Using Gemini now.")
                set_subtitle("idle", "Anything else?")
                break

            elif switch == "offline":
                set_mode_offline()
                speak("Switched to offline mode. Using local Ollama now.")
                set_subtitle("idle", "Anything else?")
                break

            # ---- Regular question ----
            set_state("thinking")
            answer, ollama_context = ask(question, ollama_context)
            speak(answer)
            set_subtitle("idle", "Anything else?")
            break

        # Back to sleep
        set_state("sleeping")
        set_subtitle("idle", 'Say "Jarvis" to wake up')


if __name__ == "__main__":
    thread = threading.Thread(target=jarvis_loop, daemon=True)
    thread.start()
    animate()
    root.mainloop()
