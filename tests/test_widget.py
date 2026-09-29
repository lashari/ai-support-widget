import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.guards import RateLimiter, extract_lead
from app.knowledge import KnowledgeBase, parse_faq
from app.main import ROOT, create_app
from app.responder import HANDOFF_TEXT, OfflineResponder, OpenAIResponder
from app.store import Message

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KB = KnowledgeBase.from_file(ROOT / "data" / "faq.md")


@pytest.fixture
def client():
    app = create_app(responder=OfflineResponder(KB, delay=0), agent_token=TOKEN,
                     rate_limiter=RateLimiter(rate_per_min=6000, burst=1000))
    with TestClient(app) as c:
        yield c


def events(resp) -> list:
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def ask(client, cid, text) -> list:
    resp = client.post(f"/api/conversations/{cid}/messages", json={"text": text})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return events(resp)


def reply_text(evs) -> str:
    return "".join(e["text"] for e in evs if e["type"] == "token")


# -- retrieval ---------------------------------------------------------------
def test_faq_parses_every_entry():
    assert len(KB.entries) == 9
    assert all(e.question.endswith("?") and e.answer for e in KB.entries)


@pytest.mark.parametrize("question,expected", [
    ("how much does it cost per month?", "How much does hosting cost?"),
    ("I forgot my password", "How do I reset my password?"),
    ("do you give refunds", "Do you offer a free trial or refund?"),
    ("can you move my website from my old host", "Can you migrate my existing website?"),
    ("Can you move my site from another host?", "Can you migrate my existing website?"),  # via "> " alias
    ("are you open on weekends", "What are your support hours?"),
    ("do you have backups", "How do backups work?"),
])
def test_retrieval_finds_the_right_answer(question, expected):
    assert KB.search(question)[0].entry.question == expected


@pytest.mark.parametrize("question", ["Do you sell laptops?", "what is the meaning of life", "hello"])
def test_off_topic_questions_do_not_match(question):
    assert all(h.score < 0.35 for h in KB.search(question))


def test_answer_only_words_cannot_carry_a_match():
    # "take" appears in the DNS answer ("can take up to 24 hours"); it must not pull that answer
    # in for a payments question.
    kb = KnowledgeBase(parse_faq("## How do I point my domain?\nDNS changes can take 24 hours.\n"))
    assert all(h.score < 0.35 for h in kb.search("what cards do you take"))
    assert KB.search("what cards do you take")[0].entry.question == "Which payment methods do you accept?"


def test_aliases_are_parsed():
    entries = parse_faq("## Q?\n> other way to ask\n> another\nThe answer.\n")
    assert entries[0].aliases == ["other way to ask", "another"] and entries[0].answer == "The answer."


def test_parse_ignores_comments_and_empty_sections():
    entries = parse_faq("# title\n## Q1?\nA1.\n## Empty?\n\n## Q2?\nA2 line one.\nline two.\n")
    assert [(e.question, e.answer) for e in entries] == [("Q1?", "A1."), ("Q2?", "A2 line one. line two.")]


# -- visitor flow ------------------------------------------------------------
def test_grounded_answer_streams_token_by_token(client):
    cid = client.post("/api/conversations").json()["id"]
    evs = ask(client, cid, "How much does hosting cost?")
    tokens = [e for e in evs if e["type"] == "token"]
    assert len(tokens) > 5, "answer should arrive as a stream, not one blob"
    assert "$9/month" in reply_text(evs)
    assert evs[-1] == {"type": "done", "needs_human": False, "has_lead": False}


def test_unknown_question_hands_off_instead_of_guessing(client):
    cid = client.post("/api/conversations").json()["id"]
    evs = ask(client, cid, "Do you sell gaming laptops?")
    assert reply_text(evs) == HANDOFF_TEXT
    assert evs[-1]["needs_human"] is True
    waiting = client.get("/api/agent/conversations", headers=AUTH).json()["waiting"]
    assert [c["id"] for c in waiting] == [cid]


def test_email_typed_after_handoff_is_captured_as_lead(client):
    cid = client.post("/api/conversations").json()["id"]
    ask(client, cid, "Do you sell gaming laptops?")
    evs = ask(client, cid, "sure, it's Jane.Doe@Example.com")
    assert "jane.doe@example.com" in reply_text(evs)
    assert evs[-1]["has_lead"] is True
    leads = client.get("/api/agent/leads", headers=AUTH).json()
    assert leads[0]["email"] == "jane.doe@example.com"
    assert leads[0]["conversation_id"] == cid


def test_lead_form_requires_contact_details(client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/lead", json={"name": "Jane"}).status_code == 422
    ok = client.post(f"/api/conversations/{cid}/lead", json={"name": "Jane", "email": "j@x.io"})
    assert ok.status_code == 200 and ok.json()["lead"]["email"] == "j@x.io"


def test_unknown_conversation_is_404(client):
    assert client.post("/api/conversations/nope/messages", json={"text": "hi"}).status_code == 404


def test_message_length_is_capped(client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": "x" * 1001}).status_code == 422


# -- human takeover ----------------------------------------------------------
def test_agent_endpoints_require_token(client):
    assert client.get("/api/agent/conversations").status_code == 401
    assert client.get("/api/agent/conversations", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_takeover_silences_the_bot_and_agent_reply_reaches_visitor(client):
    app_store = client.app.state.store
    cid = client.post("/api/conversations").json()["id"]
    conv = app_store.get(cid)

    async def scenario():
        feed = app_store.subscribe(conv)  # what the visitor's EventSource receives
        client.post(f"/api/agent/conversations/{cid}/takeover", headers=AUTH)
        evs = ask(client, cid, "How much does hosting cost?")
        assert [e["type"] for e in evs] == ["queued"], "bot must not answer while a human owns the chat"
        client.post(f"/api/agent/conversations/{cid}/reply", headers=AUTH, json={"text": "Hi, Sam here!"})
        received = []
        while not feed.empty():
            received.append(feed.get_nowait())
        return received

    received = asyncio.run(scenario())
    assert {"type": "mode", "mode": "human"} in received
    agent_msgs = [e["message"]["text"] for e in received if e["type"] == "message" and e["message"]["role"] == "agent"]
    assert agent_msgs == ["Hi, Sam here!"]

    client.post(f"/api/agent/conversations/{cid}/release", headers=AUTH)
    assert "$9/month" in reply_text(ask(client, cid, "How much does hosting cost?"))


def test_rate_limit_returns_429():
    app = create_app(responder=OfflineResponder(KB, delay=0), agent_token=TOKEN,
                     rate_limiter=RateLimiter(rate_per_min=1, burst=2))
    with TestClient(app) as c:
        codes = [c.post("/api/conversations").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# -- guards ------------------------------------------------------------------
def test_extract_lead():
    assert extract_lead("mail me at A.B@test.co please") == {"email": "a.b@test.co"}
    assert extract_lead("call +1 (555) 010-2233") == {"phone": "+15550102233"}
    assert extract_lead("no contact here, order 42") is None


def test_rate_limiter_refills():
    rl = RateLimiter(rate_per_min=60, burst=1)
    assert rl.allow("ip", now=0.0) is True
    assert rl.allow("ip", now=0.5) is False
    assert rl.allow("ip", now=1.6) is True


# -- OpenAI path (no network: mocked transport) -----------------------------
def _fake_openai(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["stream"] is True and body["model"] == "gpt-4o-mini"
    assert "How much does hosting cost?" in body["messages"][0]["content"], "FAQ context must be in the prompt"
    assert request.headers["authorization"] == "Bearer sk-test"
    chunks = ["Plans ", "start ", "at ", "$9."]
    sse_body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks)
    return httpx.Response(200, text=sse_body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


def test_openai_responder_streams_and_grounds():
    responder = OpenAIResponder(KB, "sk-test", transport=httpx.MockTransport(_fake_openai))

    async def run():
        reply = await responder.reply("how much does hosting cost", [Message("visitor", "how much does hosting cost")])
        return reply.grounded, [c async for c in reply.chunks]

    grounded, chunks = asyncio.run(run())
    assert grounded is True
    assert chunks == ["Plans ", "start ", "at ", "$9."]


def test_openai_responder_skips_the_model_when_nothing_matches():
    def must_not_call(request):
        raise AssertionError("model should not be called without grounding")

    responder = OpenAIResponder(KB, "sk-test", transport=httpx.MockTransport(must_not_call))

    async def run():
        reply = await responder.reply("do you sell gaming laptops", [])
        return reply.grounded, "".join([c async for c in reply.chunks])

    assert asyncio.run(run()) == (False, HANDOFF_TEXT)


def test_model_failure_mid_stream_degrades_to_human():
    def broken(request):
        return httpx.Response(500, text="upstream down")

    app = create_app(responder=OpenAIResponder(KB, "sk-test", transport=httpx.MockTransport(broken)),
                     agent_token=TOKEN, rate_limiter=RateLimiter(rate_per_min=6000, burst=1000))
    with TestClient(app) as c:
        cid = c.post("/api/conversations").json()["id"]
        evs = ask(c, cid, "How much does hosting cost?")
    assert evs[0]["type"] == "error"
    assert evs[-1] == {"type": "done", "needs_human": True, "has_lead": False}


def test_pages_are_served(client):
    assert "widget.js" in client.get("/").text
    assert "Agent console" in client.get("/agent").text
    assert client.get("/static/widget.js").status_code == 200
    assert client.get("/health").json()["faq_entries"] == 9


def test_transcript_restores_the_chat(client):
    cid = client.post("/api/conversations").json()["id"]
    ask(client, cid, "How do I reset my password?")
    data = client.get(f"/api/conversations/{cid}/messages").json()
    assert [m["role"] for m in data["messages"]] == ["ai", "visitor", "ai"]
    assert "Forgot password" in data["messages"][-1]["text"]
    assert data["mode"] == "ai" and data["needs_human"] is False
    assert client.get("/api/conversations/unknown/messages").status_code == 404
