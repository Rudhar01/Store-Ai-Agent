"""All configuration comes from environment variables (see .env.example)."""
import json
import os


def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _json(name: str) -> dict:
    v = os.environ.get(name)
    return json.loads(v) if v else {}


# --- auth
RAG_API_KEY = _s("RAG_API_KEY")

# --- vector store
QDRANT_URL = _s("QDRANT_URL")                      # Qdrant Cloud / server. Empty -> embedded (dev only)
QDRANT_API_KEY = _s("QDRANT_API_KEY") or None
QDRANT_PATH = _s("QDRANT_PATH", "./qdrant_data")
COLLECTION = _s("QDRANT_COLLECTION", "store_knowledge") or "store_knowledge"

# --- dense embeddings (any OpenAI-compatible /embeddings endpoint)
EMBED_BASE_URL = _s("EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/")
EMBED_API_KEY = _s("EMBEDDING_API_KEY")
EMBED_MODEL = _s("EMBEDDING_MODEL", "text-embedding-3-small")
EMBED_DIMENSIONS = _i("EMBEDDING_DIMENSIONS", 0) or None   # optional (OpenAI v3 models can shorten vectors)
EMBED_DOC_PREFIX = _s("EMBED_DOC_PREFIX")                  # e.g. "passage: " for e5 models
EMBED_QUERY_PREFIX = _s("EMBED_QUERY_PREFIX")              # e.g. "query: " for e5 models
EMBED_BATCH = _i("EMBED_BATCH", 64)
EMBED_TIMEOUT_S = _f("EMBED_TIMEOUT_S", 15)

# --- optional LLM for query rewriting / translation / expansion (any OpenAI-compatible chat endpoint)
LLM_BASE_URL = _s("LLM_BASE_URL").rstrip("/")
LLM_API_KEY = _s("LLM_API_KEY")
LLM_MODEL = _s("LLM_MODEL")
LLM_EXTRA_BODY = _json("LLM_EXTRA_BODY")                   # e.g. {"chat_template_kwargs":{"enable_thinking":false}} for Qwen3 on vLLM
LLM_TIMEOUT_S = _f("LLM_TIMEOUT_S", 2.5)

# --- optional reranker (Cohere / Jina / vLLM style  POST {base}/rerank)
RERANK_BASE_URL = _s("RERANK_BASE_URL").rstrip("/")
RERANK_API_KEY = _s("RERANK_API_KEY")
RERANK_MODEL = _s("RERANK_MODEL")
RERANK_TIMEOUT_S = _f("RERANK_TIMEOUT_S", 2.0)

# --- Firecrawl (cloud, or self-hosted: http://host:3002/v2)
FIRECRAWL_BASE_URL = _s("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v2").rstrip("/")
FIRECRAWL_API_KEY = _s("FIRECRAWL_API_KEY")
MAX_CRAWL_PAGES = _i("MAX_CRAWL_PAGES", 100)
CRAWL_TIMEOUT_S = _i("CRAWL_TIMEOUT_S", 600)
CRAWL_POLL_S = _f("CRAWL_POLL_S", 3)
ALLOWED_CRAWL_HOSTS = [h.strip().lower() for h in _s("ALLOWED_CRAWL_HOSTS").split(",") if h.strip()]

# --- indexing / retrieval behaviour
DOC_LANGUAGE = _s("DOC_LANGUAGE", "en").lower()            # language your documents are written in
QUERY_TRANSLATION = _s("QUERY_TRANSLATION", "non_english").lower()   # off | non_english | always (needs LLM_*)
CHUNK_CHARS = _i("CHUNK_CHARS", 1000)
CANDIDATES = _i("CANDIDATES", 20)                          # per-query candidates fetched before scoring
MIN_SCORE_DENSE = _f("MIN_SCORE_DENSE", 0.0)               # cosine cutoff when no reranker ran. Tune with eval/run_eval.py
MIN_SCORE_RERANK = _f("MIN_SCORE_RERANK", 0.0)             # relevance cutoff when the reranker ran

LLM_ENABLED = bool(LLM_BASE_URL and LLM_MODEL)
RERANK_ENABLED = bool(RERANK_BASE_URL and RERANK_MODEL)

# --- retrieval pipeline
QUERY_EXPANSIONS = _i("QUERY_EXPANSIONS", 0)               # extra LLM paraphrases per query (0 = off). Needs LLM_*
RRF_K = _i("RRF_K", 60)                                    # reciprocal-rank-fusion constant
DEFAULT_DEADLINE_MS = _i("DEFAULT_DEADLINE_MS", 4000)      # optional steps (LLM / rerank) are skipped when time is short
RERANK_CANDIDATES = _i("RERANK_CANDIDATES", 12)            # how many fused candidates are sent to the reranker
MAX_TOP_K = _i("MAX_TOP_K", 10)

# --- ingestion limits
MAX_DOC_CHARS = _i("MAX_DOC_CHARS", 400_000)
INGEST_CONCURRENCY = _i("INGEST_CONCURRENCY", 4)
