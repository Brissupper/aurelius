# Aurelius 📚🎧

> An AI-powered platform that turns any book or PDF into a narrated audiobook.
> Built with Python, FastAPI, and free open-source tools.

![Status](https://img.shields.io/badge/status-active-brightgreen)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-gold)

---

## What it does

1. **Upload** any PDF — books, articles, documents
2. **Store** it in your personal library with title, author, and category
3. **Narrate** — pick a voice and generate audio on demand
4. **Listen** — stream chapter by chapter with full playback controls

---

## Features

- Personal library with grid and list views
- Filter by category, status, search by title or author
- 6 narrator voices (American & British, male & female)
- Live progress bar during audio generation
- Speed control (0.75× to 2×), skip back/forward
- Hybrid TTS engine — auto-selects fastest available:
  - **Google Cloud TTS** — ~30 seconds per book (needs free API key)
  - **Piper TTS** — ~10 minutes per book (local, no API key)
  - **Kokoro TTS** — highest quality, fully offline fallback

---

## Quick Start

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/aurelius.git
cd aurelius
```

### 2. Set up Python environment
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Mac / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Download Kokoro model files (one-time)
Download both files into your `aurelius/` folder:
- [kokoro-v1.0.onnx](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) (~310 MB)
- [voices-v1.0.bin](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) (~18 MB)

### 4. (Optional) Set up faster TTS

**Google Cloud TTS — ~30 seconds per book:**
```bash
# Get free key at console.cloud.google.com → Enable Text-to-Speech API
set GOOGLE_TTS_API_KEY=your-key-here   # Windows
export GOOGLE_TTS_API_KEY=your-key-here # Mac/Linux
```

**Piper TTS — ~10 minutes per book:**
```bash
mkdir piper-models
# Download en_US-amy-medium.onnx + .onnx.json from:
# https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/amy/medium
```

### 5. Start the server
```bash
uvicorn main:app --reload --port 8000
```

### 6. Open the app
Visit [http://localhost:8000](http://localhost:8000)

---

## Project Structure

```
aurelius/
├── main.py          # FastAPI backend — all API routes
├── worker.py        # Background job processor
├── tts_engine.py    # Hybrid TTS system (Google / Piper / Kokoro)
├── models.py        # SQLite database schema
├── engine_v2.py     # Standalone CLI engine (for testing)
├── app.html         # Full frontend (served by FastAPI)
├── requirements.txt
├── .env.example
└── README.md
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/` | Web interface |
| `POST` | `/api/books/upload` | Upload PDF to library |
| `GET`  | `/api/books` | List all books |
| `GET`  | `/api/books/{id}` | Book details + chapters |
| `POST` | `/api/books/{id}/narrate` | Start audio generation |
| `GET`  | `/api/jobs/{id}` | Poll job progress |
| `GET`  | `/api/audio/{chapter_id}` | Stream chapter audio |
| `DELETE` | `/api/books/{id}` | Delete book + audio |
| `GET`  | `/api/tts-status` | Check active TTS engine |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, FastAPI, Uvicorn |
| Database | SQLite (built-in, zero config) |
| PDF extraction | PyMuPDF |
| TTS (cloud) | Google Cloud Text-to-Speech |
| TTS (local fast) | Piper TTS |
| TTS (local quality) | Kokoro ONNX |
| Frontend | Vanilla HTML/CSS/JS (no framework) |

---

## Roadmap

- [x] Phase 1 — PDF to audio engine
- [x] Phase 2 — FastAPI backend + SQLite
- [x] Phase 3 — Web UI (library, upload, player)
- [ ] Phase 4 — User accounts + cloud hosting

---

## License

MIT — free to use, modify, and build on.
