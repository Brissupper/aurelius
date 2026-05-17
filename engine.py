"""
Aurelius Engine v2 — Kokoro TTS, CPU-optimized
Key fixes:
  - Chunk size reduced to 150 chars (Kokoro processes these in ~5-8 sec on CPU)
  - Progress bar shows real-time ETA
  - Warnings suppressed

Usage:
    python engine_v2.py --input input/kybalion.pdf --no-ai
    python engine_v2.py --input input/kybalion.pdf --voice am_adam --no-ai
"""

import os, re, sys, json, argparse, logging, warnings, time
import numpy as np
import soundfile as sf
from pathlib import Path
from datetime import datetime, timedelta
import fitz
from anthropic import Anthropic

# Suppress all Kokoro internal warnings
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/engine.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("aurelius")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CHUNK_SIZE        = 150    # Small chunks = fast Kokoro processing on CPU
OUTPUT_DIR        = Path("output")
MODEL_PATH        = "kokoro-v1.0.onnx"
VOICES_PATH       = "voices-v1.0.bin"
DEFAULT_VOICE     = "af_bella"


# =============================================================================
#  STEP 1 — PDF extraction + chapter detection
# =============================================================================

def extract_text(pdf_path: str) -> dict:
    log.info(f"Opening PDF: {pdf_path}")
    doc        = fitz.open(pdf_path)
    pages_text = [page.get_text("text").strip() for page in doc]
    log.info(f"Extracted {len(pages_text)} pages")
    chapters   = detect_chapters(pages_text, doc.metadata.get("title", Path(pdf_path).stem))
    doc.close()
    return chapters


_STOPWORDS = {
    "OF THE", "BY THE", "IN THE", "TO THE", "FROM THE", "AND THE",
    "FOR THE", "AT THE", "ON THE", "WITH THE", "AS THE", "IS THE",
}

def _is_chapter_heading(line: str) -> bool:
    s = line.strip()
    if len(s) < 5 or len(s) > 65:
        return False
    if not re.match(r'^[A-Z][A-Z\s]+$', s):
        return False
    if s in _STOPWORDS:
        return False
    if re.match(r'^[IVXLC]+$', s) and len(s) < 5:
        return False
    return True


def detect_chapters(pages: list, book_title: str) -> dict:
    standard_patterns = [
        r"^(CHAPTER\s+[IVXLC\d]+[\.\:]?\s*.*)$",
        r"^(Chapter\s+\d+[\.\:]?\s*.*)$",
        r"^(PART\s+[IVXLC\d]+[\.\:]?\s*.*)$",
    ]
    full_text = "\n".join(pages)
    lines     = full_text.split("\n")

    # Pass 1: standard patterns
    chapters, current_title, current_text, found_any = [], "Introduction", [], False
    for line in lines:
        stripped, is_heading = line.strip(), False
        for pat in standard_patterns:
            if re.match(pat, stripped, re.MULTILINE):
                if current_text:
                    text = clean_raw_text(" ".join(current_text))
                    if len(text) > 200:
                        chapters.append({"title": current_title, "text": text})
                current_title, current_text, is_heading, found_any = (
                    stripped.title(), [], True, True)
                break
        if not is_heading and stripped:
            current_text.append(stripped)
    if current_text:
        text = clean_raw_text(" ".join(current_text))
        if text:
            chapters.append({"title": current_title, "text": text})
    if found_any and chapters:
        log.info(f"Detected {len(chapters)} chapter(s) [standard]")
        return {"title": book_title, "chapters": chapters}

    # Pass 2: all-caps heading detection (Kybalion style)
    chapters, current_title, current_text, found_any = [], book_title, [], False
    for line in lines:
        stripped = line.strip()
        if _is_chapter_heading(stripped):
            if current_text:
                text = clean_raw_text(" ".join(current_text))
                if len(text) > 300:
                    chapters.append({"title": current_title, "text": text})
            current_title, current_text, found_any = stripped.title(), [], True
        elif stripped:
            current_text.append(stripped)
    if current_text:
        text = clean_raw_text(" ".join(current_text))
        if text:
            chapters.append({"title": current_title, "text": text})
    if found_any and len(chapters) > 1:
        log.info(f"Detected {len(chapters)} chapter(s) [all-caps]")
        return {"title": book_title, "chapters": chapters}

    log.warning("No chapters found — treating as single chapter")
    return {"title": book_title,
            "chapters": [{"title": book_title, "text": clean_raw_text(full_text)}]}


def clean_raw_text(text: str) -> str:
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\b\d{1,3}\b(?=\s)", "", text)
    text = re.sub(r"[^\w\s.,!?;:'\"\-()\u2018\u2019\u201c\u201d]", " ", text)
    return text.strip()


# =============================================================================
#  STEP 2 — AI cleanup
# =============================================================================

def ai_clean_chapter(client, text, title):
    log.info(f"  AI cleaning: '{title}'")
    resp = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=4096,
        messages=[{"role": "user", "content":
            f"Clean this PDF text for audiobook narration. Fix OCR errors, "
            f"join broken sentences, remove footnote markers and page numbers. "
            f"Keep ALL content. Output ONLY cleaned text.\n\nChapter: {title}\n\n{text}"}])
    return resp.content[0].text.strip()


# =============================================================================
#  STEP 3 — Kokoro TTS with progress bar
# =============================================================================

def split_into_chunks(text: str, size: int = CHUNK_SIZE) -> list:
    sentences    = re.split(r'(?<=[.!?])\s+', text)
    chunks, curr = [], ""
    for s in sentences:
        if len(curr) + len(s) + 1 <= size:
            curr += (" " if curr else "") + s
        else:
            if curr:
                chunks.append(curr.strip())
            if len(s) > size:
                chunks += [s[i:i+size] for i in range(0, len(s), size)]
                curr = ""
            else:
                curr = s
    if curr:
        chunks.append(curr.strip())
    return [c for c in chunks if c.strip()]


def print_progress(done: int, total: int, start_time: float, chapter_title: str) -> None:
    """Print a clean progress bar with ETA to stdout."""
    pct     = done / total
    elapsed = time.time() - start_time
    eta_sec = (elapsed / done * (total - done)) if done > 0 else 0
    eta_str = str(timedelta(seconds=int(eta_sec)))
    bar_len = 35
    filled  = int(bar_len * pct)
    bar     = "#" * filled + "-" * (bar_len - filled)
    # \r rewrites same line; flush forces immediate display
    print(f"\r  [{bar}] {done}/{total}  ETA {eta_str}  ", end="", flush=True)


def tts_kokoro(text: str, output_path: str, voice: str, kokoro) -> None:
    chunks     = split_into_chunks(text)
    total      = len(chunks)
    all_audio  = []
    sample_rate = 24000
    failed     = 0
    start_time = time.time()

    log.info(f"  Kokoro TTS: {total} chunks -> {Path(output_path).name}")
    print(f"  Processing {total} chunks...", flush=True)

    for i, chunk in enumerate(chunks):
        try:
            # Redirect stderr to suppress Kokoro's internal print warnings
            import io as _io
            _old = sys.stderr
            sys.stderr = _io.StringIO()
            samples, sr = kokoro.create(chunk, voice=voice, speed=1.0, lang="en-us")
            sys.stderr = _old
            all_audio.append(samples)
            sample_rate = sr
        except Exception as e:
            if '_old' in dir():
                sys.stderr = _old
            failed += 1

        print_progress(i + 1, total, start_time, "")

    print()  # newline after progress bar

    elapsed = time.time() - start_time
    log.info(f"  TTS complete in {timedelta(seconds=int(elapsed))} | {failed} chunks skipped")

    if not all_audio:
        log.error("  No audio generated.")
        return

    combined = np.concatenate(all_audio)
    wav_path = output_path.replace(".mp3", ".wav")
    sf.write(wav_path, combined, sample_rate)

    try:
        from pydub import AudioSegment
        AudioSegment.from_wav(wav_path).export(output_path, format="mp3", bitrate="128k")
        os.remove(wav_path)
        size_mb = Path(output_path).stat().st_size / 1_000_000
        log.info(f"  Saved MP3: {Path(output_path).name} ({size_mb:.1f} MB)")
    except Exception:
        final = output_path.replace(".mp3", ".wav")
        if wav_path != final:
            try:
                os.rename(wav_path, final)
            except Exception:
                pass
        size_mb = Path(final).stat().st_size / 1_000_000
        log.info(f"  Saved WAV: {Path(final).name} ({size_mb:.1f} MB)")
        log.info("  Tip: install pydub + ffmpeg to get MP3 output")


# =============================================================================
#  STEP 4 — Save helpers
# =============================================================================

def save_text_output(book, output_dir):
    text_dir = output_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    for i, ch in enumerate(book["chapters"]):
        safe = re.sub(r"[^\w\s-]", "", ch["title"])[:40].strip()
        (text_dir / f"{i+1:02d}_{safe}.txt").write_text(ch["text"], encoding="utf-8")
    log.info(f"Text saved -> {text_dir}/")


def save_manifest(book, audio_files, output_dir):
    manifest = {
        "title":     book["title"],
        "generated": datetime.now().isoformat(),
        "chapters":  [
            {"number": i+1, "title": ch["title"],
             "audio":  str(audio_files[i]) if i < len(audio_files) else None,
             "chars":  len(ch["text"])}
            for i, ch in enumerate(book["chapters"])
        ]
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Manifest saved.")


# =============================================================================
#  MAIN
# =============================================================================

def run(pdf_path: str, voice: str, use_ai: bool) -> None:
    # Check model files
    missing = [f for f in [MODEL_PATH, VOICES_PATH] if not Path(f).exists()]
    if missing:
        log.error("Kokoro model files not found. Download into your aurelius/ folder:")
        log.error("  https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0")
        log.error("  Files: kokoro-v1.0.onnx + voices-v1.0.bin")
        sys.exit(1)

    from kokoro_onnx import Kokoro
    log.info("Loading Kokoro model (takes ~3 seconds)...")
    kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    log.info(f"Kokoro ready. Voice: {voice}")

    pdf_path   = Path(pdf_path)
    book_slug  = re.sub(r"[^\w]", "_", pdf_path.stem.lower())
    output_dir = OUTPUT_DIR / book_slug
    audio_dir  = output_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "text").mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info(f"  AURELIUS v2  |  {pdf_path.name}  |  {voice}")
    log.info("=" * 60)

    book = extract_text(str(pdf_path))
    log.info(f"Book: '{book['title']}' — {len(book['chapters'])} chapter(s)")
    for i, ch in enumerate(book["chapters"]):
        log.info(f"  {i+1:02d}. {ch['title']}  ({len(ch['text'])} chars)")

    if use_ai:
        if not ANTHROPIC_API_KEY:
            log.warning("ANTHROPIC_API_KEY not set — skipping AI cleanup")
        else:
            client = Anthropic(api_key=ANTHROPIC_API_KEY)
            for ch in book["chapters"]:
                ch["text"] = ai_clean_chapter(client, ch["text"], ch["title"])

    save_text_output(book, output_dir)

    audio_files = []
    total_start = time.time()

    for i, ch in enumerate(book["chapters"]):
        safe       = re.sub(r"[^\w\s-]", "", ch["title"])[:40].strip()
        audio_path = str(audio_dir / f"{i+1:02d}_{safe}.mp3")
        log.info(f"\n[{i+1}/{len(book['chapters'])}] {ch['title']}")
        tts_kokoro(ch["text"], audio_path, voice, kokoro)
        audio_files.append(audio_path)

    save_manifest(book, audio_files, output_dir)

    total_elapsed = timedelta(seconds=int(time.time() - total_start))
    log.info("\n" + "=" * 60)
    log.info(f"  DONE in {total_elapsed}!  Files -> {output_dir / 'audio'}/")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aurelius v2 — Kokoro audiobook engine")
    parser.add_argument("--input",  required=True,         help="Path to PDF")
    parser.add_argument("--voice",  default=DEFAULT_VOICE, help="Voice (default: af_bella)")
    parser.add_argument("--no-ai",  action="store_true",   help="Skip Claude AI cleanup")
    args = parser.parse_args()
    run(pdf_path=args.input, voice=args.voice, use_ai=not args.no_ai)
