"""
Incremental Ingestion Pipeline - four-group chunking strategy
=========================================================================
Implements the chunking strategy finalized from CORPUS_ANALYSIS.md:

  Group A - whole-file (leadership, student-life, scholarships):
      one chunk per file up to a cap (1500; scholarships 2000), with a
      paragraph-first splitter fallback only for files that overflow.
  Group B - faculty: same mechanics as A (1500 cap), but ~40% of files
      are expected to legitimately split into 2 chunks (long bios).
  Group C - prose (admissions, about, events, root):
      RecursiveCharacterTextSplitter(1200, 200).
  Group D - table/record-aware (programs, academic-policies, library,
      tuition): prose sections get the Group C splitter, but runs of
      consecutive `Header: value | Header: value` table rows are packed
      whole-rows-per-chunk and are NEVER split mid-row. Short headings
      directly above a table run are carried onto each of that run's
      chunks so "Technical Electives" style context isn't lost.

  Special files (regardless of folder):
      course-descriptions.txt - split per course record (one chunk per
          course), since it's ~261 independent records, not prose.
      academic-and-student-policies-and-regulations.txt - split at bare
          title lines into sub-policies first, then Group C within each
          section, with a "Section: <title>" prefix on every chunk.

  Hygiene: files with < 150 chars of body content (link-list hub pages)
  are excluded from the index entirely - they add noise, answer nothing.

Incremental workflow:
  - db/manifest.json tracks which categories are already embedded; adding
    a new category only embeds that category's chunks (stable chunk ids
    mean re-runs upsert instead of duplicating).
  - Every ingested category's chunks are also written to
    db/chunks/<category>.jsonl. The query side must rebuild BM25 from ALL
    of these files each session (BM25 has no persistence - see workflow
    notes in the project discussion), so these jsonl files are the
    contract between ingestion and retrieval.

Usage:
    python ingest.py --status
    python ingest.py --add programs scholarships          # first run
    python ingest.py --add admissions                     # later, incremental
    python ingest.py --add tuition --dry-run              # chunk+verify only
    python ingest.py --add faculty --force                # re-embed a category
    python ingest.py --add programs --backend ollama      # default is hf (BGE-M3)
"""

import os
import re
import json
import glob
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

DOCS_DIR = "docs"
PERSIST_DIR = "db/aupp_chroma_db"
COLLECTION_NAME = "aupp_docs"
MANIFEST_PATH = "db/manifest.json"
CHUNKS_DIR = "db/chunks"

MIN_BODY_CHARS = 150          # exclude link-list hub pages below this

# Two files where the length-based hygiene filter gets the wrong answer,
# found by POLICIES_LIBRARY_ANALYSIS.md:
#   - library-library-staff.txt is only ~70 chars but lists 2 real staff
#     members - MIN_BODY_CHARS was dropping it from the index entirely,
#     so "who works at the library" got NO retrieved chunk at all, not
#     just an incomplete one. Rescued regardless of length.
#   - general-information.txt is 382 chars (safely above the threshold)
#     but is a pure link-hub with no answerable content ("click one of
#     the links below" + 5 links) - it passes the length filter but
#     answers nothing, so it's excluded by name instead.
ALWAYS_INCLUDE_FILES = {"library-library-staff.txt"}
ALWAYS_EXCLUDE_FILES = {"general-information.txt"}
EMBED_BATCH_SIZE = 64

TABLE_ROW_PATTERN = re.compile(r"^[^:]+: .+ \| [^:]+: .+")
SOURCE_LINE_PATTERN = re.compile(r"^Source: (\S+)\s*$")
COURSE_RECORD_PATTERN = re.compile(r"^[A-Z]{2,5} ?[0-9]{2,3}[A-Z]? ?- ")
# Matches both "1. How do..." (early entries) and "10.\tHow do..." (later
# entries switch to a tab after the period) - a real formatting
# inconsistency in the source, not something to normalize away.
FAQ_ENTRY_PATTERN = re.compile(r"^\d{1,3}\.\s+\S")
# A "bare title line": short, starts capitalized, no sentence-ending
# punctuation - used to segment the concatenated policies file.
TITLE_LINE_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9 ,&\-'()/]{2,60}$")

CATEGORY_TO_GROUP = {
    "leadership": "A",
    "student-life": "A",
    "scholarships": "A",
    "faculty": "B",
    "admissions": "C",
    "about": "C",
    "events": "C",
    "root": "C",
    "programs": "D",
    "academic-policies": "D",
    "library": "D",
    "tuition": "D",
}

WHOLE_FILE_CAP_DEFAULT = 1500
CATEGORY_CAP_OVERRIDES = {"scholarships": 2000}
PROSE_CHUNK_SIZE = 1200
PROSE_CHUNK_OVERLAP = 200
# A short prose segment sitting directly above a table run is treated as
# that run's heading and carried onto each of its chunks.
TABLE_HEADING_MAX_CHARS = 200

SPECIAL_FILE_CHUNKERS = {
    "course-descriptions.txt": "course_records",
    # Same 300+ record, 6-field-per-course shape as course-descriptions.txt
    # above - this archive file was apparently missed by that earlier fix
    # (POLICIES_LIBRARY_ANALYSIS.md). Reuses the identical chunker.
    "course-descriptions-archive.txt": "course_records",
    "academic-and-student-policies-and-regulations.txt": "titled_sections",
    # 28 numbered Q&A entries, 4,497 chars - too big for the roster
    # whole-file cap (2500) and a poor fit for one giant chunk anyway,
    # since that would dilute the embedding across 28 unrelated topics
    # (renewals, hours, laptop loans, printing rates...). Packed
    # multiple-entries-per-chunk instead, never splitting one entry.
    "library-library-faqs.txt": "faq_entries",
    # Unlike leadership/faculty (one file = one person), this single file
    # lists ALL board members together - a roster, not prose. Left to the
    # default "about" category's Group C prose splitter (1200 chars), its
    # 1,928 chars split into 2 chunks, roughly bisecting the 9-member list.
    # Retrieval then only surfaced the first chunk in top-k, so "who are
    # the board of trustees" answered with only the first ~4 members -
    # not a wrong answer, an INCOMPLETE one, silently. Whole-file chunking
    # with a generous cap keeps the roster intact as a single chunk.
    "board-of-trustees.txt": "whole_file_roster",
}
ROSTER_CAP = 2500  # comfortably above board-of-trustees.txt's 1,928 chars, with headroom to grow


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def discover_categories(docs_dir):
    """All ingestable categories on disk: each subfolder, plus 'root' for
    files sitting directly in docs/."""
    categories = []
    for entry in sorted(os.listdir(docs_dir)):
        full = os.path.join(docs_dir, entry)
        if os.path.isdir(full) and glob.glob(os.path.join(full, "*.txt")):
            categories.append(entry)
    if glob.glob(os.path.join(docs_dir, "*.txt")):
        categories.append("root")
    return categories


def derive_url_context(source_url):
    """Build a short, human-readable context prefix from the URL path,
    e.g. https://www.aupp.edu.kh/bachelors-admission/admission-process/
    -> 'Bachelors Admission > Admission Process'

    Why this exists: a page's own body text often doesn't repeat its
    category. 'bachelors-admission-admission-process.txt' has an H1 of
    just 'Admission Process' - the word 'bachelor' only lives in the URL
    path, never in the text itself. Since only page_content gets embedded
    and BM25-indexed (metadata like source_url is invisible to both), a
    query mentioning 'bachelor' has no textual signal connecting it to
    this chunk. Prepending the URL's path segments as a heading fixes
    this without needing per-file special-casing - it's the same
    "carry context that would otherwise be lost" pattern used elsewhere
    for table headings and policy section titles.
    """
    if not source_url or source_url == "unknown":
        return None
    path = urlparse(source_url).path.strip("/")
    if not path:
        return None
    segments = [s for s in path.split("/") if s]
    humanized = [s.replace("-", " ").title() for s in segments]
    return " > ".join(humanized)


def load_documents(category, only_files=None, exclude_files=None):
    """Load one category's .txt files into Documents. The Source: line is
    extracted into metadata so every chunk inherits its citation URL.

    only_files: if given, load ONLY these filenames (for --files, embedding
    a subset of a category - e.g. the two huge course-description files
    separately from the rest, both to control memory and to make progress
    resumable).
    exclude_files: if given, skip these filenames (used internally so a
    later full category --add doesn't redundantly re-embed files already
    covered by an earlier --files run).

    A short context prefix derived from the URL path is prepended to the
    body BEFORE chunking (see derive_url_context) - this makes category
    context that only lives in the URL (e.g. "bachelor's admission")
    actually present in the text that gets embedded, not just in metadata
    no retriever ever looks at."""
    if category == "root":
        filepaths = sorted(glob.glob(os.path.join(DOCS_DIR, "*.txt")))
    else:
        filepaths = sorted(glob.glob(os.path.join(DOCS_DIR, category, "*.txt")))

    if only_files is not None:
        filepaths = [fp for fp in filepaths if os.path.basename(fp) in only_files]
    if exclude_files:
        filepaths = [fp for fp in filepaths if os.path.basename(fp) not in exclude_files]

    docs, excluded = [], []
    for filepath in filepaths:
        with open(filepath, encoding="utf-8") as f:
            raw = f.read()

        lines = raw.split("\n")
        match = SOURCE_LINE_PATTERN.match(lines[0]) if lines else None
        source_url = match.group(1) if match else "unknown"
        body = "\n".join(lines[1:]).strip() if match else raw.strip()

        filename = os.path.basename(filepath)

        if filename in ALWAYS_EXCLUDE_FILES:
            excluded.append((filename, len(body)))
            continue
        if len(body) < MIN_BODY_CHARS and filename not in ALWAYS_INCLUDE_FILES:
            excluded.append((filename, len(body)))
            continue

        context = derive_url_context(source_url)
        if context:
            body = f"{context}\n\n{body}"

        docs.append(Document(
            page_content=body,
            metadata={
                "source_url": source_url,
                "filename": os.path.basename(filepath),
                "category": category,
            },
        ))

    print(f"  Loaded {len(docs)} documents from {category}/")
    for filename, length in excluded:
        print(f"    excluded hub/stub page: {filename} ({length} body chars)")
    return docs


# ---------------------------------------------------------------------------
# Chunkers
# ---------------------------------------------------------------------------

def build_prose_splitter(chunk_size=PROSE_CHUNK_SIZE, chunk_overlap=PROSE_CHUNK_OVERLAP):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def chunk_whole_file(doc, cap):
    """Group A/B: one chunk if the body fits the cap; otherwise fall back
    to paragraph-first splitting at the cap size."""
    if len(doc.page_content) <= cap:
        return [doc.page_content]
    fallback = build_prose_splitter(chunk_size=cap, chunk_overlap=PROSE_CHUNK_OVERLAP)
    return fallback.split_text(doc.page_content)


def chunk_prose(doc):
    """Group C: plain paragraph-first splitting."""
    return build_prose_splitter().split_text(doc.page_content)


def _pack_table_rows(rows, chunk_size, heading=None):
    """Pack whole table rows into chunks up to chunk_size, never splitting
    a row internally. A single row longer than chunk_size becomes its own
    oversized chunk (with a warning) rather than being cut."""
    header_prefix = (heading + "\n") if heading else ""
    chunks, current = [], []
    current_len = len(header_prefix)

    for row in rows:
        row_len = len(row) + 1  # + newline
        if len(header_prefix) + row_len > chunk_size and not current:
            # Single row alone exceeds the cap - emit as-is, never split it
            print(f"    WARNING: single table row ({len(row)} chars) exceeds "
                  f"chunk_size={chunk_size}; emitting oversized chunk intact")
            chunks.append(header_prefix + row)
            current_len = len(header_prefix)
            continue
        if current_len + row_len > chunk_size and current:
            chunks.append(header_prefix + "\n".join(current))
            current, current_len = [], len(header_prefix)
        current.append(row)
        current_len += row_len

    if current:
        chunks.append(header_prefix + "\n".join(current))
    return chunks


def chunk_text_table_aware(text):
    """Core logic: segment a block of text into alternating prose and
    table-row runs, packing table rows whole-rows-per-chunk and carrying
    short headings onto their table run. Operates on plain text so it can
    be reused both for whole documents (chunk_table_aware) and for
    individual sections within a larger document (chunk_titled_sections) -
    the latter is why this file's own inline GPA tables need this same
    row-safety, not just the standalone table/tuition/programs files."""
    lines = text.split("\n")

    segments = []
    for line in lines:
        is_table = bool(TABLE_ROW_PATTERN.match(line.strip()))
        if segments and segments[-1][0] == is_table:
            segments[-1][1].append(line)
        else:
            segments.append((is_table, [line]))

    prose_splitter = build_prose_splitter()
    chunks = []
    i = 0
    while i < len(segments):
        is_table, seg_lines = segments[i]
        block = "\n".join(seg_lines).strip()

        if not block:
            i += 1
            continue

        if is_table:
            rows = [l.strip() for l in seg_lines if l.strip()]
            chunks.extend(_pack_table_rows(rows, PROSE_CHUNK_SIZE))
            i += 1
            continue

        next_is_table = i + 1 < len(segments) and segments[i + 1][0]
        if next_is_table and len(block) <= TABLE_HEADING_MAX_CHARS:
            rows = [l.strip() for l in segments[i + 1][1] if l.strip()]
            chunks.extend(_pack_table_rows(rows, PROSE_CHUNK_SIZE, heading=block))
            i += 2
            continue

        chunks.extend(prose_splitter.split_text(block))
        i += 1

    return chunks


def chunk_table_aware(doc):
    """Group D: programs/, tuition/, etc. - documents that mix prose with
    table-row runs across the WHOLE file."""
    return chunk_text_table_aware(doc.page_content)


def chunk_course_records(doc):
    """course-descriptions.txt / course-descriptions-archive.txt: one chunk
    per course record. Each record starts at a 'CODE - Name' line;
    everything until the next code line belongs to that course.

    Any content BEFORE the first record boundary (e.g. the URL-context
    prefix derived by derive_url_context, like "Course Descriptions
    Archive") is merged into the first real record rather than emitted as
    its own near-empty orphan chunk."""
    lines = doc.page_content.split("\n")
    records, current = [], []
    for line in lines:
        if COURSE_RECORD_PATTERN.match(line.strip()) and current and records_has_real_content(current):
            records.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        records.append("\n".join(current).strip())
    return [r for r in records if r]


def chunk_faq_entries(doc):
    """library-library-faqs.txt: 28 numbered Q&A entries. Packs multiple
    whole entries per chunk - never splitting a single Q&A across chunks -
    using the same whole-unit packing as _pack_table_rows, just keyed on
    numbered-entry boundaries instead of the Header:value table pattern
    (this file has no such labels, it's plain numbered prose)."""
    lines = doc.page_content.split("\n")
    entries, current = [], []
    for line in lines:
        if FAQ_ENTRY_PATTERN.match(line.strip()) and current:
            entries.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current).strip())
    entries = [e for e in entries if e]
    return _pack_table_rows(entries, PROSE_CHUNK_SIZE)


def records_has_real_content(lines):
    """True if this pending record already contains an actual course
    record (a line matching the course pattern) rather than just leading
    context/prefix text with nothing to split off yet."""
    return any(COURSE_RECORD_PATTERN.match(l.strip()) for l in lines)


def chunk_titled_sections(doc):
    """academic-and-student-policies-and-regulations.txt: split at bare
    title lines into sub-policy sections, then chunk WITHIN each section
    using the same table-aware logic as chunk_table_aware - this file
    contains its own inline copies of the standing/grading GPA tables
    (POLICIES_LIBRARY_ANALYSIS.md), and a plain prose splitter would have
    no awareness of those rows, cutting them wherever the 1200-char limit
    happened to land. Every resulting chunk is prefixed with
    'Section: <title>' so a GPA rule retrieved alone still says which
    policy it belongs to."""
    lines = doc.page_content.split("\n")

    sections = []  # (title, [lines])
    current_title, current_lines = None, []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        is_title = (
            TITLE_LINE_PATTERN.match(stripped)
            and not TABLE_ROW_PATTERN.match(stripped)
            and not stripped.endswith((".", ":", ";", ","))
            # a title is followed by real content, not another blank/title
            and idx + 1 < len(lines) and lines[idx + 1].strip()
        )
        if is_title:
            if current_lines or current_title:
                sections.append((current_title, current_lines))
            current_title, current_lines = stripped, []
        else:
            current_lines.append(line)
    sections.append((current_title, current_lines))

    chunks = []
    for title, section_lines in sections:
        body = "\n".join(section_lines).strip()
        if not body and title:
            continue
        prefix = f"Section: {title}\n" if title else ""
        for piece in chunk_text_table_aware(body) if body else []:
            chunks.append(prefix + piece)
    return chunks


def chunk_document(doc):
    """Route one document to its chunker based on special-file rules first,
    then category group."""
    filename = doc.metadata["filename"]
    category = doc.metadata["category"]

    special = SPECIAL_FILE_CHUNKERS.get(filename)
    if special == "course_records":
        return chunk_course_records(doc)
    if special == "titled_sections":
        return chunk_titled_sections(doc)
    if special == "whole_file_roster":
        return chunk_whole_file(doc, ROSTER_CAP)
    if special == "faq_entries":
        return chunk_faq_entries(doc)

    group = CATEGORY_TO_GROUP.get(category, "C")
    if group in ("A", "B"):
        cap = CATEGORY_CAP_OVERRIDES.get(category, WHOLE_FILE_CAP_DEFAULT)
        return chunk_whole_file(doc, cap)
    if group == "D":
        return chunk_table_aware(doc)
    return chunk_prose(doc)


def chunk_category(docs):
    """Chunk every document in a category, attaching stable chunk ids
    (category/filename::N) so re-runs upsert instead of duplicating."""
    all_chunks = []
    for doc in docs:
        texts = chunk_document(doc)
        for i, text in enumerate(texts):
            all_chunks.append(Document(
                page_content=text,
                metadata={
                    **doc.metadata,
                    "chunk_index": i,
                    "chunk_id": f"{doc.metadata['category']}/{doc.metadata['filename']}::{i}",
                },
            ))

    lens = [len(c.page_content) for c in all_chunks]
    if lens:
        print(f"  Chunked into {len(all_chunks)} chunks "
              f"(min={min(lens)} avg={sum(lens) // len(lens)} max={max(lens)})")
    return all_chunks


# ---------------------------------------------------------------------------
# Verification - corpus-wide table-row integrity
# ---------------------------------------------------------------------------

def verify_table_rows(docs, chunks):
    """Every 'Header: value | Header: value' row in the source documents
    must survive intact inside at least one chunk of the same file. This
    is the safety net that no chunking strategy silently reintroduces the
    split-table ambiguity the reformatting work fixed."""
    chunks_by_file = {}
    for chunk in chunks:
        chunks_by_file.setdefault(chunk.metadata["filename"], []).append(chunk.page_content)

    total_rows, broken = 0, []
    for doc in docs:
        filename = doc.metadata["filename"]
        table_lines = [l.strip() for l in doc.page_content.split("\n")
                       if TABLE_ROW_PATTERN.match(l.strip())]
        file_chunks = chunks_by_file.get(filename, [])
        for line in table_lines:
            total_rows += 1
            if not any(line in chunk_text for chunk_text in file_chunks):
                broken.append((filename, line))

    if broken:
        for filename, line in broken[:10]:
            print(f"    BROKEN {filename}: {line[:80]}")
        raise AssertionError(
            f"{len(broken)}/{total_rows} table rows split or dropped - "
            f"fix chunking before embedding")

    print(f"  Verified: all {total_rows} table rows intact within a single chunk")


# ---------------------------------------------------------------------------
# Manifest + persistence
# ---------------------------------------------------------------------------

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
        # Backward-compatible: older manifests won't have this key yet
        manifest.setdefault("partial", {})
        return manifest
    return {"embedded": {}, "partial": {}}


def save_manifest(manifest):
    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def write_chunks_jsonl(category, chunks, merge=False):
    """Persist this category's chunks so the query side can rebuild BM25
    over the full corpus each session without re-chunking.

    merge=True (used for --files partial embeds): read whatever's already
    in this category's jsonl, replace only the entries for filenames
    covered by `chunks` (in case of a re-run), and keep every other
    file's previously-written chunks untouched. Without this, a partial
    write would silently wipe out earlier partial progress on the same
    category's jsonl, since chunks here is only ever a subset when
    merge=True."""
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    path = os.path.join(CHUNKS_DIR, f"{category}.jsonl")

    if merge and os.path.exists(path):
        new_filenames = {c.metadata["filename"] for c in chunks}
        kept = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                if entry["metadata"]["filename"] not in new_filenames:
                    kept.append(entry)
        all_entries = kept + [
            {"page_content": c.page_content, "metadata": c.metadata} for c in chunks
        ]
    else:
        all_entries = [
            {"page_content": c.page_content, "metadata": c.metadata} for c in chunks
        ]

    with open(path, "w", encoding="utf-8") as f:
        for entry in all_entries:
            f.write(json.dumps(entry) + "\n")
    print(f"  Wrote {len(all_entries)} total chunks to {path} "
          f"({len(chunks)} new/updated this run)")
    return len(all_entries)


def get_embeddings(backend):
    """Build the embedding model for the chosen backend. Imported lazily so
    only the chosen backend's package needs to be installed."""
    if backend == "hf":
        import torch
        from langchain_huggingface import HuggingFaceEmbeddings
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
        print(f"  Embedding backend: HuggingFace BAAI/bge-m3 on {device}")
        return HuggingFaceEmbeddings(
            model_name="BAAI/bge-m3",
            model_kwargs={"device": device},
            encode_kwargs={"normalize_embeddings": True},
        )
    if backend == "ollama":
        from langchain_ollama import OllamaEmbeddings
        print("  Embedding backend: Ollama bge-m3")
        return OllamaEmbeddings(model="bge-m3")
    raise ValueError(f"Unknown backend: {backend}")


def free_device_memory():
    """PyTorch's MPS backend caches memory across encode() calls instead of
    releasing it back to the OS - over many sequential batches this cache
    can balloon far past what the workload actually needs (reported: 27GB
    for a few hundred chunks that should need a few GB). Explicitly
    clearing it after every batch keeps usage bounded. Guarded so this is
    a no-op on CUDA/CPU-only setups (including this being imported/run
    anywhere MPS isn't available at all)."""
    try:
        import torch
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except ImportError:
        pass
    import gc
    gc.collect()


def embed_category(chunks, embeddings, batch_size=EMBED_BATCH_SIZE):
    """Add this category's chunks to the persistent Chroma store in
    batches, with stable ids so re-runs upsert. Memory is freed after each
    batch (see free_device_memory) - without this, MPS's memory caching
    can accumulate across many sequential batches far past actual need."""
    from langchain_chroma import Chroma

    vectorstore = Chroma(
        persist_directory=PERSIST_DIR,
        embedding_function=embeddings,
        collection_name=COLLECTION_NAME,
        collection_metadata={"hnsw:space": "cosine"},
    )

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        ids = [c.metadata["chunk_id"] for c in batch]
        vectorstore.add_documents(batch, ids=ids)
        free_device_memory()
        print(f"  Embedded {min(start + batch_size, len(chunks))}/{len(chunks)} chunks")

    return vectorstore


def cmd_remove_file(category, filename):
    """Remove one specific file's chunks from the vector store, the
    category's jsonl, and manifest tracking - for a file that turns out
    to have been embedded by mistake (e.g. course-descriptions-archive.txt
    being an outdated duplicate of course-descriptions.txt).

    Uses the raw chromadb client directly rather than the langchain
    Chroma wrapper - deletion doesn't need to embed anything, so there's
    no reason to load the multi-GB embedding model just to remove a few
    hundred vectors.

    Note: this only removes what's already embedded. If filename.txt is
    still sitting in docs/<category>/, a future plain `--add <category>`
    would pick it right back up and re-embed it - move or delete the
    source file too if it should stay gone for good."""
    import chromadb

    manifest = load_manifest()
    removed_vectors = 0

    if os.path.exists(PERSIST_DIR):
        client = chromadb.PersistentClient(path=PERSIST_DIR)
        try:
            collection = client.get_collection(COLLECTION_NAME)
            matched = collection.get(where={"filename": filename})
            matched_ids = matched.get("ids", [])
            if matched_ids:
                collection.delete(ids=matched_ids)
            removed_vectors = len(matched_ids)
        except Exception as e:
            print(f"  Warning: couldn't query/delete from Chroma collection ({e})")
    else:
        print(f"  No vector store found at {PERSIST_DIR} - nothing to remove there")

    jsonl_path = os.path.join(CHUNKS_DIR, f"{category}.jsonl")
    removed_jsonl_entries = 0
    remaining_total = 0
    if os.path.exists(jsonl_path):
        kept = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                if entry["metadata"]["filename"] == filename:
                    removed_jsonl_entries += 1
                else:
                    kept.append(entry)
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry) + "\n")
        remaining_total = len(kept)
    else:
        print(f"  No jsonl found at {jsonl_path} - nothing to remove there")

    if category in manifest["embedded"]:
        manifest["embedded"][category]["chunks"] = remaining_total
        manifest["embedded"][category]["files"] = max(
            0, manifest["embedded"][category].get("files", 1) - 1)
    if category in manifest.get("partial", {}) and filename in manifest["partial"][category]:
        manifest["partial"][category].remove(filename)
        if not manifest["partial"][category]:
            manifest["partial"].pop(category)
    save_manifest(manifest)

    print(f"\nRemoved {filename} from {category}:")
    print(f"  {removed_vectors} vector(s) deleted from Chroma")
    print(f"  {removed_jsonl_entries} entry(ies) removed from {jsonl_path} "
          f"({remaining_total} remaining for this category)")
    print(f"\nReminder: if {filename} is still in docs/{category}/, move or "
          f"delete it there too, or a future --add will re-embed it.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status():
    manifest = load_manifest()
    on_disk = discover_categories(DOCS_DIR)
    embedded = manifest["embedded"]

    print("=== Ingestion status ===\n")
    print(f"{'category':<20} {'on disk':<10} {'embedded':<10} details")
    for category in on_disk:
        info = embedded.get(category)
        partial_files = manifest.get("partial", {}).get(category)
        if info:
            state, details = "yes", f"{info['chunks']} chunks @ {info['when'][:19]}"
        elif partial_files:
            state, details = "partial", f"{len(partial_files)} file(s) done: {', '.join(partial_files)}"
        else:
            state, details = "no", "-"
        print(f"{category:<20} {'yes':<10} {state:<10} {details}")

    orphans = [c for c in embedded if c not in on_disk]
    for category in orphans:
        print(f"{category:<20} {'NO':<10} {'yes':<10} embedded but folder missing on disk")

    pending = [c for c in on_disk if c not in embedded]
    print(f"\nPending (not yet fully embedded): {', '.join(pending) if pending else 'none'}")


def cmd_add(categories, backend, dry_run, force, batch_size, files=None):
    manifest = load_manifest()
    on_disk = discover_categories(DOCS_DIR)

    if files and len(categories) != 1:
        print("SKIP: --files requires exactly one --add category")
        return

    to_process = []
    for category in categories:
        if category not in on_disk:
            print(f"SKIP {category}: no such folder (or no .txt files) under {DOCS_DIR}/")
            continue
        if category in manifest["embedded"] and not force:
            print(f"SKIP {category}: already embedded (use --force to re-embed)")
            continue
        to_process.append(category)

    if not to_process:
        print("Nothing to do.")
        return

    embeddings = None if dry_run else get_embeddings(backend)

    for category in to_process:
        print(f"\n=== {category}{' (files: ' + ', '.join(files) + ')' if files else ''} ===")

        # Load the category fully unfiltered ONCE - this is the source of
        # truth for "which files SHOULD end up embedded." Using this
        # (rather than a raw filesystem glob) is what makes the
        # completeness check below correct: hygiene-excluded files (like
        # a thin hub page) are never expected to be embedded, so a
        # category isn't wrongly stuck at "partial" forever just because
        # it has one.
        all_loadable_docs = load_documents(category)
        all_files_for_category = {d.metadata["filename"] for d in all_loadable_docs}

        already_partial = set(manifest["partial"].get(category, []))
        if files:
            # --files run: use only the requested files, regardless of
            # whatever partial progress already exists (lets you
            # re-embed a specific file on purpose without --force
            # wiping the whole category).
            requested = set(files)
            docs = [d for d in all_loadable_docs if d.metadata["filename"] in requested]
            requested_not_found = requested - {d.metadata["filename"] for d in docs}
            for missing in requested_not_found:
                print(f"  WARNING: {missing} not found (or excluded by hygiene filter) - skipped")
        elif already_partial and not force:
            # Full category add, but some files were already embedded via
            # an earlier --files run - skip those, only do what's left.
            docs = [d for d in all_loadable_docs if d.metadata["filename"] not in already_partial]
            if docs:
                print(f"  Skipping {len(already_partial)} file(s) already partially "
                      f"embedded: {', '.join(sorted(already_partial))}")
        else:
            docs = all_loadable_docs

        if not docs:
            print("  No ingestable documents to process, skipping.")
            continue

        chunks = chunk_category(docs)
        verify_table_rows(docs, chunks)

        if dry_run:
            print("  --dry-run: skipping embedding and persistence")
            continue

        embed_category(chunks, embeddings, batch_size=batch_size)

        # Baseline of "already covered" files, before this run's work:
        # - if the category was already fully embedded, treat EVERY
        #   expected file as already covered - this run might just be
        #   refreshing one specific file (--files --force), which
        #   shouldn't downgrade a complete category back to "partial".
        # - otherwise, whatever partial progress was recorded before.
        was_fully_embedded = category in manifest["embedded"]
        baseline_covered = all_files_for_category if was_fully_embedded else already_partial
        processed_filenames = {d.metadata["filename"] for d in docs}
        now_covered = baseline_covered | processed_filenames

        if now_covered < all_files_for_category:
            # Still partial overall - merge into the jsonl rather than
            # overwrite, and record partial progress only.
            write_chunks_jsonl(category, chunks, merge=True)
            manifest["partial"][category] = sorted(now_covered)
            save_manifest(manifest)
            print(f"  Manifest updated: {category} partial progress "
                  f"({len(now_covered)}/{len(all_files_for_category)} files)")
        else:
            # Every expected file for this category is now covered -
            # promote to (or keep) a full completion record. Always
            # merge, not just when there was recorded partial progress -
            # this run's `chunks` might only cover a subset of files
            # (e.g. a single-file --files --force refresh of an already
            # complete category), and merge is always safe: it degrades
            # to a plain fresh write when there's nothing to merge with.
            total_chunks = write_chunks_jsonl(category, chunks, merge=True)
            manifest["embedded"][category] = {
                "files": len(now_covered),
                "chunks": total_chunks,
                "when": datetime.now(timezone.utc).isoformat(),
            }
            manifest["partial"].pop(category, None)
            save_manifest(manifest)
            print(f"  Manifest updated: {category} recorded as fully embedded")

    if not dry_run:
        print(f"\nDone. Reminder: the query side must rebuild BM25 from ALL "
              f"jsonl files in {CHUNKS_DIR}/ - BM25 does not persist.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--add", nargs="+", metavar="CATEGORY",
                        help="categories to ingest (folder names under docs/, or 'root')")
    parser.add_argument("--status", action="store_true", help="show manifest vs disk")
    parser.add_argument("--backend", choices=["hf", "ollama"], default="hf",
                        help="embedding backend (default: hf = BAAI/bge-m3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="chunk + verify only, no embedding or persistence")
    parser.add_argument("--force", action="store_true",
                        help="re-embed categories already in the manifest")
    parser.add_argument("--batch-size", type=int, default=EMBED_BATCH_SIZE,
                        help=f"chunks embedded per batch (default {EMBED_BATCH_SIZE}) - "
                             f"lower this if embedding is using excessive RAM, e.g. on MPS "
                             f"where memory caches across batches instead of releasing")
    parser.add_argument("--files", nargs="+", metavar="FILENAME",
                        help="only embed these specific files within the category "
                             "(requires exactly one --add category); progress is tracked "
                             "so a later full --add on the same category skips these")
    parser.add_argument("--remove-file", nargs=2, metavar=("CATEGORY", "FILENAME"),
                        help="remove one specific file's chunks from the vector store, "
                             "jsonl, and manifest - for a file that was embedded by "
                             "mistake or turned out to be redundant/outdated")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return
    if args.remove_file:
        cmd_remove_file(args.remove_file[0], args.remove_file[1])
        return
    if args.add:
        cmd_add(args.add, args.backend, args.dry_run, args.force, args.batch_size, args.files)
        return
    parser.print_help()


if __name__ == "__main__":
    main()