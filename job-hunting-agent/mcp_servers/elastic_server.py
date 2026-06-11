import datetime
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from soma_algorithm import (
    build_episode_from_memory,
    build_rewrite_guidance,
    extract_jd_fingerprint,
    extract_patterns_and_warnings,
    extract_resume_evidence,
    rerank_episode,
    stable_id,
    summarize_case,
)

app = FastAPI(title="Elasticsearch Job Retrieval Server", version="1.2.0")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = PROJECT_ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "scheduler_config.json"
LOCAL_JOBS_PATH = DATA_DIR / "elastic_local_jobs.jsonl"
LOCAL_EPISODES_PATH = DATA_DIR / "elastic_local_episodes.jsonl"
EXTRACTED_JDS_DIR = DATA_DIR / "extracted_jds"


def load_env_manually() -> None:
    for root in [PROJECT_ROOT, PARENT_ROOT]:
        for env_file in [".env", ".env.local"]:
            env_path = root / env_file
            if not env_path.exists():
                continue
            try:
                with env_path.open("r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            os.environ[key.strip()] = value.strip().strip('"').strip("'")
            except Exception as err:
                print(f"[Elastic] Failed to parse {env_path}: {err}")


def load_project_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as err:
        print(f"[Elastic] Failed to read scheduler_config.json: {err}")
        return {}


load_env_manually()


class SearchRequest(BaseModel):
    keywords: List[str]
    limit: int = 5
    location: str = "United States"
    date_posted: Optional[str] = None
    experience_level: Optional[List[str]] = None
    remote: Optional[List[str]] = None
    live_fallback: bool = True
    hybrid: bool = True


class JobDocument(BaseModel):
    job_id: Optional[str] = None
    company: str = "Unknown"
    role: str = "Unknown"
    job_description: str
    location: str = "Unknown"
    apply_link: Optional[str] = None
    source: str = "manual"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EpisodeSyncRequest(BaseModel):
    mongo_url: Optional[str] = None
    limit: int = 100
    status: Optional[str] = None
    persist_artifacts_to_mongo: bool = True


class SimilarEpisodeRequest(BaseModel):
    company: Optional[str] = None
    role: Optional[str] = None
    job_description: str
    resume_v0: str
    application_id: Optional[str] = None
    mongo_url: Optional[str] = None
    limit: int = 8
    retrieve_limit: int = 50
    include_low_faithfulness: bool = False


STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "into", "over", "under", "using",
    "you", "your", "our", "are", "will", "can", "have", "has", "job", "role", "team",
    "work", "skills", "experience", "requirements", "responsibilities", "candidate",
}

EMBEDDING_DIMS = 128


def config_value(*keys: str, default: str = "") -> str:
    cfg = load_project_config()
    config_aliases = {
        "ELASTIC_URL": ["elastic_backend_url", "elasticsearch_url"],
        "ELASTIC_CLOUD_ID": ["elastic_cloud_id"],
        "ELASTIC_API_KEY": ["elastic_api_key"],
        "ELASTIC_USER": ["elastic_user"],
        "ELASTIC_PASSWORD": ["elastic_password"],
        "ELASTIC_INDEX_NAME": ["elastic_index_name"],
        "ELASTIC_EPISODE_INDEX_NAME": ["elastic_episode_index_name"],
    }
    for key in keys:
        value = os.getenv(key)
        if not value:
            for alias in config_aliases.get(key, [key.lower(), key]):
                value = cfg.get(alias)
                if value:
                    break
        if value:
            return str(value)
    return default


def elastic_index_name() -> str:
    return config_value("ELASTIC_INDEX_NAME", default="job_descriptions")


def elastic_episode_index_name() -> str:
    return config_value("ELASTIC_EPISODE_INDEX_NAME", default="application_episodes")


def create_es_client():
    cloud_id = config_value("ELASTIC_CLOUD_ID")
    api_key = config_value("ELASTIC_API_KEY")
    elastic_url = config_value("ELASTIC_URL")
    user = config_value("ELASTIC_USER")
    password = config_value("ELASTIC_PASSWORD")
    try:
        if cloud_id and api_key:
            client = Elasticsearch(cloud_id=cloud_id, api_key=api_key, request_timeout=10)
        elif elastic_url and api_key:
            client = Elasticsearch(elastic_url, api_key=api_key, verify_certs=False, request_timeout=10)
        elif elastic_url:
            kwargs = {"verify_certs": False, "request_timeout": 10}
            if user:
                kwargs["basic_auth"] = (user, password)
            client = Elasticsearch(elastic_url, **kwargs)
        else:
            return None, "not_configured"
        if client.ping():
            return client, "connected"
        return None, "ping_failed"
    except Exception as err:
        print(f"[Elastic] connection failed: {err}")
        return None, f"connection_failed: {err}"


es_client, es_status = create_es_client()


def refresh_es_client():
    global es_client, es_status
    client, status = create_es_client()
    es_client = client
    es_status = status
    return client, status


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def stable_job_id(company: str, role: str, jd: str) -> str:
    raw = f"{company}|{role}|{jd[:500]}"
    return "job-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def tokenize(text: str) -> List[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}", text or "")
        if token.lower() not in STOPWORDS
    ]


def hashed_embedding(text: str, dims: int = EMBEDDING_DIMS) -> List[float]:
    vector = [0.0] * dims
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [round(value / norm, 6) for value in vector]


def add_job_embeddings(doc: Dict[str, Any]) -> Dict[str, Any]:
    text = " ".join([
        doc.get("company") or "",
        doc.get("role") or "",
        doc.get("location") or "",
        doc.get("job_description") or "",
    ])
    doc["job_description_embedding"] = hashed_embedding(text)
    return doc


def add_episode_embeddings(doc: Dict[str, Any]) -> Dict[str, Any]:
    jd_text = " ".join([
        doc.get("company") or "",
        doc.get("role_title") or "",
        doc.get("stack_cluster") or "",
        " ".join(doc.get("tech_stack_terms") or []),
        doc.get("jd_text") or "",
    ])
    responsibility_text = doc.get("responsibilities_text") or ""
    doc["jd_embedding"] = hashed_embedding(jd_text)
    doc["responsibility_embedding"] = hashed_embedding(responsibility_text)
    return doc


def infer_company_role_from_filename(path: Path) -> Dict[str, str]:
    stem = re.sub(r"^jd_\d+_", "", path.stem)
    text = stem.replace("__", "_").replace("_", " ").strip()
    parts = re.split(r"\s{2,}|\s-\s", text, maxsplit=1)
    if len(parts) == 2:
        return {"company": parts[0].strip() or "Unknown", "role": parts[1].strip() or text}
    words = text.split()
    if len(words) > 4:
        return {"company": " ".join(words[:2]), "role": " ".join(words[2:])}
    return {"company": "Local JD", "role": text or path.stem}


def parse_jd_header(content: str) -> Dict[str, str]:
    values = {}
    for line in (content or "").splitlines()[:12]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower()
        if normalized_key in {"company", "role", "location", "apply link", "apply_link", "apply url", "apply_url"}:
            values[normalized_key.replace(" ", "_")] = value.strip()
    return {
        "company": values.get("company", ""),
        "role": values.get("role", ""),
        "location": values.get("location", ""),
        "apply_link": values.get("apply_link") or values.get("apply_url") or "",
    }


def normalize_job_doc(job: Dict[str, Any]) -> Dict[str, Any]:
    jd = job.get("job_description") or job.get("description") or ""
    company = job.get("company") or "Unknown"
    role = job.get("role") or job.get("title") or "Unknown"
    job_id = job.get("job_id") or stable_job_id(company, role, jd)
    doc = {
        "job_id": str(job_id),
        "company": company,
        "role": role,
        "job_description": jd,
        "location": job.get("location") or "Unknown",
        "apply_link": job.get("apply_link") or job.get("apply_url") or "",
        "source": job.get("source") or "unknown",
        "indexed_at": job.get("indexed_at") or now_iso(),
        "metadata": job.get("metadata") or {},
    }
    return add_job_embeddings(doc)


def load_extracted_jd_docs() -> List[Dict[str, Any]]:
    docs = []
    if not EXTRACTED_JDS_DIR.exists():
        return docs
    for path in EXTRACTED_JDS_DIR.glob("*.txt"):
        try:
            content = path.read_text(encoding="utf-8-sig").strip()
        except Exception:
            continue
        if not content:
            continue
        inferred = infer_company_role_from_filename(path)
        header = parse_jd_header(content)
        docs.append(normalize_job_doc({
            "company": header.get("company") or inferred["company"],
            "role": header.get("role") or inferred["role"],
            "job_description": content,
            "location": header.get("location") or "Unknown",
            "apply_link": header.get("apply_link") or "",
            "source": "local_extracted_jd",
            "metadata": {"file": str(path)},
        }))
    return docs


def load_local_jsonl_docs() -> List[Dict[str, Any]]:
    if not LOCAL_JOBS_PATH.exists():
        return []
    docs = []
    with LOCAL_JOBS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                docs.append(normalize_job_doc(json.loads(line)))
            except Exception:
                continue
    return docs


def load_local_docs() -> List[Dict[str, Any]]:
    seen = set()
    docs = []
    for doc in load_local_jsonl_docs() + load_extracted_jd_docs():
        if doc["job_id"] in seen:
            continue
        seen.add(doc["job_id"])
        docs.append(doc)
    return docs


def load_local_episodes() -> List[Dict[str, Any]]:
    if not LOCAL_EPISODES_PATH.exists():
        return []
    docs = []
    with LOCAL_EPISODES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("episode_id"):
                docs.append(item)
    return docs


def write_local_episodes(docs: List[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    by_id = {doc.get("episode_id"): doc for doc in load_local_episodes() if doc.get("episode_id")}
    for doc in docs:
        if doc.get("episode_id"):
            by_id[doc["episode_id"]] = doc
    with LOCAL_EPISODES_PATH.open("w", encoding="utf-8") as f:
        for doc in by_id.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def ensure_episode_index(client=None):
    client = client or es_client
    if not client:
        return
    index = elastic_episode_index_name()
    try:
        if client.indices.exists(index=index):
            try:
                client.indices.put_mapping(index=index, properties={
                    "jd_embedding": {
                        "type": "dense_vector",
                        "dims": EMBEDDING_DIMS,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "responsibility_embedding": {
                        "type": "dense_vector",
                        "dims": EMBEDDING_DIMS,
                        "index": True,
                        "similarity": "cosine",
                    },
                })
            except Exception:
                pass
            return
        client.indices.create(index=index, mappings={
            "properties": {
                "episode_id": {"type": "keyword"},
                "application_id": {"type": "keyword"},
                "company": {"type": "text"},
                "role_title": {"type": "text"},
                "role_family": {"type": "keyword"},
                "seniority": {"type": "keyword"},
                "stack_cluster": {"type": "keyword"},
                "tech_stack_terms": {"type": "keyword"},
                "responsibilities_text": {"type": "text"},
                "jd_text": {"type": "text"},
                "outcome_label": {"type": "keyword"},
                "outcome_score": {"type": "float"},
                "faithfulness_score": {"type": "float"},
                "hallucination_risk": {"type": "float"},
                "updated_at": {"type": "date"},
                "jd_fingerprint": {"type": "object", "enabled": True},
                "resume_evidence_fingerprint": {"type": "object", "enabled": True},
                "rewrite_patterns": {"type": "object", "enabled": True},
                "eval_summary": {"type": "object", "enabled": True},
                "outcome": {"type": "object", "enabled": True},
                "jd_embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine",
                },
                "responsibility_embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        })
    except Exception as err:
        print(f"[Elastic] ensure_episode_index failed: {err}")


def index_episode_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc = add_episode_embeddings(dict(doc))
    client, _ = refresh_es_client()
    if client:
        ensure_episode_index(client)
        client.index(
            index=elastic_episode_index_name(),
            id=doc["episode_id"],
            document=doc,
            refresh=True,
        )
        return {"indexed": True, "backend": "elasticsearch", "episode_id": doc["episode_id"]}
    write_local_episodes([doc])
    return {"indexed": True, "backend": "local_episode_cache", "episode_id": doc["episode_id"]}


def candidate_terms_from_fingerprint(fp: Dict[str, Any]) -> List[str]:
    terms = [
        fp.get("role_family") or "",
        fp.get("seniority") or "",
        fp.get("stack_cluster") or "",
    ]
    terms.extend(item.get("canonical", "") for item in fp.get("tech_stack", []))
    terms.extend(fp.get("responsibilities") or [])
    return [term for term in terms if term]


def local_episode_prefilter_score(doc: Dict[str, Any], fp: Dict[str, Any]) -> float:
    query_terms = set(tokenize(" ".join(candidate_terms_from_fingerprint(fp))))
    doc_terms = set(tokenize(" ".join([
        doc.get("role_title") or "",
        doc.get("company") or "",
        doc.get("stack_cluster") or "",
        " ".join(doc.get("tech_stack_terms") or []),
        doc.get("responsibilities_text") or "",
        doc.get("jd_text") or "",
    ])))
    if not query_terms:
        return 0.0
    overlap = len(query_terms & doc_terms) / max(1, len(query_terms))
    cluster_bonus = 0.35 if fp.get("stack_cluster") and fp.get("stack_cluster") == doc.get("stack_cluster") else 0.0
    role_bonus = 0.2 if fp.get("role_family") and fp.get("role_family") == doc.get("role_family") else 0.0
    outcome_bonus = min(0.25, float(doc.get("outcome_score") or 0) * 0.25)
    return round(overlap + cluster_bonus + role_bonus + outcome_bonus, 4)


def search_episode_elasticsearch(fp: Dict[str, Any], limit: int, include_low_faithfulness: bool = False) -> List[Dict[str, Any]]:
    client, _ = refresh_es_client()
    if not client:
        return []
    ensure_episode_index(client)
    terms = candidate_terms_from_fingerprint(fp)
    filters = []
    if fp.get("role_family"):
        filters.append({"term": {"role_family": fp.get("role_family")}})
    if not include_low_faithfulness:
        filters.extend([
            {"range": {"faithfulness_score": {"gte": 0.75}}},
            {"range": {"hallucination_risk": {"lte": 0.30}}},
        ])
    should = [
        {"terms": {"tech_stack_terms": [item.get("canonical") for item in fp.get("tech_stack", []) if item.get("canonical")], "boost": 3.0}},
        {"term": {"stack_cluster": {"value": fp.get("stack_cluster") or "", "boost": 4.0}}},
        {"match": {"responsibilities_text": {"query": " ".join(fp.get("responsibilities") or []), "boost": 1.5}}},
        {"match": {"jd_text": {"query": " ".join(terms), "boost": 1.0}}},
    ]
    bool_query = {
        "query": {
            "bool": {
                "filter": filters,
                "should": should,
                "minimum_should_match": 1,
            }
        },
    }
    query = {
        "query": {
            "script_score": {
                "query": bool_query["query"],
                "script": {
                    "source": "double a = doc['jd_embedding'].size() == 0 ? 0.0 : cosineSimilarity(params.jd_vector, 'jd_embedding'); double b = doc['responsibility_embedding'].size() == 0 ? 0.0 : cosineSimilarity(params.resp_vector, 'responsibility_embedding'); return _score + Math.max(0, a) * 1.5 + Math.max(0, b) * 0.8;",
                    "params": {
                        "jd_vector": hashed_embedding(" ".join(terms)),
                        "resp_vector": hashed_embedding(" ".join(fp.get("responsibilities") or [])),
                    },
                },
            }
        },
        "sort": [
            {"_score": "desc"},
            {"outcome_score": "desc"},
            {"updated_at": "desc"},
        ],
        "size": limit,
    }
    try:
        res = client.search(index=elastic_episode_index_name(), body=query)
    except Exception as err:
        try:
            fallback_query = {
                **bool_query,
                "sort": [
                    {"_score": "desc"},
                    {"outcome_score": "desc"},
                    {"updated_at": "desc"},
                ],
                "size": limit,
            }
            res = client.search(index=elastic_episode_index_name(), body=fallback_query)
        except Exception as fallback_err:
            print(f"[Elastic] episode query failed: {fallback_err}")
            return []
    candidates = []
    for hit in res.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        source["retrieval_score"] = hit.get("_score", 0)
        source["retrieval_backend"] = "elasticsearch_hybrid_vector"
        candidates.append(source)
    return candidates


def search_episode_local(fp: Dict[str, Any], limit: int, include_low_faithfulness: bool = False) -> List[Dict[str, Any]]:
    scored = []
    for doc in load_local_episodes():
        if not include_low_faithfulness:
            if float(doc.get("faithfulness_score") or 0.0) < 0.75:
                continue
            if float(doc.get("hallucination_risk") or 0.0) > 0.30:
                continue
        score = local_episode_prefilter_score(doc, fp)
        if score <= 0:
            continue
        item = dict(doc)
        item["retrieval_score"] = score
        item["retrieval_backend"] = "local_episode_cache"
        scored.append(item)
    scored.sort(key=lambda item: item["retrieval_score"], reverse=True)
    return scored[:limit]


def score_doc(doc: Dict[str, Any], query_terms: List[str], location: str = "") -> float:
    text = f"{doc.get('company', '')} {doc.get('role', '')} {doc.get('job_description', '')}".lower()
    doc_terms = set(tokenize(text))
    query_set = set(query_terms)
    if not query_set:
        return 0.0
    overlap = len(query_set & doc_terms)
    phrase_bonus = sum(1 for term in query_set if term in text) * 0.25
    role_bonus = sum(1 for term in query_set if term in (doc.get("role") or "").lower()) * 0.6
    location_bonus = 0.2 if location and location.lower() in (doc.get("location") or "").lower() else 0.0
    return round(overlap + phrase_bonus + role_bonus + location_bonus, 4)


def search_local(req: SearchRequest) -> List[Dict[str, Any]]:
    query = " ".join(req.keywords)
    query_terms = tokenize(query)
    scored = []
    for doc in load_local_docs():
        score = score_doc(doc, query_terms, req.location)
        if score <= 0:
            continue
        item = dict(doc)
        item["score"] = score
        item["search_backend"] = "local_jd_cache"
        scored.append(item)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: req.limit]


def ensure_index():
    client = es_client
    if not client:
        return
    index = elastic_index_name()
    try:
        if client.indices.exists(index=index):
            try:
                client.indices.put_mapping(index=index, properties={
                    "job_description_embedding": {
                        "type": "dense_vector",
                        "dims": EMBEDDING_DIMS,
                        "index": True,
                        "similarity": "cosine",
                    }
                })
            except Exception:
                pass
            return
        client.indices.create(index=index, mappings={
            "properties": {
                "company": {"type": "text"},
                "role": {"type": "text"},
                "job_description": {"type": "text"},
                "location": {"type": "text"},
                "apply_link": {"type": "keyword"},
                "source": {"type": "keyword"},
                "indexed_at": {"type": "date"},
                "metadata": {"type": "object", "enabled": True},
                "job_description_embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        })
    except Exception as err:
        print(f"[Elastic] ensure_index failed: {err}")


def search_elasticsearch(req: SearchRequest) -> List[Dict[str, Any]]:
    client, _ = refresh_es_client()
    if not client:
        return []
    index = elastic_index_name()
    query_str = " ".join(req.keywords)
    keyword_query = {
        "query": {
            "multi_match": {
                "query": query_str,
                "fields": ["role^3", "company^2", "job_description", "location"],
                "type": "best_fields",
            }
        },
        "size": req.limit,
    }
    query = keyword_query
    if req.hybrid:
        query = {
            "query": {
                "script_score": {
                    "query": keyword_query["query"],
                    "script": {
                        "source": "double v = doc['job_description_embedding'].size() == 0 ? 0.0 : cosineSimilarity(params.query_vector, 'job_description_embedding'); return _score + Math.max(0, v) * 1.5;",
                        "params": {"query_vector": hashed_embedding(query_str)},
                    },
                }
            },
            "size": req.limit,
        }
    try:
        res = client.search(index=index, body=query)
    except Exception as err:
        if req.hybrid:
            try:
                res = client.search(index=index, body=keyword_query)
            except Exception as fallback_err:
                print(f"[Elastic] index query failed: {fallback_err}")
                return []
        else:
            print(f"[Elastic] index query failed: {err}")
            return []
    jobs = []
    for hit in res.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        jobs.append({
            "job_id": hit.get("_id") or source.get("job_id"),
            "company": source.get("company", "Unknown"),
            "role": source.get("role", "Unknown"),
            "job_description": source.get("job_description", ""),
            "location": source.get("location", "Unknown"),
            "apply_link": source.get("apply_link", ""),
            "score": hit.get("_score", 0),
            "search_backend": "elasticsearch_hybrid" if req.hybrid else "elasticsearch",
        })
    return jobs


def search_live(req: SearchRequest) -> List[Dict[str, Any]]:
    playwright_url = os.getenv("PLAYWRIGHT_URL", "http://localhost:8004").rstrip("/")
    res = requests.post(
        f"{playwright_url}/search-linkedin",
        json={
            "keywords": req.keywords,
            "location": req.location,
            "date_posted": req.date_posted,
            "experience_level": req.experience_level,
            "remote": req.remote,
        },
        timeout=45,
    )
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"Playwright LinkedIn search failed: {res.text}")
    jobs = res.json().get("jobs", [])[: req.limit]
    for job in jobs:
        job["search_backend"] = "playwright_linkedin_live"
    return jobs


def persist_local_job(doc: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCAL_JOBS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def configured_mongo_url() -> str:
    cfg = load_project_config()
    return (os.getenv("MONGO_URL") or cfg.get("mongo_url") or "http://localhost:8001").rstrip("/")


def configured_arize_url() -> str:
    cfg = load_project_config()
    return (os.getenv("ARIZE_URL") or cfg.get("arize_url") or "http://localhost:8003").rstrip("/")


def mongo_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, base_url: Optional[str] = None, timeout: int = 12) -> Optional[Dict[str, Any]]:
    url = f"{(base_url or configured_mongo_url()).rstrip('/')}{path}"
    try:
        if method.upper() == "GET":
            res = requests.get(url, timeout=timeout)
        elif method.upper() == "POST":
            res = requests.post(url, json=payload or {}, timeout=timeout)
        else:
            return None
        if res.status_code >= 400:
            print(f"[Elastic] Mongo request failed {res.status_code}: {url} {res.text[:300]}")
            return None
        return res.json()
    except Exception as err:
        print(f"[Elastic] Mongo request failed: {url} {err}")
        return None


def persist_episode_artifacts_to_mongo(episode: Dict[str, Any], mongo_url: Optional[str]) -> None:
    app_id = episode.get("application_id")
    if not app_id:
        return
    artifacts = [
        ("soma_job_fingerprint", episode.get("jd_fingerprint") or {}),
        ("soma_resume_evidence_map", episode.get("resume_evidence_fingerprint") or {}),
        ("soma_application_episode", {
            key: value
            for key, value in episode.items()
            if key not in {"resume_v0_text", "resume_v1_text", "jd_text"}
        }),
    ]
    for artifact_type, payload in artifacts:
        mongo_request(
            "POST",
            f"/applications/{app_id}/artifacts",
            {
                "artifact_type": artifact_type,
                "payload": payload,
                "metadata": {"source": "soma_sync", "indexed_at": now_iso()},
            },
            base_url=mongo_url,
            timeout=5,
        )


def trace_soma_retrieval(trace_payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(trace_payload or {})
    payload["metadata"] = metadata or {}
    try:
        res = requests.post(
            f"{configured_arize_url()}/trace-retriever",
            json=payload,
            timeout=15,
        )
        if res.status_code >= 400:
            return {"exported": False, "error": f"{res.status_code}: {res.text[:300]}"}
        data = res.json()
        phoenix = data.get("phoenix_export") or {}
        return {
            "trace_id": data.get("trace_id"),
            "exported": bool(phoenix.get("exported")),
            "phoenix_export": phoenix,
        }
    except Exception as err:
        return {"exported": False, "error": str(err)}


ensure_index()


@app.get("/health")
def health():
    client, status = refresh_es_client()
    return {
        "ok": True,
        "service": "elastic-job-retrieval",
        "elasticsearch_configured": bool(client),
        "elasticsearch_status": status,
        "index": elastic_index_name(),
        "episode_index": elastic_episode_index_name(),
        "hybrid_search_enabled": True,
        "embedding_dims": EMBEDDING_DIMS,
        "local_docs": len(load_local_docs()),
        "local_extracted_jds": len(load_extracted_jd_docs()),
        "local_jsonl_jobs": len(load_local_jsonl_docs()),
        "local_episodes": len(load_local_episodes()),
    }


@app.post("/search")
def search_jobs(req: SearchRequest):
    if not req.keywords:
        raise HTTPException(status_code=400, detail="Keywords list cannot be empty")

    jobs = search_elasticsearch(req)
    if jobs:
        return {"backend": "elasticsearch", "jobs": jobs}

    jobs = search_local(req)
    if jobs:
        return {"backend": "local_jd_cache", "jobs": jobs}

    if not req.live_fallback:
        return {"backend": "none", "jobs": []}

    try:
        return {"backend": "playwright_linkedin_live", "jobs": search_live(req)}
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"No Elasticsearch/local hits, and live fallback failed: {err}")


@app.post("/index-job")
def index_job(job: JobDocument):
    doc = normalize_job_doc(job.model_dump() if hasattr(job, "model_dump") else job.dict())
    client, _ = refresh_es_client()
    if client:
        ensure_index()
        client.index(index=elastic_index_name(), id=doc["job_id"], document=doc, refresh=True)
        return {"indexed": True, "backend": "elasticsearch", "job_id": doc["job_id"]}
    persist_local_job(doc)
    return {"indexed": True, "backend": "local_jd_cache", "job_id": doc["job_id"]}


@app.post("/bulk-index")
def bulk_index(jobs: List[JobDocument]):
    indexed = []
    client, _ = refresh_es_client()
    for job in jobs:
        doc = normalize_job_doc(job.model_dump() if hasattr(job, "model_dump") else job.dict())
        if client:
            ensure_index()
            client.index(index=elastic_index_name(), id=doc["job_id"], document=doc)
        else:
            persist_local_job(doc)
        indexed.append(doc["job_id"])
    if client:
        client.indices.refresh(index=elastic_index_name())
    return {"indexed": len(indexed), "backend": "elasticsearch" if client else "local_jd_cache", "job_ids": indexed}


@app.post("/seed-local")
def seed_local_to_elasticsearch():
    docs = load_local_docs()
    client, _ = refresh_es_client()
    if not client:
        return {"seeded": 0, "backend": "local_jd_cache", "message": "Elasticsearch is not configured; local cache is already available.", "local_docs": len(docs)}
    ensure_index()
    for doc in docs:
        client.index(index=elastic_index_name(), id=doc["job_id"], document=doc)
    client.indices.refresh(index=elastic_index_name())
    return {"seeded": len(docs), "backend": "elasticsearch", "index": elastic_index_name()}


@app.post("/seed-soma-demo-episodes")
def seed_soma_demo_episodes():
    config = load_project_config()
    resume_v0 = config.get("resume_v0") or """
Built Python automation scripts, SQL dashboards, Redis caching, Docker containerized services, and REST API gateways.
"""
    specs = [
        {
            "application_id": "soma-demo-python-backend-interview",
            "company": "SOMA Demo Cloud",
            "role_title": "Backend Software Engineer Intern",
            "status": "Interview",
            "jd_text": "Backend intern building Python REST APIs with Django, PostgreSQL, Redis, Docker, and database-backed services.",
            "eval_summary": {"faithfulness_score": 0.94, "jd_match_score": 0.88, "hallucination_risk": 0.04, "verdict": "approve"},
            "outcome": {"label": "INTERVIEW", "score": 1.0, "confidence": 0.95, "source": "demo_seed"},
            "rewrite_patterns": [],
        },
        {
            "application_id": "soma-demo-kubernetes-interview-blocked",
            "company": "SOMA Demo Infra",
            "role_title": "Backend Platform Intern",
            "status": "Interview",
            "jd_text": "Platform backend role using Python, Kubernetes, AWS, Docker, microservices, and deployment pipelines.",
            "eval_summary": {"faithfulness_score": 0.91, "jd_match_score": 0.82, "hallucination_risk": 0.08, "verdict": "approve"},
            "outcome": {"label": "INTERVIEW", "score": 1.0, "confidence": 0.9, "source": "demo_seed"},
            "rewrite_patterns": [
                {
                    "pattern_id": "pat_demo_kubernetes_deployment",
                    "name": "Emphasize Kubernetes deployment experience",
                    "stack_cluster": "backend_python_kubernetes",
                    "applicability_conditions": {
                        "jd_should_contain": ["python", "kubernetes", "aws"],
                        "resume_must_have_evidence": ["kubernetes", "deployment"],
                    },
                    "rewrite_instruction": "Emphasize Kubernetes deployment pipelines only when resume_v0 contains direct Kubernetes evidence.",
                    "risk_constraints": ["Do not add Kubernetes or AWS without direct resume evidence."],
                }
            ],
        },
        {
            "application_id": "soma-demo-cloud-keyword-rejection",
            "company": "SOMA Demo Risk",
            "role_title": "Cloud Backend Engineer",
            "status": "Rejected",
            "jd_text": "Backend role asking for Python, AWS, Kubernetes, Docker, distributed systems, and production microservices.",
            "eval_summary": {"faithfulness_score": 0.80, "jd_match_score": 0.86, "hallucination_risk": 0.25, "verdict": "revise"},
            "outcome": {"label": "REJECTION", "score": 0.0, "confidence": 0.8, "source": "demo_seed"},
            "rewrite_patterns": [],
        },
    ]

    docs = []
    for spec in specs:
        jd_fp = extract_jd_fingerprint(spec["company"], spec["role_title"], spec["jd_text"])
        resume_fp = extract_resume_evidence(resume_v0, jd_fp)
        doc = {
            "episode_id": stable_id("ep", spec["application_id"], spec["company"], spec["role_title"]),
            "application_id": spec["application_id"],
            "company": spec["company"],
            "role_title": spec["role_title"],
            "status": spec["status"],
            "apply_link": "",
            "source_url": "soma_demo_seed",
            "jd_text": spec["jd_text"],
            "resume_v0_text": resume_v0,
            "resume_v1_text": "",
            "jd_fingerprint": jd_fp,
            "resume_evidence_fingerprint": resume_fp,
            "rewrite_actions": [],
            "eval_summary": spec["eval_summary"],
            "outcome": spec["outcome"],
            "role_family": jd_fp.get("role_family"),
            "seniority": jd_fp.get("seniority"),
            "stack_cluster": jd_fp.get("stack_cluster"),
            "tech_stack_terms": [item.get("canonical") for item in jd_fp.get("tech_stack", []) if item.get("canonical")],
            "responsibilities_text": " ".join(jd_fp.get("responsibilities") or []),
            "outcome_score": spec["outcome"]["score"],
            "outcome_label": spec["outcome"]["label"],
            "faithfulness_score": spec["eval_summary"]["faithfulness_score"],
            "hallucination_risk": spec["eval_summary"]["hallucination_risk"],
            "rewrite_patterns": spec["rewrite_patterns"],
            "updated_at": now_iso(),
            "metadata": {"demo_seed": True},
        }
        docs.append(doc)

    results = [index_episode_doc(doc) for doc in docs]
    return {
        "seeded": len(results),
        "backend": "elasticsearch" if es_client else "local_episode_cache",
        "episode_index": elastic_episode_index_name(),
        "episodes": [
            {
                "episode_id": doc["episode_id"],
                "company": doc["company"],
                "role_title": doc["role_title"],
                "stack_cluster": doc["stack_cluster"],
                "outcome_label": doc["outcome_label"],
            }
            for doc in docs
        ],
    }


@app.post("/sync-mongo-episodes")
def sync_mongo_episodes(req: EpisodeSyncRequest):
    mongo_url = (req.mongo_url or configured_mongo_url()).rstrip("/")
    path = f"/applications?limit={max(1, min(req.limit, 500))}"
    if req.status:
        path += f"&status={req.status}"
    applications_payload = mongo_request("GET", path, base_url=mongo_url, timeout=15)
    if not applications_payload:
        raise HTTPException(status_code=502, detail=f"Could not read applications from Mongo memory server at {mongo_url}")

    indexed = []
    skipped = []
    for app_doc in applications_payload.get("applications", []):
        app_id = str(app_doc.get("id") or app_doc.get("_id") or "")
        if not app_id:
            continue
        memory = mongo_request("GET", f"/applications/{app_id}/memory", base_url=mongo_url, timeout=10)
        if not memory:
            skipped.append({"application_id": app_id, "reason": "memory_not_found"})
            continue
        episode = build_episode_from_memory(memory)
        if not episode:
            skipped.append({"application_id": app_id, "reason": "episode_build_failed"})
            continue
        result = index_episode_doc(episode)
        indexed.append({
            "episode_id": episode["episode_id"],
            "application_id": app_id,
            "company": episode.get("company"),
            "role_title": episode.get("role_title"),
            "backend": result.get("backend"),
            "stack_cluster": episode.get("stack_cluster"),
        })
        if req.persist_artifacts_to_mongo:
            persist_episode_artifacts_to_mongo(episode, mongo_url)

    return {
        "synced": len(indexed),
        "skipped": skipped,
        "episodes": indexed,
        "backend": "elasticsearch" if es_client else "local_episode_cache",
        "episode_index": elastic_episode_index_name(),
        "mongo_url": mongo_url,
    }


@app.post("/retrieve-similar-episodes")
def retrieve_similar_episodes(req: SimilarEpisodeRequest):
    if not req.job_description:
        raise HTTPException(status_code=400, detail="job_description is required")
    if not req.resume_v0:
        raise HTTPException(status_code=400, detail="resume_v0 is required")

    current_jd_fp = extract_jd_fingerprint(req.company or "", req.role or "", req.job_description)
    current_resume_fp = extract_resume_evidence(req.resume_v0, current_jd_fp)

    candidates = search_episode_elasticsearch(
        current_jd_fp,
        limit=max(req.retrieve_limit, req.limit),
        include_low_faithfulness=req.include_low_faithfulness,
    )
    backend = candidates[0].get("retrieval_backend", "elasticsearch") if candidates else "local_episode_cache"
    if not candidates:
        candidates = search_episode_local(
            current_jd_fp,
            limit=max(req.retrieve_limit, req.limit),
            include_low_faithfulness=req.include_low_faithfulness,
        )

    reranked = [
        rerank_episode(current_jd_fp, current_resume_fp, candidate)
        for candidate in candidates
        if candidate.get("episode_id")
    ]
    reranked.sort(key=lambda item: item.get("case_utility", 0.0), reverse=True)
    top_cases = reranked[: max(1, req.limit)]
    pattern_payload = extract_patterns_and_warnings(top_cases, current_resume_fp)
    guidance = build_rewrite_guidance(
        pattern_payload.get("selected_patterns") or [],
        pattern_payload.get("warnings") or [],
    )

    trace_payload = {
        "span_name": "retrieve_similar_stack_experience",
        "span_kind": "RETRIEVER",
        "input": {
            "application_id": req.application_id,
            "company": req.company,
            "role": req.role,
            "current_stack_cluster": current_jd_fp.get("stack_cluster"),
            "current_tech_stack": [
                item.get("canonical")
                for item in current_jd_fp.get("tech_stack", [])
                if item.get("canonical")
            ],
        },
        "retrieved_cases": [summarize_case(case) for case in top_cases],
        "selected_patterns": [
            {
                "pattern_id": pattern.get("pattern_id"),
                "current_applicability_score": pattern.get("current_applicability_score"),
                "source_episode_ids": pattern.get("source_episode_ids", []),
            }
            for pattern in pattern_payload.get("selected_patterns", [])
        ],
    }
    arize_trace = trace_soma_retrieval(trace_payload, metadata={
        "backend": backend,
        "algorithm": "SOMA",
        "retrieved_count": len(candidates),
    })

    if req.application_id and req.mongo_url:
        mongo_url = req.mongo_url.rstrip("/")
        mongo_request(
            "POST",
            f"/applications/{req.application_id}/artifacts",
            {
                "artifact_type": "soma_retrieval_result",
                "payload": {
                    "current_jd_fingerprint": current_jd_fp,
                    "current_resume_evidence": current_resume_fp,
                    "retrieved_cases": [summarize_case(case) for case in top_cases],
                    "selected_patterns": pattern_payload.get("selected_patterns", []),
                    "warnings": pattern_payload.get("warnings", []),
                    "rewrite_guidance": guidance,
                    "arize_trace": arize_trace,
                },
                "metadata": {"source": "retrieve_similar_episodes", "backend": backend},
            },
            base_url=mongo_url,
            timeout=5,
        )

    return {
        "algorithm": "SOMA",
        "backend": backend,
        "current_jd_fingerprint": current_jd_fp,
        "current_resume_evidence": current_resume_fp,
        "retrieved_count": len(candidates),
        "reranked_cases": [summarize_case(case) for case in top_cases],
        "positive_cases": [summarize_case(case) for case in pattern_payload.get("positive_cases", [])],
        "blocked_positive_cases": [summarize_case(case) for case in pattern_payload.get("blocked_positive_cases", [])],
        "caution_cases": [summarize_case(case) for case in pattern_payload.get("caution_cases", [])],
        "selected_patterns": pattern_payload.get("selected_patterns", []),
        "warnings": pattern_payload.get("warnings", []),
        "rewrite_guidance": guidance,
        "arize_span_payload": trace_payload,
        "arize_trace": arize_trace,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("ELASTIC_SERVER_PORT", 8002))
    uvicorn.run(app, host="0.0.0.0", port=port)
