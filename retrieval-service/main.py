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

import providers
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
    "- Thoroughness means using everything relevant that IS in the context, not inventing what isn't: "
    "if multiple sources or passages bear on the question, pull the relevant details from all of them "
    "into one complete, well-explained answer rather than a single terse sentence.\n"
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

def get_permitted_doc_ids(user: UserContext, restrict_classifications: list[str] | None = None) -> list[str]:
    """
    Security gate — always runs before retrieval.

    Combines:
    - Role-based classification access (from JWT roles)
    - Explicit per-user document grants (from permissions table)
    - Lifecycle state filter: regular users see only 'published'; managers also see 'approved'
    - Soft-deleted documents are never returned

    restrict_classifications, when given, further caps role-based access to
    that set — used when an external LLM backend is active, so e.g. an
    Administrator's normal Restricted-document access doesn't silently leak
    into context sent to a third-party provider (see external_llm_settings).
    Explicit per-user grants are NOT capped by this — an admin who explicitly
    granted a user access to one specific document has already made that call.
    """
    classifications = list(user.accessible_classifications)
    if restrict_classifications is not None:
        classifications = [c for c in classifications if c in restrict_classifications]

    # Determine which lifecycle states this user may retrieve
    if REQUIRE_AUTH:
        lifecycle_states = ["published", "approved"] if user.can_manage_documents() else ["published"]
    else:
        # Dev mode: bypass workflow gate, return all completed docs
        lifecycle_states = ["draft", "review", "approved", "published"]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Classification-based access (lifecycle + soft-delete filtered).
            # An empty set (e.g. restrict_classifications shares nothing with
            # this user's role access) is a valid outcome, not an error — just
            # means no classification-based documents qualify.
            if classifications:
                cls_ph = ",".join(["%s"] * len(classifications))
                lc_ph  = ",".join(["%s"] * len(lifecycle_states))
                cur.execute(
                    f"SELECT document_id::text FROM documents "
                    f"WHERE classification IN ({cls_ph}) AND status='completed' "
                    f"AND lifecycle_state IN ({lc_ph}) AND deleted_at IS NULL",
                    classifications + lifecycle_states,
                )
                class_ids = {r[0] for r in cur.fetchall()}
            else:
                class_ids = set()

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


async def stream_llm(history: list[dict], cid: str, citations: list[dict], strategy: str, chunk_count: int, ext: dict):
    backend = ext["backend"]
    model   = ext["model"] if backend != "local" else _current_model
    try:
        if backend == "local":
            augmented = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
            payload = {
                "model": _current_model, "messages": augmented, "stream": True,
                "repetition_penalty": 1.15,  # discourage the model re-emitting a duplicate "Answer:" pass
            }
            if "vllm" not in LLM_BASE_URL:
                payload["keep_alive"] = "30m"  # Ollama-only: avoid a cold model reload between messages
            async with llm_client.stream("POST", f"{LLM_BASE_URL}/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        yield line + "\n\n"
        else:
            api_key = get_provider_credential(backend)
            if not api_key:
                raise RuntimeError(f"No API key configured for {backend}.")
            async for chunk in providers.STREAM_ADAPTERS[backend](model, SYSTEM_PROMPT, history, api_key):
                yield chunk
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
        elif backend != "local":
            stub = f"[{backend} error: {_exc}] Retrieved {n} chunk(s). " + (f"Top sources: {srcs}." if srcs else "No matches.")
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
        "strategy": strategy, "chunks": chunk_count, "model": model,
        "gpu": (await _gpu_status()) if backend == "local" else backend, "citations": citations,
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
    ext = get_external_settings()
    active = ext["model"] if ext["backend"] != "local" else _current_model
    return {"object": "list", "data": [
        {"id": active, "object": "model", "created": int(time.time()), "owned_by": "ekap"}
    ]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, user: UserContext = Depends(get_user_context)):
    t0 = time.perf_counter()
    body = await request.json()
    messages  = body.get("messages", [])
    do_stream = body.get("stream", True)

    user_question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    # ── External-LLM governance ──────────────────────────────────────────────
    # When an external provider is active, retrieval itself is capped to the
    # admin-configured allowed classifications — this is the single point
    # where "don't send Confidential/Restricted content off-box" is enforced,
    # regardless of the requesting user's own (possibly broader) role access.
    ext = get_external_settings()
    class_cap = ext["allowed_classifications"] if ext["backend"] != "local" else None

    # ── Security gate ────────────────────────────────────────────────────────
    permitted_ids = get_permitted_doc_ids(user, class_cap)
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

    # ── Audit ────────────────────────────────────────────────────────────────
    await audit(user.user_id, "QUERY", details={
        "query": user_question[:200],
        "strategy": strategy,
        "chunks": len(chunks),
        "doc_ids": [c["document_id"] for c in citations],
        "username": user.username,
        "roles": user.roles,
        "backend": ext["backend"],
    })

    query_counter.labels(status="ok").inc()
    query_latency.observe(time.perf_counter() - t0)

    if do_stream:
        return StreamingResponse(stream_llm(history, cid, citations, strategy, len(chunks), ext), media_type="text/event-stream")

    active_model = ext["model"] if ext["backend"] != "local" else _current_model
    try:
        if ext["backend"] == "local":
            augmented = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
            payload = {
                "model": _current_model, "messages": augmented, "stream": False,
                "repetition_penalty": 1.15,  # discourage the model re-emitting a duplicate "Answer:" pass
            }
            if "vllm" not in LLM_BASE_URL:
                payload["keep_alive"] = "30m"  # Ollama-only: avoid a cold model reload between messages
            resp = await llm_client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"]
        else:
            api_key = get_provider_credential(ext["backend"])
            if not api_key:
                raise RuntimeError(f"No API key configured for {ext['backend']}.")
            answer = await providers.COMPLETE_ADAPTERS[ext["backend"]](active_model, SYSTEM_PROMPT, history, api_key)
    except Exception as e:
        answer = f"[LLM unavailable: {e}] Retrieved {len(citations)} chunk(s) via {strategy}."

    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": active_model,
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
    # "model"/"backend"/"base_url"/"gpu" below describe the LOCAL Ollama/vLLM
    # setup specifically (admin's Active Model + GPU Setup cards still need
    # this regardless of what's actually live) — active_backend/active_model
    # are what's actually serving chat right now, accounting for an external
    # provider override. The Employee Portal must read the active_* fields,
    # not "model"/"backend", or it shows stale local info after a switch.
    ext = get_external_settings()
    local_backend_label = "vllm" if "vllm" in LLM_BASE_URL else "ollama"
    return {
        "model":            _current_model,
        "env_model":        LLM_MODEL,
        "base_url":         LLM_BASE_URL,
        "backend":          local_backend_label,
        "gpu":              await _gpu_status(),
        "show_model_name":  _show_model_name,
        "show_stats":       _show_stats,
        "show_timing":      _show_timing,
        "active_backend":   ext["backend"] if ext["backend"] != "local" else local_backend_label,
        "active_model":     ext["model"] if ext["backend"] != "local" else _current_model,
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


ALL_CLASSIFICATIONS = ["Public", "Internal", "Confidential", "Restricted"]
# Internal/Confidential/Restricted content is never allowed as external-LLM
# context — only Public may leave this network, and that's not an
# admin-configurable choice beyond it. Enforced here (not just disabled in the
# admin UI) so a direct API call can't bypass it — and re-applied on every
# read in get_external_settings() too, so a row saved before this policy
# tightened can't keep granting a since-revoked classification.
EXTERNAL_ALLOWED_CLASSIFICATIONS = ["Public"]
EXTERNAL_PROVIDERS = ["openai", "anthropic", "deepseek", "google", "grok"]
ALL_BACKENDS = ["local", *EXTERNAL_PROVIDERS]


def get_external_settings() -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT backend, model, allowed_classifications FROM external_llm_settings WHERE id = 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"backend": "local", "model": None, "allowed_classifications": ["Public"]}
    allowed = [c for c in row[2] if c in EXTERNAL_ALLOWED_CLASSIFICATIONS]
    return {"backend": row[0], "model": row[1], "allowed_classifications": allowed}


def get_provider_credential(provider: str) -> str | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT api_key_enc FROM external_llm_credentials WHERE provider = %s", (provider,))
            row = cur.fetchone()
    finally:
        conn.close()
    return providers.decrypt_secret(row[0]) if row else None


@app.get("/api/llm/external-config")
async def get_external_config(user: UserContext = Depends(get_user_context)):
    settings = get_external_settings()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT provider, added_by, added_at FROM external_llm_credentials")
            rows = cur.fetchall()
    finally:
        conn.close()
    configured = {r[0]: {"configured": True, "added_by": r[1], "added_at": r[2].isoformat()} for r in rows}
    settings["providers"] = {
        p: configured.get(p, {"configured": False}) for p in EXTERNAL_PROVIDERS
    }
    settings["all_classifications"] = ALL_CLASSIFICATIONS
    return settings


@app.post("/api/llm/external-config")
async def set_external_config(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body    = await request.json()
    backend = (body.get("backend") or "").strip()
    model   = (body.get("model") or "").strip()
    allowed = body.get("allowed_classifications")

    if backend not in ALL_BACKENDS:
        raise HTTPException(status_code=400, detail=f"backend must be one of {ALL_BACKENDS}.")
    if not isinstance(allowed, list) or not all(c in EXTERNAL_ALLOWED_CLASSIFICATIONS for c in allowed):
        raise HTTPException(
            status_code=400,
            detail=f"allowed_classifications must be a subset of {EXTERNAL_ALLOWED_CLASSIFICATIONS} — "
                   f"Internal/Confidential/Restricted content can never be sent to an external LLM.",
        )

    if backend != "local":
        if not model:
            raise HTTPException(status_code=400, detail="model is required for an external backend.")
        if get_provider_credential(backend) is None:
            raise HTTPException(
                status_code=400,
                detail=f"Add an API key for {backend} first (see External Provider Credentials below).",
            )

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO external_llm_settings (id, backend, model, allowed_classifications, updated_by) "
                "VALUES (1, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  backend = EXCLUDED.backend, model = EXCLUDED.model, "
                "  allowed_classifications = EXCLUDED.allowed_classifications, "
                "  updated_by = EXCLUDED.updated_by, updated_at = NOW()",
                (backend, model or None, allowed, user.username),
            )
        conn.commit()
    finally:
        conn.close()

    await audit(user.user_id, "EXTERNAL_LLM_CONFIG_CHANGE",
                details={"backend": backend, "model": model, "allowed_classifications": allowed})
    return get_external_settings()


@app.post("/api/llm/external-credentials")
async def add_external_credential(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body     = await request.json()
    provider = (body.get("provider") or "").strip()
    api_key  = (body.get("api_key") or "").strip()
    if provider not in EXTERNAL_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {EXTERNAL_PROVIDERS}.")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required.")

    try:
        encrypted = providers.encrypt_secret(api_key)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO external_llm_credentials (provider, api_key_enc, added_by) VALUES (%s, %s, %s) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "  api_key_enc = EXCLUDED.api_key_enc, added_by = EXCLUDED.added_by, added_at = NOW()",
                (provider, encrypted, user.username),
            )
        conn.commit()
    finally:
        conn.close()

    # Never logs the key itself — only that one was set, and by whom.
    await audit(user.user_id, "EXTERNAL_LLM_CREDENTIAL_SET", details={"provider": provider})
    return {"provider": provider, "configured": True}


@app.delete("/api/llm/external-credentials/{provider}")
async def remove_external_credential(provider: str, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    if provider not in EXTERNAL_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {EXTERNAL_PROVIDERS}.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM external_llm_credentials WHERE provider = %s", (provider,))
            deleted = cur.rowcount
            # Safety: don't leave the active backend pointed at a provider with
            # no key — fall back to local rather than fail every chat request.
            reset = False
            cur.execute("SELECT backend FROM external_llm_settings WHERE id = 1")
            row = cur.fetchone()
            if row and row[0] == provider:
                cur.execute(
                    "UPDATE external_llm_settings SET backend = 'local', model = NULL, "
                    "updated_by = %s, updated_at = NOW() WHERE id = 1",
                    (user.username,),
                )
                reset = True
        conn.commit()
    finally:
        conn.close()

    if not deleted:
        raise HTTPException(status_code=404, detail=f'No stored API key for "{provider}".')
    await audit(user.user_id, "EXTERNAL_LLM_CREDENTIAL_REMOVED", details={"provider": provider, "backend_reset": reset})
    return {"status": "removed", "provider": provider, "backend_reset": reset}


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


# ── Custom "Pull New Model" chips ──────────────────────────────────────────────
# Lets an admin add a chip for a model outside the built-in curated list
# (admin-ui's POPULAR_MODELS) without a code change. Sizes are resolved from
# the public Ollama registry once, at add time, and cached in custom_models.
REGISTRY_BASE   = "https://registry.ollama.ai/v2/library"
QUANT_SUFFIXES  = ["q4_0", "q4_K_M", "q5_K_M", "q8_0", "fp16"]


async def _registry_manifest_size(tag: str) -> int | None:
    lib, _, ver = tag.partition(":")
    ver = ver or "latest"
    url = f"{REGISTRY_BASE}/{lib}/manifests/{ver}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None
    return sum(layer.get("size", 0) for layer in data.get("layers", []))


def _guess_quant_tags(name: str, quant: str) -> list[str]:
    # Mirrors admin.js's buildPullName heuristic (most Ollama instruct/chat
    # models tag quantized variants "...-instruct-<quant>"), plus a bare
    # "-<quant>" fallback for models that don't follow it — best-effort only,
    # same disclaimer as the frontend's guessed-suffix path.
    if ":" not in name:
        guessed = f"{name}:instruct-{quant}"
    elif name.endswith("-instruct"):
        guessed = f"{name}-{quant}"
    else:
        guessed = f"{name}-instruct-{quant}"
    return [guessed, f"{name}-{quant}"]


# Best-effort HTML scrape of ollama.com's (undocumented) search page — there's
# no public JSON search API, so this is fragile against site markup changes by
# design; a parse failure just yields no suggestions, never breaks anything
# else. Lets an admin search "qwen3" and get back the real library name(s) and
# published sizes instead of having to already know the exact tag.
_SEARCH_ITEM_RE   = re.compile(r'^([^"]+)"')
_SEARCH_TITLE_RE  = re.compile(r'x-test-search-response-title>([^<]*)</span>')
_SEARCH_DESC_RE   = re.compile(r'<p class="[^"]*">([^<]*)</p>')
_SEARCH_SIZE_RE   = re.compile(r'x-test-size[^>]*>([^<]*)</span>')


@app.get("/api/llm/registry-search")
async def registry_search(q: str = "", user: UserContext = Depends(get_user_context)):
    import html as html_module
    term = q.strip()
    if len(term) < 2:
        return {"results": []}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://ollama.com/search", params={"q": term})
            resp.raise_for_status()
            body = resp.text
    except Exception as e:
        return {"results": [], "error": str(e)}

    results = []
    for block in body.split('<a href="/library/')[1:11]:
        m = _SEARCH_ITEM_RE.match(block)
        if not m:
            continue
        name  = m.group(1)
        title = m.group(1)
        if (t := _SEARCH_TITLE_RE.search(block)):
            title = html_module.unescape(t.group(1).strip())
        desc = ""
        if (d := _SEARCH_DESC_RE.search(block)):
            desc = html_module.unescape(d.group(1).strip())
        sizes = [html_module.unescape(s) for s in _SEARCH_SIZE_RE.findall(block)]
        results.append({"name": name, "title": title, "description": desc[:160], "sizes": sizes})
    return {"results": results}


@app.get("/api/llm/custom-models")
async def list_custom_models(user: UserContext = Depends(get_user_context)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, label, note, quant_sizes, added_by, added_at "
                "FROM custom_models ORDER BY added_at"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {"models": [
        {"name": r[0], "label": r[1], "note": r[2] or "", "quant_sizes": r[3],
         "added_by": r[4], "added_at": r[5].isoformat()}
        for r in rows
    ]}


@app.post("/api/llm/custom-models")
async def add_custom_model(request: Request, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    body  = await request.json()
    name  = (body.get("name") or "").strip()
    label = (body.get("label") or "").strip() or name
    note  = (body.get("note") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")

    default_size = await _registry_manifest_size(name)
    if default_size is None:
        raise HTTPException(
            status_code=400,
            detail=f'Could not find "{name}" on the Ollama registry. '
                   f'Check the exact tag on ollama.com/library.',
        )

    quant_sizes = {"": default_size}
    for quant in QUANT_SUFFIXES:
        for candidate in _guess_quant_tags(name, quant):
            size = await _registry_manifest_size(candidate)
            if size is not None:
                quant_sizes[quant] = size
                break

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custom_models (name, label, note, quant_sizes, added_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  label = EXCLUDED.label, note = EXCLUDED.note, "
                "  quant_sizes = EXCLUDED.quant_sizes, added_by = EXCLUDED.added_by, "
                "  added_at = NOW() "
                "RETURNING name, label, note, quant_sizes, added_by, added_at",
                (name, label, note, json.dumps(quant_sizes), user.username),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    await audit(user.user_id, "CUSTOM_MODEL_ADDED", details={"name": name})
    return {"name": row[0], "label": row[1], "note": row[2] or "", "quant_sizes": row[3],
            "added_by": row[4], "added_at": row[5].isoformat()}


@app.delete("/api/llm/custom-models/{name:path}")
async def remove_custom_model(name: str, user: UserContext = Depends(get_user_context)):
    from fastapi import HTTPException
    if not user.can_manage_documents():
        raise HTTPException(status_code=403, detail="Requires knowledge-manager or administrator role.")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM custom_models WHERE name = %s", (name,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail=f'"{name}" is not a custom model chip.')
    await audit(user.user_id, "CUSTOM_MODEL_REMOVED", details={"name": name})
    return {"status": "removed", "name": name}


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
