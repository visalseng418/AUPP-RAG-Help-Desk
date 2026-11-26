"""
FastAPI backend for the AUPP RAG chatbot
=========================================================================
Wraps query.py's retrieval + rerank + generation pipeline as an HTTP API.
Reuses every piece of query.py's logic directly (hybrid search, RRF fusion,
reranking, history-aware rewriting, aggregation routing) rather than
reimplementing any of it - this file is purely a thin HTTP layer on top.

All three models (embeddings, reranker, LLM) load ONCE at server startup,
not per-request - the CLI reloads everything on every invocation, which is
fine for one-off testing but would make every API call take minutes.

Conversation history is kept in-memory, per session_id, in the same
(role, text) tuple format query.py's CLI already uses. This does NOT
persist across server restarts - acceptable for now, worth revisiting
(e.g. a real database) if this needs to survive deploys/restarts later.

Usage:
    pip install --break-system-packages fastapi "uvicorn[standard]"
    python build_backend.py
    # or: uvicorn build_backend:app --reload --port 8000

    Interactive API docs: http://localhost:8000/docs
"""

import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import query as q  # reuse the existing pipeline - no logic duplicated here

# Populated once at startup, read (never mutated) by every request
state = {}

# session_id -> [(role, text), ...] - same format as query.py's CLI history
sessions: dict[str, list[tuple[str, str]]] = {}

MAX_HISTORY_TURNS = 20  # (10 exchanges) - trimmed per session to avoid unbounded growth


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Checking for embedded data...")
    q.ensure_data_present()  # pulls db/ from an HF Dataset if missing -
                              # must happen before build_retriever() etc.,
                              # since they all read from db/ on disk
    print("Loading models - this happens once at startup...")
    state["hybrid"] = q.build_retriever()
    state["reranker"] = q.build_reranker()
    # backend=None -> resolves from LLM_BACKEND env var (defaults to "mlx"
    # for local dev; set LLM_BACKEND=llamacpp in production, e.g. the HF
    # Spaces Dockerfile, since MLX only runs on Apple Silicon)
    state["llm"] = q.build_llm(backend=None)
    state["ready"] = True
    print("✅ All models loaded, API ready to serve requests")
    yield
    state.clear()
    sessions.clear()


app = FastAPI(
    title="AUPP RAG API",
    description="Retrieval-augmented Q&A over AUPP (American University of Phnom Penh) content.",
    lifespan=lifespan,
)

# Permissive for local dev - tighten allow_origins to the real frontend's
# origin once one exists, rather than leaving "*" in anything deployed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None  # omit to start a new session
    top_n: int = q.DEFAULT_TOP_N


class RetrievedChunk(BaseModel):
    filename: str
    source_url: str
    score: float
    preview: str  # first ~200 chars, not the full chunk - keeps responses light


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[str]
    used_aggregation: bool
    rewritten_query: Optional[str] = None
    retrieved_chunks: list[RetrievedChunk] = []


class SessionResponse(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Whether the server is up AND models are loaded - the latter takes a
    while at startup, so a plain 200 doesn't mean requests will succeed yet."""
    return {"status": "ok", "models_loaded": state.get("ready", False)}


@app.get("/status")
def status():
    facts = q.load_program_facts()
    return {
        "embed_model": q.EMBED_MODEL,
        "embed_model_revision": q.EMBED_MODEL_REVISION,
        "reranker_model": q.RERANKER_MODEL,
        "llm_model": q.DEFAULT_LLM,
        "program_facts_loaded": len(facts),
        "active_sessions": len(sessions),
    }


@app.post("/session", response_model=SessionResponse)
def create_session():
    """Explicitly start a new session. Optional - /chat will create one
    automatically if session_id is omitted, this just lets a client get
    the id up front before the first message."""
    session_id = str(uuid.uuid4())
    sessions[session_id] = []
    return SessionResponse(session_id=session_id)


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="session not found")
    sessions[session_id] = []
    return {"status": "cleared"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    if not state.get("ready"):
        raise HTTPException(status_code=503, detail="Models are still loading, try again shortly")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    session_id = request.session_id or str(uuid.uuid4())
    history = sessions.setdefault(session_id, [])

    hybrid = state["hybrid"]
    reranker = state["reranker"]
    llm = state["llm"]
    query_text = request.message

    # History-aware rewrite - same logic as the CLI's answer_question()
    search_query = query_text
    rewritten_query = None
    if history:
        search_query = q.rewrite_with_history(llm, query_text, history)
        if search_query != query_text:
            rewritten_query = search_query

    # Aggregation routing - "how many / list all ... credits" bypasses
    # retrieval entirely in favor of the exact, complete facts table
    if q.is_aggregation_query(search_query):
        facts = q.load_program_facts()
        if facts:
            answer = q.answer_aggregation_query(llm, query_text, facts)
            _record_turn(history, query_text, answer)
            return ChatResponse(
                session_id=session_id,
                answer=answer,
                sources=[],  # aggregation answers summarize a filtered facts
                           # list, not individual retrieved chunks with URLs
                used_aggregation=True,
                rewritten_query=rewritten_query,
                retrieved_chunks=[],
            )
        # facts file missing - fall through to normal retrieval below

    # Normal hybrid retrieval -> rerank -> generate
    candidates = hybrid.invoke(search_query)
    q.free_device_memory()  # clears PyTorch's MPS cache - see query.py's
                             # free_device_memory() docstring for why this
                             # matters especially here: this is a
                             # long-running server, so uncleared MPS cache
                             # from query-embedding would accumulate across
                             # every /chat request for the server's entire
                             # lifetime, not just within one batch job.
    top_chunks, scores = q.rerank(reranker, search_query, candidates, request.top_n)
    raw_answer = q.generate_answer(llm, query_text, top_chunks, history=history)
    _record_turn(history, query_text, raw_answer)

    seen, sources = set(), []
    for doc in top_chunks:
        url = doc.metadata.get("source_url", "unknown")
        if url not in seen:
            seen.add(url)
            sources.append(url)

    retrieved_chunks = [
        RetrievedChunk(
            filename=doc.metadata.get("filename", "?"),
            source_url=doc.metadata.get("source_url", "unknown"),
            score=float(score),
            preview=doc.page_content[:200],
        )
        for doc, score in zip(top_chunks, scores)
    ]

    return ChatResponse(
        session_id=session_id,
        answer=raw_answer,
        sources=sources,
        used_aggregation=False,
        rewritten_query=rewritten_query,
        retrieved_chunks=retrieved_chunks,
    )


def _record_turn(history, query_text, answer):
    """Append this exchange to session history, trimmed to the most
    recent MAX_HISTORY_TURNS entries so a long-running session's history
    (and therefore the rewrite/generation prompts built from it) doesn't
    grow unbounded."""
    history.append(("user", query_text))
    history.append(("assistant", answer))
    del history[:-MAX_HISTORY_TURNS]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)