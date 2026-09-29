"""A tiny FAQ retriever.

Parses `## Question` / answer pairs from a Markdown file and scores them against
the visitor's question with IDF-weighted term overlap. Lines starting with `> `
under a question are alternate phrasings ("move my site" for "migrate"), which
is how real support teams teach a bot the words customers actually use.
Terms that only appear in an answer count for half, so an incidental word like
"take" can't drag in an unrelated answer. It is deliberately
simple and dependency-free; the point is the *gate*: if nothing in the
knowledge base matches well enough, the bot must not guess — it offers a human.
Swap `search()` for a vector or hybrid retriever without touching the rest.
"""
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

_WORD = re.compile(r"[a-z0-9$]+")
STOPWORDS = frozenset(
    """a an and any are as at be but by can could do does for from get got has have
    hello hi how i if in is it its just me my need of on or our please so that the
    their them then there this to up want was we what when where which who why will
    with would you your yours""".split()
)


def tokenize(text: str) -> List[str]:
    tokens = []
    for word in _WORD.findall(text.lower()):
        if word in STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("s"):  # "backups" -> "backup"
            word = word[:-1]
        tokens.append(word)
    return tokens


@dataclass
class Entry:
    question: str
    answer: str
    aliases: List[str] = field(default_factory=list)


@dataclass
class Hit:
    entry: Entry
    score: float


def parse_faq(text: str) -> List[Entry]:
    entries: List[Entry] = []
    current = None
    body: List[str] = []

    def flush() -> None:
        if current is not None and body:
            current.answer = " ".join(body).strip()
            entries.append(current)

    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("## "):
            flush()
            current, body = Entry(line[3:].strip(), ""), []
        elif current is None or not stripped or line.startswith("#"):
            continue
        elif stripped.startswith("> "):
            current.aliases.append(stripped[2:].strip())
        else:
            body.append(stripped)
    flush()
    return entries


ANSWER_ONLY_WEIGHT = 0.5


class KnowledgeBase:
    def __init__(self, entries: List[Entry]) -> None:
        self.entries = entries
        self._titles = [set(tokenize(" ".join([e.question] + e.aliases))) for e in entries]
        self._bodies = [set(tokenize(e.answer)) for e in entries]
        n = max(len(entries), 1)
        df: Dict[str, int] = {}
        for title, body in zip(self._titles, self._bodies):
            for term in title | body:
                df[term] = df.get(term, 0) + 1
        self._idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self._unseen_idf = math.log(1 + n)  # a word the KB has never seen is maximally informative

    @classmethod
    def from_file(cls, path: Path) -> "KnowledgeBase":
        return cls(parse_faq(path.read_text(encoding="utf-8")))

    def search(self, query: str, k: int = 3) -> List[Hit]:
        terms = set(tokenize(query))
        if not terms:
            return []
        # Normalise by the query's own weight so scores sit in 0..1.
        total = sum(self._idf.get(t, self._unseen_idf) for t in terms)
        hits = []
        for entry, title, body in zip(self.entries, self._titles, self._bodies):
            matched = 0.0
            for t in terms:
                if t in title:
                    matched += self._idf[t]
                elif t in body:
                    matched += self._idf[t] * ANSWER_ONLY_WEIGHT
            if matched:
                hits.append(Hit(entry, matched / total))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
