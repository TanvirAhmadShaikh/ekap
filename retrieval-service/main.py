import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import psycopg2
from fastembed import TextEmbedding
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
from starlette.responses import Response

from auth import UserContext, get_user_context

DATABASE_URL        = os.getenv("DATABASE_URL", "postgresql://ekap:changeme@postgres:5432/ekap")
QDRANT_URL          = os.getenv("QDRANT_URL", "http://qdrant:6333")
LLM_BASE_URL        = os.getenv("LLM_BASE_URL", "http://vllm:8000/v1")
LLM_MODEL           = os.getenv("LLM_MODEL", "ekap-llm")

# Runtime-mutable model selection (resets to LLM_MODEL on container restart)
_current_model: str = LLM_MODEL
_show_model_name: bool = False
_show_stats: bool = False   # strategy/chunks/model/GPU-CPU line
_show_timing: bool = False  # ⏱ time-taken line
_pulling: dict[str, dict] = {}
_cancel_pull: set[str] = set()
# Ollama management API base (strip /v1 suffix used by OpenAI-compat path)
OLLAMA_BASE: str    = LLM_BASE_URL.rstrip("/").removesuffix("/v1")
COLLECTION_NAME     = "ekap_chunks"
SECTIONS_COLLECTION = "ekap_sections"
EMBEDDING_MODEL     = "BAAI/bge-small-en-v1.5"
TOP_K               = 5
RRF_K               = 60
SMALL_DOC_MAX       = 50
MEDIUM_DOC_MAX      = 300

# Deliberately has no per-query content (no {context} placeholder) — kept 100%
# identical across every turn and every conversation so it forms a stable,
# cacheable prefix (vLLM prefix caching / llama.cpp context-shift). Retrieved
# context instead gets attached to the latest user turn (see chat_completions),
# since that's the only part of the prompt that's actually new each turn.
SYSTEM_PROMPT = (
    "You are EKAP, an enterprise knowledge assistant. "
    "Answer the user's question using ONLY the information explicitly stated in the context "
    "provided with their latest message. Cite sources inline as [Source N].\n\n"
    "Important rules:\n"
    "- If the context contains only a table of contents, index entries, or page-number references "
    "(e.g. 'Chapter 5 ... 42', 'TRICEPS TRAINING 410'), treat it as if no useful content was found "
    "and tell the user the document has not been fully indexed yet.\n"
    "- Never infer, guess, or expand on what a source *might* say beyond what is explicitly quoted.\n"
    "- If the answer is genuinely not present in the context, say exactly: "
    "\"I don't have that information in the knowledge base.\"\n"
    "- Give your answer exactly once. Do not restate, summarize, or repeat it "
    "in a second pass or under a heading like 'Answer:'."
)

qdrant: QdrantClient = None
embedder: TextEmbedding = None
llm_client: httpx.AsyncClient = None

query_counter = Counter("ekap_queries_total", "Total queries", ["status"])
query_latency = Histogram("ekap_query_latency_seconds", "Query latency")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, embedder, llm_client
    qdrant   = QdrantClient(QDRANT_URL)
    embedder = TextEmbedding(EMBEDDING_MODEL)
    # Shared across requests — a fresh AsyncClient per request meant a new TCP
    # connection to Ollama/vLLM on every single chat message.
    llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0)
    )
    yield
    await llm_client.aclose()


REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "false").lower() == "true"

app = FastAPI(title="EKAP Retrieval Service", version="0.7.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Database ───────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


# ── Audit logging (non-blocking, never raises) ─────────────────────────────────

def _audit_sync(user_id: str, event_type: str, resource_id: str | None, details: dict):
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (user_id, event_type, resource_id, details) VALUES (%s,%s,%s,%s)",
                    (user_id, event_type, resource_id, json.dumps(details)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


async def audit(user_id: str, event_type: str, resource_id: str | None = None, details: dict | None = None):
    asyncio.create_task(asyncio.to_thread(_audit_sync, user_id, event_type, resource_id, details or {}))


# ── Permission gate (now classification-aware) ────────────────────────────────

def get_permitted_doc_ids(user: UserContext) -> list[str]:
    """
    Security gate — always runs before retrieval.

    Combines:
    - Role-based classification access (from JWT roles)
    - Explicit per-user document grants (from permissions table)
    - Lifecycle state filter: regular users see only 'published'; managers also see 'approved'
    - Soft-deleted documents are never returned
    """
    classifications = list(user.accessible_classifications)

    # Determine which lifecycle states this user may retrieve
    if REQUIRE_AUTH:
        lifecycle_states = ["published", "approved"] if user.can_manage_documents() else ["published"]
    else:
        # Dev mode: bypass workflow gate, return all completed docs
        lifecycle_states = ["draft", "review", "approved", "published"]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Classification-based access (lifecycle + soft-delete filtered)
            cls_ph = ",".join(["%s"] * len(classifications))
            lc_ph  = ",".join(["%s"] * len(lifecycle_states))
            cur.execute(
                f"SELECT document_id::text FROM documents "
                f"WHERE classification IN ({cls_ph}) AND status='completed' "
                f"AND lifecycle_state IN ({lc_ph}) AND deleted_at IS NULL",
                classifications + lifecycle_states,
            )
            class_ids = {r[0] for r in cur.fetchall()}

            # Explicit per-user grants (additive; still subject to lifecycle + soft-delete)
            cur.execute(
                f"SELECT p.document_id::text FROM permissions p "
                f"JOIN documents d ON d.document_id=p.document_id "
                f"WHERE p.user_id=%s AND p.can_read=TRUE "
                f"AND d.lifecycle_state IN ({lc_ph}) AND d.deleted_at IS NULL",
                [user.user_id] + lifecycle_states,
            )
            explicit_ids = {r[0] for r in cur.fetchall()}

            return list(class_ids | explicit_ids)
    finally:
        conn.close()


# ── Retrieval strategies ───────────────────────────────────────────────────────

def get_doc_size_map(permitted_ids: list[str]) -> dict[str, int]:
    if not permitted_ids:
        return {}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_id::text, coalesce(page_count,1) "
                "FROM documents WHERE document_id::text=ANY(%s) AND status='completed'",
                (permitted_ids,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def _hit_to_dict(hit, source: str) -> dict:
    p = hit.payload
    return {
        "chunk_id": p["chunk_id"], "document_id": p["document_id"],
        "document_title": p["document_title"], "page_number": p.get("page_number"),
        "section": p.get("section"), "content": p["content"],
        "score": hit.score, "source": source,
    }


def _vec(embedding: list, doc_ids: list[str], limit: int = TOP_K):
    f = Filter(must=[FieldCondition(key="document_id", match=MatchAny(any=doc_ids))]) if doc_ids else None
    return qdrant.search(collection_name=COLLECTION_NAME, query_vector=embedding,
                         query_filter=f, limit=limit, with_payload=True)


def _bm25(query: str, doc_ids: list[str]) -> list[dict]:
    if not query.strip() or not doc_ids:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT c.chunk_id::text, c.document_id::text, d.title AS document_title,
                              c.page_number, c.section, c.content,
                              ts_rank_cd(c.content_fts, plainto_tsquery('english',%s)) AS bm25_rank
                       FROM chunks c JOIN documents d ON c.document_id=d.document_id
                       WHERE c.content_fts @@ plainto_tsquery('english',%s)
                         AND c.document_id::text=ANY(%s) AND d.status='completed'
                       ORDER BY bm25_rank DESC LIMIT %s""",
                    (query, query, doc_ids, TOP_K),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except psycopg2.Error:
                return []
    finally:
        conn.close()


def _rrf(v_hits, b_hits: list[dict]) -> list[dict]:
    scores: dict[str, float] = {}
    data:   dict[str, dict]  = {}
    for rank, hit in enumerate(v_hits):
        cid = hit.payload["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        data[cid] = _hit_to_dict(hit, "vector")
    for rank, hit in enumerate(b_hits):
        cid = hit["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        if cid in data:
            data[cid]["source"] = "hybrid"
        else:
            data[cid] = {**hit, "score": float(hit["bm25_rank"]), "source": "bm25"}
    return [data[c] for c, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_K]]


def hybrid_search(query: str, emb: list, doc_ids: list[str]) -> list[dict]:
    return _rrf(_vec(emb, doc_ids), _bm25(query, doc_ids))


def hierarchical_search(emb: list, doc_ids: list[str], top_sections: int) -> list[dict]:
    try:
        if not qdrant.collection_exists(SECTIONS_COLLECTION):
            raise RuntimeError
        section_hits = qdrant.search(
            collection_name=SECTIONS_COLLECTION, query_vector=emb,
            query_filter=Filter(must=[FieldCondition(key="document_id", match=MatchAny(any=doc_ids))]),
            limit=top_sections, with_payload=True,
        )
    except Exception:
        section_hits = []

    if not section_hits:
        return [_hit_to_dict(h, "hierarchical-fallback") for h in _vec(emb, doc_ids)]

    chunks: list[dict] = []
    per_section = max(1, TOP_K // len(section_hits))
    for sh in section_hits:
        sid = sh.payload.get("section_id")
        if not sid:
            continue
        for h in qdrant.search(
            collection_name=COLLECTION_NAME, query_vector=emb,
            query_filter=Filter(must=[FieldCondition(key="section_id", match=MatchValue(value=sid))]),
            limit=per_section, with_payload=True,
        ):
            chunks.append(_hit_to_dict(h, "hierarchical"))
    return chunks


def rerank_combined(all_chunks: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for c in all_chunks:
        cid = c["chunk_id"]
        if cid not in seen or c["score"] > seen[cid]["score"]:
            seen[cid] = c
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:TOP_K]


# ── Retrieval Router ───────────────────────────────────────────────────────────

def retrieve(query: str, permitted_ids: list[str]) -> tuple[list[dict], str]:
    if not permitted_ids:
        return [], "no-permitted-documents"
    doc_sizes = get_doc_size_map(permitted_ids)
    if not doc_sizes:
        return [], "no-completed-documents"

    small  = [d for d, p in doc_sizes.items() if p < SMALL_DOC_MAX]
    medium = [d for d, p in doc_sizes.items() if SMALL_DOC_MAX <= p <= MEDIUM_DOC_MAX]
    large  = [d for d, p in doc_sizes.items() if p > MEDIUM_DOC_MAX]

    emb = list(embedder.embed([query]))[0].tolist()
    all_chunks, strategies = [], []

    if small:
        all_chunks.extend(hybrid_search(query, emb, small))
        strategies.append(f"hybrid({len(small)} small)")
    if medium:
        all_chunks.extend(hierarchical_search(emb, medium, top_sections=2))
        strategies.append(f"hierarchical({len(medium)} medium)")
    if large:
        all_chunks.extend(hierarchical_search(emb, large, top_sections=4))
        strategies.append(f"hierarchical({len(large)} large, wide)")

    return rerank_combined(all_chunks), " + ".join(strategies) or "none"


# ── Context builder ────────────────────────────────────────────────────────────

def build_context(chunks: list[dict]) -> tuple[str, list[dict]]:
    parts, citations = [], []
    for i, c in enumerate(chunks, 1):
        sec = f" | Section: {c['section']}" if c.get("section") else ""
        parts.append(
            f"[Source {i}: \"{c['document_title']}\"{sec} | Page: {c.get('page_number','N/A')}]\n{c['content']}"
        )
        citations.append({
            "source_num": i, "document_title": c["document_title"],
            "section": c.get("section"), "page_number": c.get("page_number"),
            "confidence": round(c["score"], 4), "document_id": c["document_id"],
            "retrieval_source": c["source"],
        })
    return "\n\n".join(parts), citations


# ── LLM streaming ──────────────────────────────────────────────────────────────

def _sse(cid: str, content: str, finish_reason=None) -> str:
    return f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':int(time.time()),'model':_current_model,'choices':[{'index':0,'delta':{'content':content} if content else {},'finish_reason':finish_reason}]})}\n\n"


async def _stub_stream(cid: str, answer: str):
    words = answer.split()
    for i, w in enumerate(words):
        yield _sse(cid, w + ("" if i == len(words) - 1 else " "))
    yield _sse(cid, "", finish_reason="stop")
    yield "data: [DONE]\n\n"


async def _gpu_status() -> str:
    """gpu | gpu-partial | cpu | unknown, for the active model's *last* inference."""
    if "vllm" in LLM_BASE_URL:
        # vLLM's docker-compose profile reserves an nvidia GPU to even start.
        return "gpu"
    try:
        resp = await llm_client.get(f"{OLLAMA_BASE}/api/ps", timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        for m in data.get("models", []):
            if m.get("model") == _current_model or m.get("name") == _current_model:
                size = m.get("size", 0)
                vram = m.get("size_vram", 0)
                if size and vram >= size * 0.98:
                    return "gpu"
                return "gpu-partial" if vram > 0 else "cpu"
        return "unknown"
    except Exception:
        return "unknown"


async def stream_llm(messages: list[dict], cid: str, citations: list[dict], strategy: str, chunk_count: int):
    try:
        payload = {
            "model": _current_model, "messages": messages, "stream": True,
            "repetition_penalty": 1.15,  # discourage the model re-emitting a duplicate "Answer:" pass
        }
        if "vllm" not in LLM_BASE_URL:
            payload["keep_alive"] = "30m"  # Ollama-only: avoid a cold model reload between messages
        async with llm_client.stream("POST", f"{LLM_BASE_URL}/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n\n"
    except Exception as _exc:
        n = len(citations)
        srcs = ", ".join(f"\"{c['document_title']}\" p.{c['page_number']}" for c in citations[:3])
        # Distinguish LLM-unreachable vs transient timeout so the message is accurate
        is_timeout = isinstance(_exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout))
        if is_timeout:
            stub = (
                f"The AI model took too long to respond. "
                f"Retrieved {n} relevant chunk(s)." + (f" Top sources: {srcs}." if srcs else "")
            )
        else:
            stub = (
                f"[LLM unavailable — check Ollama is running] "
                f"Retrieved {n} chunk(s). " + (f"Top sources: {srcs}." if srcs else "No matches.")
            )
        async for chunk in _stub_stream(cid, stub):
            yield chunk

    # Non-OpenAI-standard trailing event — our own Employee Portal reads this for
    # the "(strategy · N chunks · model)" stats bracket under each reply. Placed
    # after the try/except (not in a `finally`) — yielding from `finally` during
    # generator close (e.g. client disconnects mid-stream) raises "async generator
    # ignored GeneratorExit"; this way it only runs on a normal, watched completion.
    stats = {
        "strategy": strategy, "chunks": chunk_count, "model": _current_model,
        "gpu": await _gpu_status(), "citations": citations,
    }
    yield f"data: {json.dumps({'ekap_stats': stats})}\n\n"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "retrieval-service", "version": "0.7.0"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": _current_model, "object": "model", "created": int(time.time()), "owned_by": "ekap"}
    ]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, user: UserContext = Depends(get_user_context)):
    t0 = time.perf_counter()
    body = await request.json()
    messages  = body.get("messages", [])
    do_stream = body.get("stream", True)

    user_question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    # ── Security gate ────────────────────────────────────────────────────────
    permitted_ids = get_permitted_doc_ids(user)
    if not permitted_ids:
        await audit(user.user_id, "PERMISSION_DENIED", details={"query": user_question[:200]})

    # ── Routed retrieval ─────────────────────────────────────────────────────
    chunks, strategy  = retrieve(user_question, permitted_ids)
    context, citations = build_context(chunks)

    # Context goes on the latest user turn, not the system message — keeps the
    # system prompt + prior turns byte-identical across the conversation so the
    # LLM backend can reuse cached prefill for everything except the new turn.
    history = [m for m in messages if m.get("role") != "system"]
    context_block = context or "No relevant documents found in the knowledge base."
    if history and history[-1].get("role") == "user":
        history = history[:-1] + [{
            "role": "user",
            "content": f"Context:\n{context_block}\n\nQuestion: {history[-1]['content']}",
        }]
    augmented = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    # ── Audit ────────────────────────────────────────────────────────────────
    await audit(user.user_id, "QUERY", details={
        "query": user_question[:200],
        "strategy": strategy,
        "chunks": len(chunks),
        "doc_ids": [c["document_id"] for c in citations],
        "username": user.username,
        "roles": user.roles,
    })

    query_counter.labels(status="ok").inc()
    query_latency.observe(time.perf_counter() - t0)

    if do_stream:
        return StreamingResponse(stream_llm(augmented, cid, citations, strategy, len(chunks)), media_type="text/event-stream")

    try:
        payload = {
            "model": _current_model, "messages": augmented, "stream": False,
            "repetition_penalty": 1.15,  # discourage the model re-emitting a duplicate "Answer:" pass
        }
        if "vllm" not in LLM_BASE_URL:
            payload["keep_alive"] = "30m"  # Ollama-only: avoid a cold model reload between messages
        resp = await llm_client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        answer = f"[LLM unavailable] Retrieved {len(citations)} chunk(s) via {strategy}."

    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": _current_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "citations": citations, "retrieval_strategy": strategy,
    }


@app.post("/api/query")
async def query(request: Request, user: UserContext = Depends(get_user_context)):
    body     = await request.json()
    question = body.get("query", "")

    permitted_ids      = get_permitted_doc_ids(user)
    chunks, strategy   = retrieve(question, permitted_ids)
    context, citations = build_context(chunks)

    await audit(user.user_id, "QUERY", details={"query": question[:200], "strategy": strategy})

    return {
        "query": question, "citations": citations,
        "retrieval_strategy": strategy,
        "chunks_retrieved": len(chunks),
        "context_preview": context[:500] + "..." if len(context) > 500 else context,
    }


@app.get("/api/conversations")
async def get_conversations(
    request: Request,
    user: UserContext = Depends(get_user_context),
    limit: int = 20,
    offset: int = 0,
):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, created_at FROM conversations WHERE user_id=%s "
                "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (user.user_id, limit, offset),
            )
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM conversations WHERE user_id=%s", (user.user_id,))
            total = cur.fetchone()[0]
        return {"conversations": [{"id": str(r[0]), "title": r[1], "created_at": str(r[2])} for r in rows],
                "total": total}
    finally:
        conn.close()


@app.post("/api/feedback")
async def post_feedback(request: Request, user: UserContext = Depends(get_user_context)):
    body = await request.json()
    await audit(user.user_id, "FEEDBACK", resource_id=body.get("message_id"),
                details={"rating": body.get("rating"), "comment": body.get("comment", "")[:500]})
    return {"status": "received", "message_id": body.get("message_id")}


# ── LLM management endpoints ───────────────────────────────────────────────────

@app.get("/api/llm/config")
async def get_llm_config(user: UserContext = Depends(get_user_context)):
    return {
        "model":            _current_model,
        "env_model":        LLM_MODEL,
        "base_url":         LLM_BASE_URL,
        "backend":          "vllm" if "vllm" in LLM_BASE_URL else "ollama",
        "gpu":              await _gpu_status(),
        "show_model_name":  _show_model_name,
        "show_stats":       _show_stats,
        "show_timing":      _show_timing,
    }


@app.post("/api/llm/config")
async def set_llm_config(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    global _current_model, _show_model_name, _show_stats, _show_timing
    body = await request.json()

    if "model" in body:
        model = body.get("model", "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="model is required.")
        _current_model = model
        await audit(user.user_id, "LLM_CONFIG_CHANGE", details={"model": model})

    if "show_model_name" in body:
        _show_model_name = bool(body["show_model_name"])
        await audit(user.user_id, "LLM_CONFIG_CHANGE", details={"show_model_name": _show_model_name})

    if "show_stats" in body:
        _show_stats = bool(body["show_stats"])
        await audit(user.user_id, "LLM_CONFIG_CHANGE", details={"show_stats": _show_stats})

    if "show_timing" in body:
        _show_timing = bool(body["show_timing"])
        await audit(user.user_id, "LLM_CONFIG_CHANGE", details={"show_timing": _show_timing})

    return {
        "model": _current_model, "show_model_name": _show_model_name,
        "show_stats": _show_stats, "show_timing": _show_timing, "status": "updated",
    }


@app.get("/api/llm/models")
async def list_llm_models(user: UserContext = Depends(get_user_context)):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = [
            {
                "name":          m["name"],
                "size":          m.get("size", 0),
                "modified_at":   m.get("modified_at", ""),
                "quantization":  m.get("details", {}).get("quantization_level", ""),
            }
            for m in data.get("models", [])
        ]
        pulling = [{"name": name, **info} for name, info in _pulling.items()]
        return {"models": models, "active": _current_model, "pulling": pulling}
    except Exception as e:
        pulling = [{"name": name, **info} for name, info in _pulling.items()]
        return {"models": [], "active": _current_model, "pulling": pulling, "error": str(e)}


MAX_GPU_DIAGNOSTIC_LEN = 20_000  # pasted terminal output; cap so a pathological paste can't bloat the table


@app.get("/api/llm/gpu-diagnostics")
async def list_gpu_diagnostics(user: UserContext = Depends(get_user_context)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, submitted_by, submitted_at, output FROM gpu_diagnostics "
                "ORDER BY submitted_at DESC LIMIT 20"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {"entries": [
        {"id": str(r[0]), "submitted_by": r[1], "submitted_at": r[2].isoformat(), "output": r[3]}
        for r in rows
    ]}


@app.post("/api/llm/gpu-diagnostics")
async def submit_gpu_diagnostics(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body = await request.json()
    output = (body.get("output") or "").strip()
    if not output:
        raise HTTPException(status_code=400, detail="output is required.")
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)[:MAX_GPU_DIAGNOSTIC_LEN]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gpu_diagnostics (submitted_by, output) VALUES (%s, %s) "
                "RETURNING id, submitted_at",
                (user.username, output),
            )
            row_id, submitted_at = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    await audit(user.user_id, "GPU_DIAGNOSTICS_SUBMITTED", details={"id": str(row_id)})
    return {"id": str(row_id), "submitted_by": user.username, "submitted_at": submitted_at.isoformat(), "output": output}


def _do_pull(model: str):
    # Ollama streams progress per-layer (weights, manifest, config, ...), each
    # with its own "total" — a model's small trailing layers would otherwise
    # make the reported size shrink after the big weights layer finishes.
    # Track the largest "total" seen so far as a stable overall download size.
    model_size = 0
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", f"{OLLAMA_BASE}/api/pull", json={"name": model, "stream": True}) as resp:
                for line in resp.iter_lines():
                    if model in _cancel_pull:
                        break  # closes the connection on `with` exit, aborting Ollama's download too
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("error"):
                        _pulling[model] = {"status": "error", "error": evt["error"], "percent": None}
                        return
                    total      = evt.get("total", 0)
                    completed  = evt.get("completed", 0)
                    model_size = max(model_size, total)
                    percent    = round(completed / total * 100, 1) if total else None
                    _pulling[model] = {
                        "status":     evt.get("status", ""),
                        "completed":  completed,
                        "total":      total,
                        "model_size": model_size,
                        "percent":    percent,
                    }
        # Streamed to completion without an explicit error — model now shows up
        # via Ollama's /api/tags, so drop it from the in-progress list.
        _pulling.pop(model, None)
    except Exception as e:
        # Breaking out of the loop above closes the stream mid-read, which can
        # itself raise (e.g. a read error) — don't let that clobber a clean
        # cancellation with a spurious "error" status.
        if model in _cancel_pull:
            _pulling.pop(model, None)
        else:
            _pulling[model] = {"status": "error", "error": str(e), "percent": None}
    finally:
        _cancel_pull.discard(model)


@app.post("/api/llm/pull")
async def pull_model(
    request: Request,
    background_tasks: BackgroundTasks,
    user: UserContext = Depends(get_user_context),
):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body  = await request.json()
    model = body.get("model", "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required.")
    # Different models pull fully in parallel (each gets its own background task);
    # this only guards against double-starting the same model's pull.
    if model in _pulling and _pulling[model].get("status") != "error":
        return {"status": "pulling", "model": model}
    _pulling[model] = {"status": "starting", "completed": 0, "total": 0, "model_size": 0, "percent": 0}
    background_tasks.add_task(_do_pull, model)
    await audit(user.user_id, "LLM_PULL", details={"model": model})
    return {"status": "pulling", "model": model}


@app.post("/api/llm/pull/cancel")
async def cancel_pull(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body  = await request.json()
    model = body.get("model", "").strip()
    if model not in _pulling:
        raise HTTPException(status_code=404, detail=f'"{model}" is not currently pulling.')
    if _pulling[model].get("status") == "error":
        # Already finished (failed) — no background task left to cancel, just
        # dismiss the entry so it stops showing in the Installed Models list.
        _pulling.pop(model, None)
        await audit(user.user_id, "LLM_PULL_DISMISS", details={"model": model})
        return {"status": "dismissed", "model": model}
    _cancel_pull.add(model)
    await audit(user.user_id, "LLM_PULL_CANCEL", details={"model": model})
    return {"status": "cancelling", "model": model}


@app.delete("/api/llm/models/{name:path}")
async def delete_model(name: str, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    if name == _current_model:
        raise HTTPException(status_code=400, detail="Cannot remove the active model. Activate a different model first.")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request("DELETE", f"{OLLAMA_BASE}/api/delete", json={"name": name})
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Ollama error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    await audit(user.user_id, "LLM_DELETE", details={"model": name})
    return {"status": "deleted", "model": name}
