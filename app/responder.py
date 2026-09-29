"""Turns a visitor question into a streamed reply.

Two implementations share one contract:

* ``OfflineResponder`` – extractive: streams the best-matching FAQ answer.
  Needs no API key, so reviewers can run the project in seconds.
* ``OpenAIResponder`` – sends the top FAQ matches as context to a chat model
  and streams its tokens back. Enabled automatically when OPENAI_API_KEY is set.

Both apply the same grounding gate *before* generating anything: if retrieval
finds nothing relevant, the bot says so and offers a human instead of letting a
model improvise an answer.
"""
import asyncio
import json
import os
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional

import httpx

from .knowledge import Hit, KnowledgeBase
from .store import Message

MIN_SCORE = float(os.getenv("MIN_SCORE", "0.35"))

HANDOFF_TEXT = (
    "I'm not sure about that one, and I'd rather not guess. "
    "Would you like me to connect you with a person? "
    "Just leave your email and we'll get back to you."
)

SYSTEM_PROMPT = (
    "You are a friendly customer-support assistant. Answer ONLY from the context "
    "below. If the context does not contain the answer, say you are not sure and "
    "offer to connect the visitor with a person. Keep answers under 80 words."
)


@dataclass
class Reply:
    grounded: bool  # False -> no knowledge matched; conversation should go to a human
    chunks: AsyncIterator[str]


async def _stream_words(text: str, delay: float) -> AsyncIterator[str]:
    words = text.split(" ")
    for i, word in enumerate(words):
        yield word if i == 0 else " " + word
        if delay:
            await asyncio.sleep(delay)


class OfflineResponder:
    name = "offline"

    def __init__(self, kb: KnowledgeBase, delay: float = 0.03) -> None:
        self.kb = kb
        self.delay = delay

    def retrieve(self, question: str) -> List[Hit]:
        return [h for h in self.kb.search(question) if h.score >= MIN_SCORE]

    async def reply(self, question: str, history: List[Message]) -> Reply:
        hits = self.retrieve(question)
        if not hits:
            return Reply(False, _stream_words(HANDOFF_TEXT, self.delay))
        return Reply(True, _stream_words(hits[0].entry.answer, self.delay))


class OpenAIResponder(OfflineResponder):
    name = "openai"

    def __init__(
        self,
        kb: KnowledgeBase,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        super().__init__(kb, delay=0)
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.transport = transport  # injectable for tests

    def build_messages(self, question: str, history: List[Message], hits: List[Hit]) -> list:
        context = "\n\n".join(f"Q: {h.entry.question}\nA: {h.entry.answer}" for h in hits)
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nContext:\n{context}"}]
        # Last few turns give the model conversational memory without growing the prompt forever.
        for msg in history[-6:]:
            if msg.role == "visitor":
                messages.append({"role": "user", "content": msg.text})
            elif msg.role in ("ai", "agent"):
                messages.append({"role": "assistant", "content": msg.text})
        if not messages or messages[-1].get("content") != question:
            messages.append({"role": "user", "content": question})
        return messages

    async def reply(self, question: str, history: List[Message]) -> Reply:
        hits = self.retrieve(question)
        if not hits:
            return Reply(False, _stream_words(HANDOFF_TEXT, 0))
        return Reply(True, self._generate(self.build_messages(question, history, hits)))

    async def _generate(self, messages: list) -> AsyncIterator[str]:
        payload = {"model": self.model, "messages": messages, "stream": True, "temperature": 0.2}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    choices = json.loads(data).get("choices") or [{}]
                    token = choices[0].get("delta", {}).get("content")
                    if token:
                        yield token


def make_responder(kb: KnowledgeBase) -> OfflineResponder:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return OpenAIResponder(kb, key, model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    return OfflineResponder(kb)
