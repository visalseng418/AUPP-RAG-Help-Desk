"""
Retrieval + Generation Pipeline - hybrid search, reranking, answer generation
==============================================================================
The query side of the AUPP RAG project. Reads what ingest.py produced:

  - db/aupp_chroma_db/     the persisted Chroma vector store (BGE-M3 embeddings)
  - db/chunks/*.jsonl      every embedded category's chunks - BM25 is rebuilt
                           from ALL of these each run (BM25 has no persistence;
                           feeding it only some categories silently breaks
                           keyword search for the rest)

Every model (embeddings, reranker, LLM) is pulled directly from HuggingFace -
same source as ingest.py's BGE-M3 embeddings, deliberately no Ollama here so
there's one consistent model-sourcing path across the whole project.

Pipeline per query:
  query -> aggregation check: "how many / list all / which programs ...
           credits" questions route to db/facts/programs.json instead of
           retrieval (vector search returns the k most SIMILAR chunks,
           never ALL matching ones - unusable for exact counts/lists; see
           build_program_facts.py)
        -> otherwise: hybrid retrieval (vector 0.7 + BM25 0.3, RRF fusion,
           wide net) -> cross-encoder rerank (BAAI/bge-reranker-v2-m3) down
           to top N -> LLM answer with source URLs

LLM: Qwen2.5-7B-Instruct, 4-bit via MLX - bitsandbytes has no Apple Silicon
support since its quantization kernels are CUDA-only, but Apple's own MLX
framework has native 4-bit quantization for the M-series unified memory
architecture, and the community has pre-quantized this exact model:
mlx-community/Qwen2.5-7B-Instruct-4bit.

Usage:
    python query.py "How many credits is the Civil Engineering major?"
    python query.py "How many programs require more than 120 credits?"
    python query.py "What scholarship do I get with 5 A's?" --show-chunks
    python query.py --retrieval-only "MPTC scholarship documents"
    python query.py                      # interactive mode
    python query.py --llm mlx-community/Qwen2.5-3B-Instruct-4bit --top-n 5 "..."

Requires (local):
    pip install --break-system-packages "langchain<1.0" "langchain-community<1.0" \
        "langchain-huggingface<1.0" "pydantic<2.11" langchain-chroma \
        sentence-transformers rank_bm25 mlx-lm
"""

import os
import re
import glob
import json
import argparse

from langchain_core.documents import Document

# --- Paths: must match ingest.py ---
PERSIST_DIR = "db/aupp_chroma_db"
COLLECTION_NAME = "aupp_docs"
CHUNKS_DIR = "db/chunks"
PROGRAM_FACTS_PATH = "db/facts/programs.json"

# Set to your HF Dataset repo id (e.g. "yourusername/aupp-rag-db") in
# production - a freshly-deployed container starts with an empty db/, and
# this is what restores it without re-running the entire ingestion
# pipeline. Unset locally, where db/ already exists on disk from having
# run ingest.py directly.
DATA_DATASET_REPO = os.environ.get("AUPP_DATA_DATASET_REPO")


def ensure_data_present():
    """Pull db/ (Chroma vector store + BM25 jsonl chunks + facts table)
    down from an HF Dataset if it's not already present locally - the
    same portable-persistence pattern discussed for migrating the vector
    store to Kaggle, just for HF Datasets instead. A fresh container (e.g.
    an HF Space) has none of this on disk yet; this is what makes startup
    work without re-embedding anything.

    Expects the dataset repo's ROOT to directly contain aupp_chroma_db/,
    chunks/, and facts/ (i.e. db/'s contents, not a nested db/ folder) -
    matching uploading from INSIDE db/ (`cd db && hf upload <repo> . `
    `--repo-type=dataset`). Downloads into local_dir="db" to reconstruct
    the expected db/aupp_chroma_db, db/chunks, db/facts layout locally.

    Safe to call unconditionally: if db/ is already present (local dev,
    or a redeploy that reuses a persistent volume), this is a no-op."""
    have_chroma = os.path.exists(PERSIST_DIR)
    have_chunks = os.path.exists(CHUNKS_DIR) and glob.glob(os.path.join(CHUNKS_DIR, "*.jsonl"))
    have_facts = os.path.exists(PROGRAM_FACTS_PATH)

    if have_chroma and have_chunks and have_facts:
        print("db/ already present locally - skipping dataset download")
        return

    if not DATA_DATASET_REPO:
        print("WARNING: db/ is missing pieces locally and AUPP_DATA_DATASET_REPO "
              "isn't set - the pipeline will likely fail to find embedded data. "
              f"(chroma={have_chroma}, chunks={bool(have_chunks)}, facts={have_facts})")
        return

    from huggingface_hub import snapshot_download
    print(f"Downloading db/ from HF Dataset '{DATA_DATASET_REPO}'...")
    snapshot_download(repo_id=DATA_DATASET_REPO, repo_type="dataset", local_dir="db")
    print("db/ downloaded")




# Aggregation questions need BOTH a genuine multi-item trigger AND a
# credits mention. "how many" alone is too broad - "how many credits is
# Civil Engineering?" is a SINGLE-program lookup, not an aggregation, but
# would match a bare "how many" + "credit" check. Requiring "how many" to
# be immediately followed by a plural/program word (programs, degrees,
# bachelor, master) fixes this: it matches "how many programs..." and
# "how many bachelor programs..." but not "how many credits...".
AGGREGATION_TRIGGER_PATTERN = re.compile(
    r"\b(how many (programs?|degrees?|bachelor|master)|list all|which (programs?|degrees?)|count)\b",
    re.IGNORECASE)
CREDITS_MENTION_PATTERN = re.compile(r"\bcredit", re.IGNORECASE)

# --- Retrieval settings ---
HYBRID_K = 15          # candidates from EACH retriever before fusion
DEFAULT_TOP_N = 5      # chunks handed to the LLM after reranking
VECTOR_WEIGHT = 0.7
BM25_WEIGHT = 0.3

EMBED_MODEL = "BAAI/bge-m3"
EMBED_MODEL_REVISION = "9a0624b896d81da7492a910ffa53731274b6cf3d"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

# Two LLM backends, selectable via LLM_BACKEND env var or --llm-backend:
#   mlx      - Apple Silicon only, fastest, used for local Mac development
#   llamacpp - CPU-native (GGUF), used in production (e.g. HF Spaces free
#              CPU tier), since MLX simply doesn't run anywhere but a Mac
DEFAULT_LLM_BACKEND = os.environ.get("LLM_BACKEND", "mlx")
DEFAULT_LLM_MLX = "mlx-community/Qwen2.5-7B-Instruct-4bit"
DEFAULT_LLM_GGUF_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
DEFAULT_LLM_GGUF_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
# Note: MLX default stays 7B (Apple Silicon is fast enough that there's no
# reason to downgrade local dev quality). The CPU/production path uses 3B
# instead - on 2 vCPUs with no GPU, response time matters more for a demo
# someone's actually waiting on than the quality gap between 3B and 7B.
# Alias resolving to whichever backend is active - lets existing callers
# (e.g. build_backend.py) keep referencing a single "the model name" value
DEFAULT_LLM = (DEFAULT_LLM_MLX if DEFAULT_LLM_BACKEND == "mlx"
               else f"{DEFAULT_LLM_GGUF_REPO}/{DEFAULT_LLM_GGUF_FILE}")


# ---------------------------------------------------------------------------
# Loading what ingest.py produced
# ---------------------------------------------------------------------------

def load_all_embedded_chunks(chunks_dir=CHUNKS_DIR):
    """Read EVERY embedded category's jsonl into Documents. BM25 must be
    built over all of them - not just the newest category - or keyword
    search silently loses coverage of everything else."""
    paths = sorted(glob.glob(os.path.join(chunks_dir, "*.jsonl")))
    if not paths:
        raise FileNotFoundError(
            f"No chunk files found in {chunks_dir}/ - run ingest.py --add <category> first")

    docs = []
    per_category = {}
    for path in paths:
        category = os.path.splitext(os.path.basename(path))[0]
        count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                docs.append(Document(page_content=data["page_content"],
                                     metadata=data["metadata"]))
                count += 1
        per_category[category] = count

    print(f"Loaded {len(docs)} chunks across {len(per_category)} categories "
          f"for BM25: {', '.join(f'{c}({n})' for c, n in sorted(per_category.items()))}")
    return docs


def get_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def free_device_memory():
    """PyTorch's MPS backend caches memory across calls instead of
    releasing it back to the OS - the same issue found and fixed in
    ingest.py's embed_category() batch loop, except here it matters more:
    the query-embedding step (inside hybrid.invoke()) runs on MPS on
    EVERY chat request, and build_backend.py is a long-running server
    process that never exits between requests the way the CLI ingestion
    script does. Without clearing the cache, usage would accumulate
    across the server's entire lifetime rather than just within one
    batch job. Called after every retrieval. Guarded so it's a no-op on
    CUDA/CPU-only setups."""
    try:
        import torch
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except ImportError:
        pass
    import gc
    gc.collect()


def build_retriever():
    """Vector store + BM25 -> EnsembleRetriever (RRF fusion built in)."""
    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_chroma import Chroma
    from langchain.retrievers import EnsembleRetriever
    from langchain_community.retrievers import BM25Retriever

    device = get_device()
    print(f"Loading embedding model {EMBED_MODEL} on {device} (revision {EMBED_MODEL_REVISION[:8]})...")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": device, "revision": EMBED_MODEL_REVISION},
        encode_kwargs={"normalize_embeddings": True},
    )

    if not os.path.exists(PERSIST_DIR):
        raise FileNotFoundError(
            f"{PERSIST_DIR} doesn't exist - run ingest.py --add <category> first")

    vectorstore = Chroma(
        persist_directory=PERSIST_DIR,
        embedding_function=embeddings,
        collection_name=COLLECTION_NAME,
    )
    print(f"Vector store: {vectorstore._collection.count()} embedded chunks")

    vector_retriever = vectorstore.as_retriever(search_kwargs={"k": HYBRID_K})

    all_chunks = load_all_embedded_chunks()
    bm25_retriever = BM25Retriever.from_documents(all_chunks)
    bm25_retriever.k = HYBRID_K

    hybrid = EnsembleRetriever(
        retrievers=[vector_retriever, bm25_retriever],
        weights=[VECTOR_WEIGHT, BM25_WEIGHT],
    )
    print(f"Hybrid retriever ready (vector {VECTOR_WEIGHT} + BM25 {BM25_WEIGHT}, "
          f"k={HYBRID_K} each)")
    return hybrid


def build_reranker():
    """Cross-encoder on CPU - reads query+chunk together, catches relevance
    the bi-encoder embeddings miss. CPU keeps it out of the way of anything
    on the GPU."""
    from sentence_transformers import CrossEncoder
    print(f"Loading reranker {RERANKER_MODEL} on cpu...")
    return CrossEncoder(RERANKER_MODEL, device="cpu")


def rerank(reranker, query, documents, top_n):
    """Score every (query, chunk) pair, return top_n docs + their scores."""
    pairs = [(query, doc.page_content) for doc in documents]
    scores = reranker.predict(pairs)
    scored = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
    top = scored[:top_n]
    return [doc for doc, _ in top], [float(s) for _, s in top]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_llm(model_name=None, backend=None):
    """Load the LLM via whichever backend is selected:

    mlx - Apple Silicon only, genuinely 4-bit quantized via Apple's own
    MLX framework (bitsandbytes' quantization kernels are CUDA-only, no
    Mac support at all). Fastest option, used for local development.

    llamacpp - CPU-native GGUF quantization via llama-cpp-python. Used in
    production deployments (e.g. HF Spaces free CPU tier) where MLX isn't
    available at all, since it only runs on Apple hardware. Genuinely
    slower on CPU with no GPU, but portable to any standard cloud box.

    Returns a dict tagged with which backend built it, so generate_answer/
    rewrite_with_history/answer_aggregation_query can all dispatch through
    the same _generate_text() helper without needing the caller to know
    or care which backend is active."""
    backend = backend or DEFAULT_LLM_BACKEND

    if backend == "mlx":
        from mlx_lm import load
        model_name = model_name or DEFAULT_LLM_MLX
        print(f"Loading LLM {model_name} via MLX (4-bit)...")
        model, tokenizer = load(model_name)
        print("LLM loaded")
        return {"backend": "mlx", "model": model, "tokenizer": tokenizer}

    if backend == "llamacpp":
        from llama_cpp import Llama
        repo_id = model_name or DEFAULT_LLM_GGUF_REPO
        print(f"Loading LLM {repo_id}/{DEFAULT_LLM_GGUF_FILE} via llama.cpp (CPU, GGUF)...")
        model = Llama.from_pretrained(
            repo_id=repo_id,
            filename=DEFAULT_LLM_GGUF_FILE,
            n_ctx=4096,
            verbose=False,
        )
        print("LLM loaded")
        return {"backend": "llamacpp", "model": model}

    raise ValueError(f"Unknown LLM backend: {backend!r} (expected 'mlx' or 'llamacpp')")


def _generate_text(llm, messages, max_tokens):
    """Dispatch generation to whichever backend built this llm object.
    Both backends take the same chat-message format (list of
    {"role": ..., "content": ...} dicts) and return plain text - this is
    the ONE place that needs to know the backends differ at all."""
    if llm["backend"] == "mlx":
        from mlx_lm import generate as mlx_generate
        model, tokenizer = llm["model"], llm["tokenizer"]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        return mlx_generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False)

    if llm["backend"] == "llamacpp":
        result = llm["model"].create_chat_completion(messages=messages, max_tokens=max_tokens)
        return result["choices"][0]["message"]["content"]

    raise ValueError(f"Unknown LLM backend: {llm.get('backend')!r}")


def generate_answer(llm, query, top_chunks, history=None):
    """Build a sourced prompt from the reranked chunks and generate.
    Works with either LLM backend (see _generate_text) - MLX for local
    Mac development, llama.cpp/GGUF for CPU-only production deployment.

    Prior turns (history) are included as real conversation turns before
    the current question, so the answer can reference earlier context
    naturally (e.g. 'as I mentioned, that program requires 128 credits').

    Returns the RAW answer text only - no source footer appended here.
    That's a display-layer concern, not a generation concern, and
    different consumers want it differently: the CLI (answer_question)
    appends a text footer for terminal printing, while the API returns
    sources as structured JSON instead of baking them into the text.
    Call append_source_list() separately wherever a text footer is
    actually wanted."""
    context_blocks = []
    for i, doc in enumerate(top_chunks, 1):
        context_blocks.append(
            f"--- Document {i} (source: {doc.metadata.get('source_url', 'unknown')}) ---\n"
            f"{doc.page_content}"
        )
    context = "\n\n".join(context_blocks)

    combined_input = f"""Based on the following documents from AUPP (American University of Phnom Penh), answer this question: {query}

{context}

Provide a clear, accurate answer using only the information in these documents. If a document refers to the same person, program, or topic the question asks about - even with a minor spelling variation, a missing/different title (Dr., Mr., Ms., etc.), or slightly different capitalization - treat it as a match and answer using that information; don't refuse to answer just because the wording isn't a perfect literal match. Only say "I don't have enough information to answer that question based on the provided documents." when the documents genuinely don't address what's being asked. Do NOT include a source list or "Document N" citations in your answer - the sources will be added automatically afterward."""

    messages = [
        {"role": "system", "content": "You are a helpful assistant answering questions about AUPP university. Use good judgment when matching names or terms in the question to the documents - minor spelling differences, titles, and formatting variations don't make something a non-match."},
    ]
    # Replay recent conversation so the model has continuity. Only the last
    # few turns are included to keep the prompt focused.
    if history:
        for role, text in history[-4:]:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": combined_input})

    return _generate_text(llm, messages, max_tokens=512)


def append_source_list(answer_text, top_chunks):
    """Append a 'Sources:' section built directly from chunk metadata -
    deduplicated, in the order each source first appears among the
    reranked chunks. This is the guaranteed-correct citation list; the
    LLM is explicitly told not to produce its own."""
    seen = set()
    urls = []
    for doc in top_chunks:
        url = doc.metadata.get("source_url", "unknown")
        if url not in seen:
            seen.add(url)
            urls.append(url)

    if not urls:
        return answer_text

    sources_block = "\n".join(f"- {url}" for url in urls)
    return f"{answer_text.strip()}\n\nSources:\n{sources_block}"


# ---------------------------------------------------------------------------
# History-aware query rewriting
# ---------------------------------------------------------------------------

def rewrite_with_history(llm, query, history):
    """Rewrite a follow-up into a standalone, searchable question using the
    recent chat history. Without this, 'what about civil engineering?'
    retrieves generic civil-engineering info instead of inheriting the
    prior turn's actual intent (e.g. 'which programs need >120 credits').

    Returns the query unchanged when there's no history, so the first turn
    of a session pays no extra LLM call.
    """
    if not history:
        return query

    # Only the last few turns matter for resolving a follow-up, and keeping
    # it short avoids the rewriter drowning in an entire long session.
    recent = history[-4:]
    history_text = "\n".join(f"{role}: {text}" for role, text in recent)

    rewrite_prompt = f"""Given the conversation history, rewrite the follow-up question so it is fully standalone and searchable on its own - carry over any subject, constraint, or number from earlier turns that the follow-up depends on. Return ONLY the rewritten question, nothing else.

Conversation history:
{history_text}

Follow-up question: {query}

Standalone question:"""

    messages = [
        {"role": "system", "content": "You rewrite follow-up questions to be standalone. Output only the rewritten question."},
        {"role": "user", "content": rewrite_prompt},
    ]
    rewritten = _generate_text(llm, messages, max_tokens=128).strip()

    # Guard against the model returning something empty or degenerate
    return rewritten if rewritten else query


# ---------------------------------------------------------------------------
# Aggregation questions - answered from db/facts/programs.json, not retrieval
# ---------------------------------------------------------------------------
#
# Filtering/counting happens in PYTHON here, not via the LLM. An earlier
# version asked the LLM to filter the full facts list itself in text - that
# failed in practice: given ~27 programs and two implicit conditions
# ("bachelor" AND ">120 credits"), a 7B model silently dropped most matches
# and returned only 5 instead of the correct ~15, with no signal anything
# was missing. LLMs are unreliable at exhaustive list enumeration even when
# given complete data. Since the actual filtering is simple, deterministic
# logic, doing it in Python removes that failure mode entirely - the LLM's
# only job is turning an already-correct result into a sentence.

# comparator phrase -> operator function
COMPARATOR_PATTERNS = [
    (re.compile(r"more than|greater than|over|above|exceed", re.IGNORECASE), lambda v, t: v > t),
    (re.compile(r"at least|no less than|\bmin(?:imum)?\b", re.IGNORECASE), lambda v, t: v >= t),
    (re.compile(r"less than|fewer than|under|below", re.IGNORECASE), lambda v, t: v < t),
    (re.compile(r"at most|no more than|\bmax(?:imum)?\b", re.IGNORECASE), lambda v, t: v <= t),
    (re.compile(r"exactly|equal to|=", re.IGNORECASE), lambda v, t: v == t),
]
THRESHOLD_NUMBER_PATTERN = re.compile(r"\b(\d{2,3})\b")

DEGREE_LEVEL_PATTERNS = {
    "bachelor": re.compile(r"\bbachelor|\bBA\b|\bBS\b", re.IGNORECASE),
    "master": re.compile(r"\bmaster|\bMBA\b|\bMS\b|\bLLM\b", re.IGNORECASE),
}


def load_program_facts(path=PROGRAM_FACTS_PATH):
    """Load the exact, complete program-credits table built by
    build_program_facts.py. Returns [] if it hasn't been built yet."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_aggregation_query(query):
    """True for questions that need a COMPLETE, exact answer (counts,
    lists, filters) rather than the k most similar chunks. Requires both
    a list/count trigger word and a credits mention, so this only fires
    for what the program-facts table can actually answer."""
    return bool(AGGREGATION_TRIGGER_PATTERN.search(query) and
                CREDITS_MENTION_PATTERN.search(query))


def parse_credit_filter(query):
    """Extract a (comparator_fn, threshold, label) triple from the query
    text, e.g. 'more than 120 credits' -> (>, 120, 'more than 120').
    Returns None if no explicit threshold is found (a plain 'list all
    programs' question with no credit condition)."""
    for pattern, fn in COMPARATOR_PATTERNS:
        match = pattern.search(query)
        if match:
            # look for a number anywhere near the comparator phrase
            num_match = THRESHOLD_NUMBER_PATTERN.search(query[match.end():match.end() + 20])
            if not num_match:
                num_match = THRESHOLD_NUMBER_PATTERN.search(query)
            if num_match:
                threshold = int(num_match.group(1))
                label = match.group(0).lower() + " " + str(threshold)
                return fn, threshold, label
    return None


def parse_degree_level(query):
    """Detect whether the question restricts to bachelor's or master's
    programs specifically, based on the program name text. Returns None
    if the question doesn't mention a degree level (no restriction)."""
    for level, pattern in DEGREE_LEVEL_PATTERNS.items():
        if pattern.search(query):
            return level
    return None


def filter_program_facts(facts, query):
    """Apply the query's credit threshold and degree-level restriction to
    the full facts list, deterministically. This is the actual answer -
    the LLM only phrases it, never recomputes it."""
    credit_filter = parse_credit_filter(query)
    degree_level = parse_degree_level(query)

    matches = facts
    if credit_filter:
        fn, threshold, _ = credit_filter
        matches = [f for f in matches if fn(f["total_credits"], threshold)]
    if degree_level:
        # Bachelor's programs start "Bachelor of ..."; master's programs
        # use several forms (Master of ..., MBA, MS in ..., LLM in ...) -
        # matched against the program name text itself.
        pattern = DEGREE_LEVEL_PATTERNS[degree_level]
        matches = [f for f in matches if pattern.search(f["program"])]

    return matches, credit_filter, degree_level


def answer_aggregation_query(llm, query, facts):
    """Filter the facts table in Python (exact, complete, deterministic),
    then ask the LLM only to phrase that already-correct result as a
    sentence - not to do the filtering/counting itself."""
    matches, credit_filter, degree_level = filter_program_facts(facts, query)

    print(f"  Filter applied: "
          f"{degree_level + ' programs' if degree_level else 'all programs'}"
          f"{' with ' + credit_filter[2] + ' credits' if credit_filter else ''}"
          f" -> {len(matches)} match(es) (of {len(facts)} total, computed in Python)")

    if not matches:
        criteria_parts = [p for p in [degree_level, credit_filter[2] if credit_filter else None] if p]
        criteria = " and ".join(criteria_parts) if criteria_parts else "the given criteria"
        return f"No programs in the data match that criteria ({criteria})."

    facts_lines = []
    for f in matches:
        line = f"- {f['program']}: {f['total_credits']} credits"
        if f["needs_review"]:
            line += " (NOTE: source data for this program is ambiguous - unconfirmed)"
        facts_lines.append(line)
    facts_text = "\n".join(facts_lines)

    combined_input = f"""The following {len(matches)} AUPP program(s) have ALREADY been filtered to exactly match the question "{query}" - this list is complete and correct, don't second-guess or re-filter it. Just present it clearly, stating the exact count ({len(matches)}).

{facts_text}

If any program above is marked as unconfirmed/ambiguous, mention that in your answer."""

    messages = [
        {"role": "system", "content": "You present pre-filtered data clearly and accurately. Never add, omit, or recount items - the list you're given is already the final, correct answer."},
        {"role": "user", "content": combined_input},
    ]

    return _generate_text(llm, messages, max_tokens=800)


# ---------------------------------------------------------------------------
# Per-query flow
# ---------------------------------------------------------------------------

def answer_question(query, hybrid, reranker, llm, top_n, show_chunks, retrieval_only, history=None):
    print(f"\n=== Query: {query} ===")

    # History-aware rewrite: only when we have both an LLM and prior turns.
    search_query = query
    if history and llm is not None:
        search_query = rewrite_with_history(llm, query, history)
        if search_query != query:
            print(f"Rewritten for search: {search_query}")

    # Aggregation routing: bypass retrieval entirely for "how many / list
    # all ... credits" style questions - answer from the complete facts
    # table instead, since retrieval can only ever return a k-sized sample.
    if llm is not None and is_aggregation_query(search_query):
        facts = load_program_facts()
        if facts:
            print(f"Routed to program facts table ({len(facts)} programs, exact/complete)")
            answer = answer_aggregation_query(llm, query, facts)
            print("\n=== Answer ===")
            print(answer)
            return answer
        else:
            print(f"Aggregation question detected, but {PROGRAM_FACTS_PATH} doesn't "
                  f"exist yet (run build_program_facts.py) - falling back to normal retrieval.")

    candidates = hybrid.invoke(search_query)
    free_device_memory()
    print(f"Hybrid search returned {len(candidates)} candidate chunks")

    top_chunks, scores = rerank(reranker, search_query, candidates, top_n)
    print(f"\nTop {len(top_chunks)} after reranking:")
    for i, (doc, score) in enumerate(zip(top_chunks, scores), 1):
        meta = doc.metadata
        print(f"  [{i}] score={score:.4f}  {meta.get('chunk_id', meta.get('filename', '?'))}")

    if show_chunks or retrieval_only:
        for i, doc in enumerate(top_chunks, 1):
            print(f"\n--- chunk [{i}] ({doc.metadata.get('source_url', 'unknown')}) ---")
            print(doc.page_content)

    if retrieval_only:
        return None

    print("\nGenerating answer...")
    # The generation prompt keeps the ORIGINAL query (natural phrasing), but
    # gets the chat history too so the answer itself stays conversational.
    raw_answer = generate_answer(llm, query, top_chunks, history=history)
    # Footer-appending happens here, not inside generate_answer, since it's
    # a CLI display concern - the API (build_backend.py) uses the raw text
    # and returns sources as structured JSON instead.
    answer = append_source_list(raw_answer, top_chunks)
    print("\n=== Answer ===")
    print(answer)
    return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", help="question to ask; omit for interactive mode")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"chunks passed to the LLM after reranking (default {DEFAULT_TOP_N})")
    parser.add_argument("--llm", default=None,
                        help="model id/repo for generation (default depends on --llm-backend: "
                             f"'{DEFAULT_LLM_MLX}' for mlx, '{DEFAULT_LLM_GGUF_REPO}' for llamacpp)")
    parser.add_argument("--llm-backend", choices=["mlx", "llamacpp"], default=DEFAULT_LLM_BACKEND,
                        help=f"LLM runtime (default: {DEFAULT_LLM_BACKEND}, or set LLM_BACKEND env var) - "
                             f"'mlx' for local Mac development, 'llamacpp' for CPU-only production "
                             f"deployment (MLX only runs on Apple Silicon)")
    parser.add_argument("--show-chunks", action="store_true",
                        help="print full reranked chunk contents before the answer")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="retrieve + rerank + print chunks, skip LLM generation entirely")
    parser.add_argument("--no-history", action="store_true",
                        help="disable history-aware rewriting in interactive mode (treat each question independently)")
    args = parser.parse_args()

    hybrid = build_retriever()
    reranker = build_reranker()
    llm = None if args.retrieval_only else build_llm(args.llm, backend=args.llm_backend)

    # A single-shot query has no prior turns to be aware of, so history
    # only matters for the interactive loop below.
    if args.query:
        answer_question(args.query, hybrid, reranker, llm,
                        args.top_n, args.show_chunks, args.retrieval_only)
        return

    print("\nInteractive mode - type a question and press Enter.")
    print("  'clear' resets conversation history, Ctrl+D or Ctrl+C to quit.")
    if args.no_history:
        print("  (history-aware rewriting disabled via --no-history)")

    # history is a list of (role, text) tuples: ("user", ...) / ("assistant", ...)
    history = []
    while True:
        try:
            query = input("\nquery> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() == "clear":
            history = []
            print("Conversation history cleared.")
            continue

        active_history = None if args.no_history else history
        answer = answer_question(query, hybrid, reranker, llm,
                                 args.top_n, args.show_chunks, args.retrieval_only,
                                 history=active_history)

        # Record the turn so the next follow-up can be resolved against it.
        # Skipped in retrieval-only mode (no answer generated) and when
        # history is disabled.
        if answer is not None and not args.no_history:
            history.append(("user", query))
            history.append(("assistant", answer))


if __name__ == "__main__":
    main()