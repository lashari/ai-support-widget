"""FastAPI backend for the support widget.

Visitor API (called by static/widget.js)
  POST /api/conversations                      start a chat
  POST /api/conversations/{id}/messages        send a message; reply streams back as SSE
  GET  /api/conversations/{id}/messages        transcript (restores the chat after a reload)
  GET  /api/conversations/{id}/events          SSE feed of agent replies / mode changes
  POST /api/conversations/{id}/lead            explicit lead form

Agent API (static/agent.html, needs `Authorization: Bearer $AGENT_TOKEN`)
  GET  /api/agent/conversations                waiting / human / ai buckets
  GET  /api/agent/conversations/{id}           full transcript
  POST /api/agent/conversations/{id}/takeover  pause the bot, a person answers
  POST /api/agent/conversations/{id}/release   hand the chat back to the bot
  POST /api/agent/conversations/{id}/reply     send a message as the agent
  GET  /api/agent/leads                        captured leads
"""
import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .guards import RateLimiter, extract_lead
from .knowledge import KnowledgeBase
from .responder import OfflineResponder, make_responder
from .store import MODE_AI, MODE_HUMAN, Conversation, Store, conversations_for_agent

ROOT = Path(__file__).resolve().parent.parent
MAX_MESSAGE_CHARS = 1000
log = logging.getLogger("support-widget")


class TextIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)


class LeadIn(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    email: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def create_app(
    kb_path: Path = ROOT / "data" / "faq.md",
    responder: Optional[OfflineResponder] = None,
    agent_token: Optional[str] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> FastAPI:
    kb = KnowledgeBase.from_file(kb_path)
    responder = responder or make_responder(kb)
    token = agent_token or os.getenv("AGENT_TOKEN", "change-me")
    if token == "change-me":
        log.warning("AGENT_TOKEN is the default 'change-me' - set it before exposing this server.")
    limiter = rate_limiter or RateLimiter()
    store = Store()

    app = FastAPI(title="AI Support Widget", version="1.0.0")
    app.state.store = store
    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def conversation(conv_id: str) -> Conversation:
        conv = store.get(conv_id)
        if conv is None:
            raise HTTPException(404, "conversation not found")
        return conv

    def require_agent(authorization: str = Header("")) -> None:
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "agent token required")

    def throttle(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, "slow down a little")

    # -- visitor ---------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "responder": responder.name, "faq_entries": len(kb.entries)}

    @app.post("/api/conversations")
    def start(_: None = Depends(throttle)) -> dict:
        conv = store.create()
        greeting = "Hi! I'm the virtual assistant. Ask me anything about our plans, billing or setup."
        store.add_message(conv, "ai", greeting)
        return {"id": conv.id, "greeting": greeting}

    @app.post("/api/conversations/{conv_id}/messages")
    async def send(conv_id: str, body: TextIn, _: None = Depends(throttle)) -> StreamingResponse:
        conv = conversation(conv_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "empty message")
        history = list(conv.messages)
        store.add_message(conv, "visitor", text)
        lead = extract_lead(text)
        if lead:
            store.save_lead(conv, lead)
        return StreamingResponse(
            _answer(conv, text, history, lead),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _answer(conv: Conversation, text: str, history: list, lead: Optional[dict]) -> AsyncIterator[str]:
        # A person owns this chat: the bot stays quiet.
        if conv.mode == MODE_HUMAN:
            yield sse({"type": "queued", "text": "Sent to the agent."})
            return
        # The bot already offered a handoff and the visitor answered with contact details.
        if conv.needs_human and lead:
            reply = f"Thanks! A member of our team will contact you at {lead.get('email') or lead.get('phone')}."
            store.add_message(conv, "ai", reply)
            yield sse({"type": "token", "text": reply})
            yield sse({"type": "done", "needs_human": True, "has_lead": True})
            return

        result = await responder.reply(text, history + [conv.messages[-1]])
        parts = []
        try:
            try:
                async for chunk in result.chunks:
                    parts.append(chunk)
                    yield sse({"type": "token", "text": chunk})
            except Exception:  # model/API failure mid-stream: degrade to a human, never to silence
                log.exception("responder failed")
                fallback = "Sorry, I hit a problem answering that. I've flagged this chat for a person."
                parts = [fallback]
                result.grounded = False
                yield sse({"type": "error", "text": fallback})
        finally:
            # Runs even if the visitor closes the tab mid-answer, so the transcript stays complete.
            if not result.grounded:
                conv.needs_human = True
                store.publish(conv, {"type": "needs_human"})
            store.add_message(conv, "ai", "".join(parts))
        yield sse({"type": "done", "needs_human": conv.needs_human, "has_lead": conv.lead is not None})

    @app.get("/api/conversations/{conv_id}/events")
    async def events(conv_id: str, request: Request) -> StreamingResponse:
        conv = conversation(conv_id)
        queue = store.subscribe(conv)

        async def feed() -> AsyncIterator[str]:
            try:
                yield sse({"type": "mode", "mode": conv.mode})
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # stops proxies from closing idle streams
                        continue
                    yield sse(event)
            finally:
                store.unsubscribe(conv, queue)

        return StreamingResponse(feed(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/conversations/{conv_id}/messages")
    def transcript(conv_id: str) -> dict:
        """Lets the widget restore the chat after a page reload. The random id is the capability."""
        conv = conversation(conv_id)
        return {
            "mode": conv.mode,
            "needs_human": conv.needs_human,
            "has_lead": conv.lead is not None,
            "messages": [m.to_dict() for m in conv.messages],
        }

    @app.post("/api/conversations/{conv_id}/lead")
    def lead(conv_id: str, body: LeadIn, _: None = Depends(throttle)) -> dict:
        conv = conversation(conv_id)
        data = {k: v.strip() for k, v in body.model_dump().items() if v and v.strip()}
        if not (data.get("email") or data.get("phone")):
            raise HTTPException(422, "email or phone required")
        conv.needs_human = True
        return {"ok": True, "lead": store.save_lead(conv, data)}

    # -- agent -----------------------------------------------------------
    @app.get("/api/agent/conversations", dependencies=[Depends(require_agent)])
    def agent_list() -> dict:
        return conversations_for_agent(store)

    @app.get("/api/agent/conversations/{conv_id}", dependencies=[Depends(require_agent)])
    def agent_detail(conv_id: str) -> dict:
        return conversation(conv_id).detail()

    @app.post("/api/agent/conversations/{conv_id}/takeover", dependencies=[Depends(require_agent)])
    def takeover(conv_id: str) -> dict:
        conv = conversation(conv_id)
        store.set_mode(conv, MODE_HUMAN)
        store.add_message(conv, "system", "A support agent has joined the chat.")
        return conv.summary()

    @app.post("/api/agent/conversations/{conv_id}/release", dependencies=[Depends(require_agent)])
    def release(conv_id: str) -> dict:
        conv = conversation(conv_id)
        store.set_mode(conv, MODE_AI)
        store.add_message(conv, "system", "The virtual assistant is back. Ask me anything.")
        return conv.summary()

    @app.post("/api/agent/conversations/{conv_id}/reply", dependencies=[Depends(require_agent)])
    def agent_reply(conv_id: str, body: TextIn) -> dict:
        conv = conversation(conv_id)
        if conv.mode != MODE_HUMAN:
            store.set_mode(conv, MODE_HUMAN)  # replying implies taking over
        return store.add_message(conv, "agent", body.text.strip()).to_dict()

    @app.get("/api/agent/leads", dependencies=[Depends(require_agent)])
    def leads() -> list:
        return list(reversed(store.leads))

    # -- pages -----------------------------------------------------------
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def demo() -> FileResponse:
        return FileResponse(ROOT / "static" / "demo.html")

    @app.get("/agent", include_in_schema=False)
    def agent_console() -> FileResponse:
        return FileResponse(ROOT / "static" / "agent.html")

    return app


app = create_app()
