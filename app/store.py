"""In-memory conversation store with a tiny pub/sub for live updates.

Everything lives in one process, which keeps the demo dependency-free. The
interface is small on purpose: swap it for Redis/Postgres when you need more
than one worker (see README, "Going to production").
"""
import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

MODE_AI = "ai"
MODE_HUMAN = "human"


@dataclass
class Message:
    role: str  # "visitor" | "ai" | "agent" | "system"
    text: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "ts": self.ts}


@dataclass
class Conversation:
    id: str
    created: float = field(default_factory=time.time)
    mode: str = MODE_AI
    needs_human: bool = False
    messages: List[Message] = field(default_factory=list)
    lead: Optional[dict] = None
    subscribers: Set["asyncio.Queue[dict]"] = field(default_factory=set)

    def summary(self) -> dict:
        last = self.messages[-1] if self.messages else None
        return {
            "id": self.id,
            "created": self.created,
            "mode": self.mode,
            "needs_human": self.needs_human,
            "message_count": len(self.messages),
            "last_message": last.to_dict() if last else None,
            "lead": self.lead,
        }

    def detail(self) -> dict:
        data = self.summary()
        data["messages"] = [m.to_dict() for m in self.messages]
        return data


class Store:
    """Bounded conversation store. Oldest conversations are evicted first."""

    def __init__(self, max_conversations: int = 1000) -> None:
        self.max_conversations = max_conversations
        self._convs: "OrderedDict[str, Conversation]" = OrderedDict()
        self.leads: List[dict] = []

    def create(self) -> Conversation:
        conv = Conversation(id=uuid.uuid4().hex)
        self._convs[conv.id] = conv
        while len(self._convs) > self.max_conversations:
            self._convs.popitem(last=False)
        return conv

    def get(self, conv_id: str) -> Optional[Conversation]:
        return self._convs.get(conv_id)

    def all(self) -> List[Conversation]:
        return list(reversed(self._convs.values()))

    def add_message(self, conv: Conversation, role: str, text: str) -> Message:
        msg = Message(role=role, text=text)
        conv.messages.append(msg)
        self.publish(conv, {"type": "message", "message": msg.to_dict()})
        return msg

    def set_mode(self, conv: Conversation, mode: str) -> None:
        conv.mode = mode
        if mode == MODE_HUMAN:
            conv.needs_human = False
        self.publish(conv, {"type": "mode", "mode": mode})

    def save_lead(self, conv: Conversation, lead: dict) -> dict:
        record = dict(lead, conversation_id=conv.id, ts=time.time())
        conv.lead = {**(conv.lead or {}), **lead}
        self.leads.append(record)
        return record

    # -- pub/sub ---------------------------------------------------------
    def subscribe(self, conv: Conversation) -> "asyncio.Queue[dict]":
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=100)
        conv.subscribers.add(queue)
        return queue

    def unsubscribe(self, conv: Conversation, queue: "asyncio.Queue[dict]") -> None:
        conv.subscribers.discard(queue)

    def publish(self, conv: Conversation, event: dict) -> None:
        for queue in list(conv.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stuck client must not block everyone else; drop it.
                conv.subscribers.discard(queue)


def conversations_for_agent(store: Store) -> Dict[str, list]:
    """Group conversations so the agent console can show 'waiting' first."""
    waiting, live, other = [], [], []
    for conv in store.all():
        if conv.needs_human:
            waiting.append(conv.summary())
        elif conv.mode == MODE_HUMAN:
            live.append(conv.summary())
        else:
            other.append(conv.summary())
    return {"waiting": waiting, "human": live, "ai": other}
