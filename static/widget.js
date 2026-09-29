/*!
 * AI Support Widget — drop-in chat bubble.
 *   <script src="https://YOUR-SERVER/static/widget.js"
 *           data-title="Acme Support" data-color="#0A66C2" defer></script>
 * No framework, no build step. Renders inside a Shadow DOM so the host page's
 * CSS can't break it (and it can't break the host page).
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var API = (script && script.getAttribute("data-api")) || (script ? new URL(script.src).origin : "");
  var TITLE = (script && script.getAttribute("data-title")) || "Support";
  var COLOR = (script && script.getAttribute("data-color")) || "#0A66C2";
  var STORAGE_KEY = "aisw:conversation";

  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, val) { try { window.localStorage.setItem(key, val); } catch (e) { /* private mode */ } }
  function forget(key) { try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ } }

  var host = document.createElement("div");
  host.id = "ai-support-widget";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  root.innerHTML =
    "<style>" +
    ":host{all:initial;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;--c:" + COLOR + "}" +
    ".bubble{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:var(--c);color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);font-size:24px;z-index:2147483646}" +
    ".panel{position:fixed;right:20px;bottom:88px;width:360px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 110px);background:#fff;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;z-index:2147483647;color:#1b1f24}" +
    ".panel.open{display:flex}" +
    ".head{background:var(--c);color:#fff;padding:14px 16px;display:flex;align-items:center;gap:8px}" +
    ".head b{flex:1;font-size:15px}.head small{opacity:.85;font-size:12px}" +
    ".head button{background:transparent;border:1px solid rgba(255,255,255,.6);color:#fff;border-radius:6px;font-size:12px;padding:4px 8px;cursor:pointer}" +
    ".log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:#f6f8fa}" +
    ".m{max-width:82%;padding:9px 12px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}" +
    ".visitor{align-self:flex-end;background:var(--c);color:#fff;border-bottom-right-radius:4px}" +
    ".ai,.agent{align-self:flex-start;background:#fff;border:1px solid #d8dee4;border-bottom-left-radius:4px}" +
    ".agent{border-color:var(--c)}.agent:before{content:'Agent';display:block;font-size:11px;font-weight:600;color:var(--c);margin-bottom:2px}" +
    ".system{align-self:center;font-size:12px;color:#57606a;background:none}" +
    "form.send{display:flex;border-top:1px solid #d8dee4}" +
    "form.send input{flex:1;border:0;padding:14px;font-size:14px;outline:none}" +
    "form.send button{border:0;background:none;color:var(--c);font-weight:600;padding:0 16px;cursor:pointer}" +
    "form.lead{display:none;flex-direction:column;gap:6px;padding:10px 14px;border-top:1px solid #d8dee4;background:#fff}" +
    "form.lead.open{display:flex}" +
    "form.lead input{border:1px solid #d8dee4;border-radius:6px;padding:8px;font-size:13px}" +
    "form.lead button{border:0;border-radius:6px;background:var(--c);color:#fff;padding:8px;cursor:pointer}" +
    "</style>" +
    "<button class='bubble' aria-label='Open chat'>💬</button>" +
    "<section class='panel' role='dialog' aria-label='" + TITLE + "'>" +
    "  <div class='head'><b></b><small class='status'>AI assistant</small><button class='human' type='button'>Talk to a person</button></div>" +
    "  <div class='log' aria-live='polite'></div>" +
    "  <form class='lead'><input name='name' placeholder='Your name' maxlength='120'>" +
    "    <input name='email' type='email' placeholder='Email' required maxlength='200'>" +
    "    <button type='submit'>Ask a person to contact me</button></form>" +
    "  <form class='send'><input name='q' placeholder='Type your question…' maxlength='1000' autocomplete='off'><button type='submit'>Send</button></form>" +
    "</section>";

  var $ = function (sel) { return root.querySelector(sel); };
  $(".head b").textContent = TITLE;
  var log = $(".log"), status = $(".status"), sendForm = $("form.send"), leadForm = $("form.lead");
  var conversationId = load(STORAGE_KEY), events = null, busy = false;

  function add(role, text) {
    var el = document.createElement("div");
    el.className = "m " + role;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function setMode(mode) {
    var human = mode === "human";
    status.textContent = human ? "Chatting with an agent" : "AI assistant";
    $(".human").hidden = human;
    if (human) leadForm.classList.remove("open");  // a person is already here
  }

  function listen() {
    if (events || !window.EventSource) return;
    events = new EventSource(API + "/api/conversations/" + conversationId + "/events");
    events.onmessage = function (e) {
      var ev = JSON.parse(e.data);
      if (ev.type === "mode") setMode(ev.mode);
      // needs_human is handled by the streamed "done" event, which also knows about leads.
      // Visitor + AI messages are already on screen from the streamed reply.
      if (ev.type === "message" && (ev.message.role === "agent" || ev.message.role === "system")) {
        add(ev.message.role, ev.message.text);
      }
    };
  }

  var started = null;
  function start() {
    if (started) return started;
    started = (conversationId ? restore() : Promise.resolve(false)).then(function (ok) {
      return ok ? listen() : create();
    }).catch(function (err) { started = null; add("system", "Could not reach support right now."); throw err; });
    return started;
  }

  // After a page reload, show the existing chat instead of an empty window.
  function restore() {
    return fetch(API + "/api/conversations/" + conversationId + "/messages").then(function (r) {
      if (!r.ok) { forget(STORAGE_KEY); conversationId = null; return false; }
      return r.json().then(function (d) {
        d.messages.forEach(function (m) { add(m.role, m.text); });
        setMode(d.mode);
        if (d.needs_human && !d.has_lead && d.mode !== "human") leadForm.classList.add("open");
        return true;
      });
    });
  }

  function create() {
    return fetch(API + "/api/conversations", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        conversationId = d.id;
        save(STORAGE_KEY, d.id);
        add("ai", d.greeting);
        listen();
      });
  }

  // POST + read the SSE body with a stream reader (EventSource can't POST).
  function ask(text, isRetry) {
    busy = true;
    if (!isRetry) add("visitor", text);
    var bubble = null;
    return fetch(API + "/api/conversations/" + conversationId + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (res) {
      if (res.status === 404) {
        forget(STORAGE_KEY); conversationId = null; started = null;
        if (events) { events.close(); events = null; }
        throw new Error("expired");
      }
      if (res.status === 429) throw new Error("Too many messages — please wait a moment.");
      if (!res.ok || !res.body) throw new Error("Could not reach support right now.");
      var reader = res.body.getReader(), decoder = new TextDecoder(), buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, { stream: true });
          var frames = buffer.split("\n\n");
          buffer = frames.pop();
          frames.forEach(function (frame) {
            if (frame.indexOf("data:") !== 0) return;
            var ev = JSON.parse(frame.slice(5));
            if (ev.type === "token" || ev.type === "error") {
              bubble = bubble || add("ai", "");
              bubble.textContent += ev.text;
              log.scrollTop = log.scrollHeight;
            } else if (ev.type === "queued") {
              add("system", ev.text);
            } else if (ev.type === "done") {
              leadForm.classList.toggle("open", ev.needs_human && !ev.has_lead);
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err.message === "expired" && !isRetry) {  // server restarted: start a fresh chat once
        return start().then(function () { return ask(text, true); });
      }
      add("system", err.message);
    }).then(function () { busy = false; });
  }

  $(".bubble").addEventListener("click", function () {
    var panel = $(".panel");
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      start().then(function () { sendForm.elements.q.focus(); });
    }
  });

  $(".human").addEventListener("click", function () { start().then(function () { leadForm.classList.add("open"); }); });

  sendForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = sendForm.elements.q.value.trim();
    if (!text || busy) return;
    sendForm.elements.q.value = "";
    start().then(function () { return ask(text); });
  });

  leadForm.addEventListener("submit", function (e) {
    e.preventDefault();
    fetch(API + "/api/conversations/" + conversationId + "/lead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: leadForm.elements["name"].value, email: leadForm.elements["email"].value })
    }).then(function (r) {
      if (!r.ok) throw new Error();
      leadForm.classList.remove("open");
      add("system", "Thanks! A person from our team will get back to you by email.");
    }).catch(function () { add("system", "Please enter a valid email."); });
  });
})();
