"""Local full-text retrieval over extracted class-rule excerpts.

Which excerpts a report gets is data, not code: an optional topics file maps
the observation kind the agent chose (or wording in free text) to a search
query. Any clause set can be loaded; nothing here names a specific clause.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[가-힣]+")
REQUIRED_FIELDS = ("id", "clause", "title", "keywords", "text")
TABLE_ID = re.compile(r"^(?P<base>.+)-TABLE-[^-]+$")
DEFAULT_LIMIT = 5


class RuleKnowledge:
    def __init__(self, path: Path, topics_path: Path | None = None):
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
        clauses = sorted({str(row["clause"]) for row in rows}, key=lambda c: (-len(c), c))
        self._citation = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, clauses)) + r")(?:조)?(?![\w.])")
        self.topics = self._load_topics(topics_bytes)

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
            item = {"topic_id": topic_id, "query": query, "observation_kinds": kinds,
                    "patterns": [re.compile(p, re.IGNORECASE) for p in topic.get("patterns", [])],
                    "limit": int(topic.get("limit", DEFAULT_LIMIT))}
            if not self.search(query, limit=item["limit"]):
                raise ValueError(f"Rule topic {topic_id} matches no excerpt in the loaded rules")
            topics.append(item)
        return topics

    def search(self, query: str, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Resolve clause numbers exactly, then rank text matches with FTS5."""
        clauses = set(self._citation.findall(query))
        if clauses:
            return [row for row in self.documents.values() if str(row["clause"]) in clauses]
        terms = list(dict.fromkeys(TOKEN.findall(query)))[:32]
        if not terms:
            return []
        expression = " OR ".join(f'"{term}"' for term in terms)
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE rules USING fts5(id UNINDEXED, title, keywords, body)")
            connection.executemany(
                "INSERT INTO rules VALUES (?, ?, ?, ?)",
                ((row["id"], row["title"], row["keywords"], row["text"]) for row in self.documents.values()),
            )
            results = connection.execute(
                "SELECT id FROM rules WHERE rules MATCH ? ORDER BY bm25(rules, 0, 3, 2, 1), id LIMIT ?",
                (expression, limit),
            ).fetchall()
        refs = [ref for (ref,) in results]
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

    def for_report(self, text: str, observation_kind: str) -> list[dict]:
        # The agent's classification (the observation kind it looked up) picks
        # the topic, so colloquial wording still gets a reproducible context.
        topic = self.topic_for(observation_kind=observation_kind)
        if topic:
            return self.search(topic["query"], limit=topic["limit"])
        return self.for_analysis(text)

    def for_analysis(self, text: str) -> list[dict]:
        if not self._citation.search(text):
            topic = self.topic_for(text=text)
            if topic:
                return self.search(topic["query"], limit=topic["limit"])
        return self.search(text)[:DEFAULT_LIMIT]
