"""Local retrieval over extracted class-rule excerpts: keyword, and hybrid when an embedder is attached.

Which excerpts a report gets is data, not code: an optional topics file maps
the observation kind the agent chose (or wording in free text) to a search
query and, optionally, to the ID prefixes (chapter/section ranges) it may draw
from. Any clause set can be loaded; nothing here names a specific clause.

Keyword ranking is SQLite FTS5 bm25. With a `SemanticIndex`, the same
candidates are also ranked by embedding similarity and the two rankings are
fused with reciprocal rank fusion (RRF), so exact terms and clause numbers
still count while paraphrases and particles no longer decide recall alone.
"""
import array
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Protocol

TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[가-힣]+")
HANGUL = re.compile(r"^[가-힣]{2,}$")
REQUIRED_FIELDS = ("id", "clause", "title", "keywords", "text")
TABLE_ID = re.compile(r"^(?P<base>.+)-TABLE-[^-]+$")
DEFAULT_LIMIT = 5
RRF_K = 60
RANKED_PER_LIST = 50
MAX_PER_CLAUSE = 2
EMBED_BATCH = 100
MAX_EMBED_CHARS = 6000


class EmbeddingClient(Protocol):
    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...


def unit_vector(vector) -> tuple[float, ...]:
    if not vector or not all(type(value) in (float, int) and math.isfinite(value) for value in vector):
        raise ValueError("Invalid embedding vector")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return tuple(value / norm for value in vector)


class VectorCache:
    """Embedding vectors keyed by model and the SHA-256 of the exact embedded text."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("CREATE TABLE IF NOT EXISTS rule_vectors (model TEXT NOT NULL, text_sha256 TEXT NOT NULL,"
                           " vector BLOB NOT NULL, PRIMARY KEY (model, text_sha256))")
        self._conn.commit()

    def get(self, model: str, hashes: list[str]) -> dict[str, tuple[float, ...]]:
        found = {}
        for start in range(0, len(hashes), 500):
            chunk = hashes[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for key, blob in self._conn.execute(
                    f"SELECT text_sha256, vector FROM rule_vectors WHERE model = ? AND text_sha256 IN ({marks})",
                    (model, *chunk)):
                found[key] = tuple(array.array("f", blob))
        return found

    def put(self, model: str, vectors: dict[str, tuple[float, ...]]):
        with self._conn:
            self._conn.executemany("INSERT OR REPLACE INTO rule_vectors VALUES (?, ?, ?)",
                                   [(model, key, array.array("f", value).tobytes()) for key, value in vectors.items()])

    def close(self):
        self._conn.close()


class SemanticIndex:
    """Embeds texts once per model and caches them; float32 storage, unit-normalized on read."""

    def __init__(self, client: EmbeddingClient, model: str, cache: VectorCache):
        self.client, self.model, self.cache = client, model, cache
        self.embedded_texts = 0

    def vectors(self, texts: list[str]) -> list[tuple[float, ...]]:
        texts = [text[:MAX_EMBED_CHARS] for text in texts]
        keys = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        found = self.cache.get(self.model, sorted(set(keys)))
        missing = list(dict.fromkeys((key, text) for key, text in zip(keys, texts) if key not in found))
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start:start + EMBED_BATCH]
            vectors = self.client.embed(self.model, [text for _, text in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding client did not return one vector per text")
            fresh = {key: unit_vector(vector) for (key, _), vector in zip(batch, vectors)}
            self.cache.put(self.model, fresh)
            found.update(fresh)
            self.embedded_texts += len(batch)
        return [unit_vector(found[key]) for key in keys]


def document_text(row: dict) -> str:
    # Chapter/section keywords are shared by hundreds of chunks; embedding them
    # would make every short chunk look alike, so only title and text are embedded.
    return f"{row['title']}\n{row['text']}"


def clause_of(row: dict) -> tuple:
    return row.get("chapter"), row.get("source"), str(row["clause"])


class RuleKnowledge:
    def __init__(self, path: Path, topics_path: Path | None = None, *, semantic: SemanticIndex | None = None,
                 keyword: bool = True):
        contents = Path(path).read_bytes()
        topics_bytes = Path(topics_path).read_bytes() if topics_path is not None else b""
        digest = hashlib.sha256(contents)
        if topics_bytes:
            digest.update(b"\0topics\0" + topics_bytes)
        # One hash approves both what can be sent and how it is selected.
        self.sha256 = digest.hexdigest()
        self.topics_file = Path(topics_path).name if topics_path is not None else None
        rows = [json.loads(line) for line in contents.decode("utf-8").splitlines() if line.strip()]
        if not rows:
            raise ValueError("Rule excerpts file is empty")
        missing = [row.get("id", "?") for row in rows if any(not row.get(key) for key in REQUIRED_FIELDS)]
        if missing:
            raise ValueError(f"Rule excerpts missing required fields: {missing[:5]}")
        self.documents = {row["id"]: row for row in rows}
        if len(self.documents) != len(rows):
            raise ValueError("Rule excerpt IDs must be unique")
        self._order = {ref: index for index, ref in enumerate(self.documents)}
        clauses = sorted({str(row["clause"]) for row in rows}, key=lambda c: (-len(c), c))
        self._citation = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, clauses)) + r")(?:조)?(?![\w.])")
        self._fts = sqlite3.connect(":memory:")
        self._fts.execute("CREATE VIRTUAL TABLE rules USING fts5(id UNINDEXED, title, keywords, body)")
        self._fts.executemany("INSERT INTO rules VALUES (?, ?, ?, ?)",
                              ((row["id"], row["title"], row["keywords"], row["text"]) for row in rows))
        if not keyword and semantic is None:
            raise ValueError("Embedding-only rule search needs a semantic index")
        self.semantic, self.keyword = semantic, keyword
        self._doc_vectors: dict[str, tuple[float, ...]] | None = None
        self.topics = self._load_topics(topics_bytes)

    def describe(self) -> dict:
        semantic = self.semantic
        mode = "keyword" if not semantic else "hybrid" if self.keyword else "embedding"
        return {"mode": mode, "documents": len(self.documents),
                "embedding_model": semantic.model if semantic else None,
                "fusion": f"rrf(k={RRF_K}, top={RANKED_PER_LIST})" if mode == "hybrid" else None}

    def _load_topics(self, data: bytes) -> list[dict]:
        if not data:
            return []
        config = json.loads(data.decode("utf-8"))
        topics = []
        seen_kinds = set()
        for topic in config.get("topics", []):
            topic_id, query = topic.get("topic_id"), topic.get("query", "")
            kinds = topic.get("observation_kinds", [])
            if not topic_id or not query.strip():
                raise ValueError("Each rule topic needs topic_id and query")
            if seen_kinds & set(kinds):
                raise ValueError(f"Observation kind mapped to more than one rule topic: {topic_id}")
            seen_kinds |= set(kinds)
            prefixes = topic.get("id_prefixes") or None
            item = {"topic_id": topic_id, "query": query, "observation_kinds": kinds, "id_prefixes": prefixes,
                    "patterns": [re.compile(p, re.IGNORECASE) for p in topic.get("patterns", [])],
                    "limit": int(topic.get("limit", DEFAULT_LIMIT))}
            # Validated by keywords only, so loading a topics file never calls an API.
            if not self._keyword_rank(query, self._candidates(prefixes)):
                raise ValueError(f"Rule topic {topic_id} matches no excerpt in the loaded rules")
            topics.append(item)
        return topics

    def _candidates(self, id_prefixes) -> list[str]:
        if not id_prefixes:
            return list(self.documents)
        return [ref for ref in self.documents if ref.startswith(tuple(id_prefixes))]

    def _keyword_rank(self, query: str, candidates: list[str]) -> list[str]:
        terms = list(dict.fromkeys(TOKEN.findall(query)))[:32]
        if not terms:
            return []
        # Korean words carry particles ("간격이"), so Hangul terms also match as prefixes.
        expression = " OR ".join(f'"{term}"*' if HANGUL.match(term) else f'"{term}"' for term in terms)
        allowed = set(candidates)
        ranked = self._fts.execute(
            "SELECT id FROM rules WHERE rules MATCH ? ORDER BY bm25(rules, 0, 3, 2, 1), id", (expression,))
        return [ref for (ref,) in ranked if ref in allowed]

    def _semantic_rank(self, query: str, candidates: list[str]) -> list[str]:
        if self._doc_vectors is None:
            refs = list(self.documents)
            vectors = self.semantic.vectors([document_text(self.documents[ref]) for ref in refs])
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("Rule embeddings have inconsistent dimensions")
            self._doc_vectors = dict(zip(refs, vectors))
        (vector,) = self.semantic.vectors([query])
        if len(vector) != len(next(iter(self._doc_vectors.values()))):
            raise ValueError("Query and rule embedding dimensions differ")
        scored = sorted(((math.sumprod(vector, self._doc_vectors[ref]), ref) for ref in candidates),
                        key=lambda item: (-item[0], self._order[item[1]]))
        return [ref for _, ref in scored]

    def search(self, query: str, *, limit: int = DEFAULT_LIMIT, id_prefixes=None,
               semantic_query: str | None = None) -> list[dict]:
        """Rank candidates by keywords, fused with embeddings when available.

        A clause number in the query narrows the candidates to that clause;
        `id_prefixes` narrows them to a chapter/section range.
        """
        candidates = self._candidates(id_prefixes)
        clauses = set(self._citation.findall(query))
        if clauses:
            candidates = [ref for ref in candidates if str(self.documents[ref]["clause"]) in clauses]
            if not candidates:
                return []
        rankings = [self._keyword_rank(query, candidates)] if self.keyword else []
        if self.semantic is not None:
            rankings.append(self._semantic_rank(semantic_query or query, candidates))
        if clauses:
            # Every excerpt of a cited clause qualifies; ranking only orders them.
            rankings.append(sorted(candidates, key=self._order.get))
        scores: dict[str, float] = {}
        for ranking in rankings:
            for rank, ref in enumerate(ranking[:RANKED_PER_LIST]):
                scores[ref] = scores.get(ref, 0.0) + 1.0 / (RRF_K + rank + 1)
        refs, per_clause = [], {}
        for ref in sorted(scores, key=lambda ref: (-scores[ref], self._order[ref])):
            # Keep one clause from filling the context: several clauses beat six slices of one.
            clause = clause_of(self.documents[ref])
            if per_clause.get(clause, 0) < MAX_PER_CLAUSE:
                per_clause[clause] = per_clause.get(clause, 0) + 1
                refs.append(ref)
            if len(refs) == limit:
                break
        # A table or sub-item is read with its parent clause body.
        for ref in list(refs):
            parent = self.documents[ref].get("parent_id")
            if parent is None and (match := TABLE_ID.match(ref)):
                parent = match["base"] + "-BODY"
            if parent in self.documents and parent not in refs:
                refs.insert(refs.index(ref) + 1, parent)
        return [self.documents[ref] for ref in refs]

    def topic_for(self, *, observation_kind: str | None = None, text: str | None = None) -> dict | None:
        if observation_kind is not None:
            found = next((t for t in self.topics if observation_kind in t["observation_kinds"]), None)
            if found:
                return found
        if text:
            return next((t for t in self.topics if any(p.search(text) for p in t["patterns"])), None)
        return None

    def _topic_search(self, topic: dict, text: str) -> list[dict]:
        # Keywords use the curated topic query; embeddings also see the report wording.
        return self.search(topic["query"], limit=topic["limit"], id_prefixes=topic["id_prefixes"],
                           semantic_query=f"{topic['query']}\n{text}" if text else None)

    def for_report(self, text: str, observation_kind: str) -> list[dict]:
        # The agent's classification (the observation kind it looked up) picks
        # the topic, so colloquial wording still gets a reproducible context.
        topic = self.topic_for(observation_kind=observation_kind)
        if topic:
            return self._topic_search(topic, text)
        return self.for_analysis(text)

    def for_analysis(self, text: str) -> list[dict]:
        if not self._citation.search(text):
            topic = self.topic_for(text=text)
            if topic:
                return self._topic_search(topic, text)
        return self.search(text)[:DEFAULT_LIMIT]

    def close(self):
        self._fts.close()


def open_semantic_index(client: EmbeddingClient, model: str, cache_path: Path) -> SemanticIndex:
    return SemanticIndex(client, model, VectorCache(cache_path))
