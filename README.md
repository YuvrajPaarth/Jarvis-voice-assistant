# 🤖 Jarvis — Local Voice Assistant with RAG

A wake-word-activated voice assistant that runs **entirely on your Mac** — no cloud, no subscriptions, no data leaving your machine (unless you switch to online mode).

> 📹 **[Watch the demo →](#)** *(replace this with your YouTube unlisted link)*

---

## What it does

Say **"Jarvis"** and it wakes up. Ask it anything — about your own files, your notes, your code, your PDFs — and it finds the relevant context and answers conversationally. It also handles general questions, tells jokes, and switches between a fully local model and Gemini on demand, all by voice.

- **Offline mode** — Uses Llama 3.2 (via Ollama) running 100% locally. Zero internet required.
- **Online mode** — Uses Gemini 3.5 Flash for faster, smarter answers with the same local RAG context injected.
- **Switch by voice** — Say *"switch to online"* or *"switch to offline"* at any time.
- **RAG over your files** — Drop any `.txt`, `.pdf`, `.docx`, `.py`, `.md`, `.xlsx`, or `.pages` file into `~/jarvis_brain/` and Jarvis can answer questions about them.
- **PIN lock** — Speak your PIN digits to unlock. Wrong PIN = back to sleep.
- **Animated GUI** — Frameless floating window with a live blob animation that changes colour by state (sleeping / listening / thinking / speaking / locked).

---

## Architecture

```
Microphone
    │
    ▼
RMS energy gate ──── silent? ──► skip (saves CPU)
    │
    ▼ speech detected
Whisper (local, base model)
    │
    ▼ "jarvis" heard
PIN verification (SHA-256 hashed, spoken digits)
    │
    ▼ unlocked
Whisper (full question)
    │
    ├──► nomic-embed-text (Ollama) ──► ChromaDB cosine query
    │         top-4 relevant chunks from ~/jarvis_brain/
    │
    ▼
Prompt builder (question + retrieved context)
    │
    ├── OFFLINE ──► Ollama  (llama3.2:3b, local)
    └── ONLINE  ──► Gemini  (gemini-3.5-flash-lite, API)
              │
              ▼
        macOS `say` TTS  +  word-highlight subtitle
```

**Why this shape:**
- Ollama handles both generation and embeddings locally — no API keys needed in offline mode.
- ChromaDB stores chunk embeddings on disk so files are only re-embedded when they change, not on every run.
- Word-based chunking with overlap keeps the dependency tree light — no LangChain or LlamaIndex needed. Good tradeoff when Ollama + Whisper + Chroma are all resident in memory at once on Apple Silicon.
- Only the top-k relevant chunks are injected into the prompt, not full file contents — this is the fix for context window overflow on local models.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Speech-to-text | [OpenAI Whisper](https://github.com/openai/whisper) (base, local) |
| Local LLM | [Ollama](https://ollama.com) — `llama3.2:3b` |
| Embeddings | Ollama — `nomic-embed-text` |
| Vector store | [ChromaDB](https://www.trychroma.com) (persistent, on-disk) |
| Online LLM | [Gemini 3.5 Flash Lite](https://ai.google.dev) |
| Audio | PyAudio |
| TTS | macOS built-in `say` (Samantha voice) |
| GUI | Tkinter (frameless, animated) |
| Security | SHA-256 PIN hashing, config `chmod 600` |

---

## Setup

### Prerequisites

```bash
# 1. Install PortAudio (required by PyAudio)
brew install portaudio

# 2. Install Ollama
# Download from https://ollama.com, then pull the required models:
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

### Install Python dependencies

```bash
git clone https://github.com/YOUR_USERNAME/jarvis-voice-assistant.git
cd jarvis-voice-assistant
pip install -r requirements.txt
```

### Configure API key and PIN (one-time)

```bash
python3 setup_jarvis.py
```

This saves your Gemini API key and optional voice PIN to `~/.jarvis/config.json` with owner-only permissions (`chmod 600`). The PIN is stored as a SHA-256 hash — never plain text.

> **Online mode is optional.** If you skip the API key, offline mode works fully without it.  
> Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com).

### Add your files

```bash
mkdir -p ~/jarvis_brain
# Drop any .txt, .pdf, .docx, .py, .md, .xlsx, or .pages files here
```

### Run

```bash
python3 Jarvis.py
```

Jarvis will index your files on first run (this takes a minute depending on how many files you have), then start listening for **"Jarvis"**.

---

## Usage

| You say | What happens |
|---|---|
| *"Jarvis"* | Wakes up, asks for PIN if configured |
| *"What's in my notes?"* | RAG search over `~/jarvis_brain/`, answers from your files |
| *"Tell me a joke"* | Ignores file context, responds naturally |
| *"Switch to online"* | Switches to Gemini for the rest of the session |
| *"Switch to offline"* | Switches back to local Ollama |
| *"Go back to sleep"* | (just stop talking — it times out automatically) |

You can also **click the ● OFFLINE / ● ONLINE pill** in the top-left corner of the GUI to toggle modes.

---

## Tuning

All knobs are constants at the top of `Jarvis.py`:

| Constant | Default | Effect |
|---|---|---|
| `WAKE_RMS_THRESHOLD` | `80` | Lower if it misses your voice; raise if ambient noise triggers it |
| `SILENCE_THRESHOLD` | `100` | Controls when smart recording stops after you finish speaking |
| `SILENCE_WAIT_SECS` | `2` | How long to wait after silence before cutting the recording |
| `TOP_K` | `4` | Number of file chunks injected into each prompt |
| `CHUNK_WORDS` | `220` | Words per RAG chunk |
| `MODEL` | `llama3.2:3b` | Swap for any model you have pulled in Ollama |

---

## Project Structure

```
jarvis-voice-assistant/
├── Jarvis.py          # Main application
├── setup_jarvis.py    # One-time config setup (API key + PIN)
├── requirements.txt   # Python dependencies
└── README.md
```

Files created at runtime (not in repo):
```
~/.jarvis/
├── config.json        # API key + PIN hash (chmod 600)
├── chroma_db/         # ChromaDB vector store
└── index_cache.json   # File hash cache (skip re-indexing unchanged files)

~/jarvis_brain/        # Your personal files for RAG
```

---

## Requirements

- macOS (uses `say` for TTS and `textutil` for `.pages` files)
- Python 3.10+
- Ollama running locally (`ollama serve`)
- Microphone

---

## License

MIT
