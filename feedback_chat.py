"""
feedback_chat.py -- conversational Feedback intake (2026-08-02).

Jay's ask: the old one-shot Feedback page (a plain textarea landing
verbatim in checkreq.app_feedback) gives him nothing actionable -- a vague
comment is just a vague comment. He wants staff to have a real,
multi-turn conversation with Claude about their feedback/idea (Claude
asking clarifying questions the way a real product conversation works),
and at the END of that conversation, a structured, digested summary
(problem statement / why it matters / suggested next step) is what
actually lands in the feedback log for him to review -- not the raw
transcript. He can still open the full transcript for context from the
admin side, but the log itself shows the distilled version.

HARD BOUNDARY (Jay's own idea, already agreed): this chat can NEVER
trigger a build, a deploy, or any code/database write beyond its own
feedback data. It's a pure conversational INTAKE tool -- same shape as
26-132 Teams Automation Bot's "no write path anywhere in the code"
design, chosen for the identical reason. There is no tool-use here at
all (no `tools` param on the Claude API calls, full stop) -- structurally,
not just by prompt convention, the model has nothing it could call to
take an action even if it wanted to. The two writes this module performs
(feedback_messages rows, and the one checkreq.app_feedback row created on
close) are both plain, hardcoded INSERTs this Python code executes
itself -- Claude's own output is only ever the *content* of a message or
a summary string, never a decision this code blindly acts on.

Model choice: 26-132 Teams Automation Bot uses claude-haiku-4-5, chosen
specifically for a hard 5-second synchronous Teams-reply deadline (see
that project's Plan.md). No such deadline exists here -- a normal web
request has ample time -- and the actual point of this feature is asking
genuinely good clarifying questions and writing a well-organized summary,
which is a quality bar haiku-tier models aren't tuned for. Uses
claude-sonnet-5 instead: strong instruction-following and writing quality
at a fraction of Opus-tier cost, appropriate for a slower-paced,
higher-quality conversational tool rather than a bulk/automated job.

Secret source matches document_extract.py's/auth_azure.py's _read_secret()
pattern exactly, same project (cfm-qbo-mcp), same secret 26-132 Teams Bot
and this app's own document_extract.py already read live
(anthropic-api-key) -- confirmed via deploy.yml's own comment that
qbo-mcp-sa's project-level secretAccessor grant already covers it; no new
IAM grant needed.

Cost/abuse guard (Jay's own ask, "don't over-engineer"): MAX_USER_TURNS
below caps a single conversation at a reasonable number of user messages
before showing a friendly "let's wrap this one up" message instead of
calling the API again -- a runaway loop or repeated accidental submission
can't rack up an unbounded bill. Not a heavy rate-limit system; this is a
low-traffic internal tool.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db

router = APIRouter()

# main.py owns the identity/entity/render helpers; register() below injects
# them so this module never imports main (which will import this one) --
# same register()-injection pattern as admin_setup.py/access_requests.py.
_current_user = None
_current_org = None
_render = None

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1024
SUMMARY_MAX_TOKENS = 1024

# A "turn" here = one user-sent message. 40 was the low end of Jay's own
# "something like 40-50" suggestion -- generous for a real reflective
# conversation, not so high a runaway loop could rack up real API spend.
MAX_USER_TURNS = 40

_WRAP_UP_MESSAGE = (
    "We've covered a lot of ground in this conversation -- let's wrap this "
    "one up. Hit “Submit my feedback” below to send what we've got, "
    "and start a fresh conversation if there's more to say."
)

_SECRET_PROJECT = os.environ.get("FIRESTORE_PROJECT", "cfm-qbo-mcp")
_SECRET_NAME = "anthropic-api-key"
_cached_key: str | None = None

# ── System prompts ───────────────────────────────────────────────────────────
# Deliberately no `tools` param anywhere in this file -- see the module
# docstring's hard-boundary note. The prompt also states the boundary in
# plain language so a refusal/clarification is clean rather than the model
# ever implying it can act.

INTERVIEW_SYSTEM_PROMPT = """You are a warm, curious product-feedback interviewer for "Beacon," \
Cornerstone Franciscan Ministries / EDOM's internal check-request and payment web app. A staff \
member has come here with feedback or an idea about Beacon. Your only job in this conversation is \
to help them go from a vague comment into something specific and useful -- you do this by asking \
good, genuine follow-up questions, one at a time, not by running through a checklist.

Things worth drawing out over the course of the conversation (not necessarily in this order, and \
not as a rigid script):
- What isn't working, or what the idea actually is
- Why it matters to them -- how it affects their day-to-day work
- What they'd want instead, if they could have it
- How urgent or important this feels to them

Hard rules:
- You cannot build, code, deploy, or change anything in this conversation, and you have no tool or \
code access here at all. If asked whether you can fix or build something right now, say plainly \
that you can't -- this conversation only collects and organizes the feedback for someone else to \
act on.
- Once, naturally, somewhere in your first or second reply (not as a canned disclaimer bolted onto \
the start), mention that this conversation becomes a summary for the CFO/admin team to review -- \
it's not an automatic ticket, a person decides what happens with it.
- Keep your replies short: a sentence or two of acknowledgment, then your next question, is usually \
enough. This is a conversation, not a form with extra steps.
- If someone's very first message is already specific and complete, don't manufacture extra \
questions just to fill a quota -- acknowledge it, ask if there's anything else worth adding, and let \
them know they can submit whenever they're ready.
- Never claim credit for having built or fixed anything -- you're the interviewer, not the builder."""

SUMMARY_SYSTEM_PROMPT = """You are summarizing a feedback conversation between a Beacon (the \
internal check-request app) staff member and an intake interviewer, for the CFO/admin team to \
review. Read the full transcript you're given and produce a clear, structured summary with exactly \
these three labeled parts, in plain language a busy reader can scan quickly:

Problem: <one or two sentences, specific>
Why it matters: <in the person's own words/context where possible>
Suggested next step: <a concrete, actionable next step -- investigate, design a fix, follow up with \
the submitter for more detail, etc. Never a promise that this will be built -- that's the reviewer's \
call, not yours.>

Output only those three labeled parts, each starting on its own line with the exact label text above \
followed by a colon. No preamble, no sign-off, no other sections."""

_SUMMARY_REQUEST_TEXT = (
    "The conversation above is complete. Please write the structured summary now, "
    "in the exact three-part format from your instructions."
)


def register(app, *, current_user, current_org, render) -> None:
    global _current_user, _current_org, _render
    _current_user, _current_org, _render = current_user, current_org, render
    app.include_router(router)


def _read_secret(name: str) -> str:
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{_SECRET_PROJECT}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode("utf-8")


def _api_key() -> str:
    global _cached_key
    if _cached_key is None:
        _cached_key = _read_secret(_SECRET_NAME)
    return _cached_key


def _call_claude(system: str, messages: list[dict], max_tokens: int) -> str:
    """One plain (non-streaming, non-tool-use) Claude API call. Raises on
    error/timeout -- both call sites below catch this and degrade
    gracefully rather than losing the user's conversation."""
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(), timeout=30.0)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return next(b.text for b in resp.content if b.type == "text")


def _load_messages(conversation_id: int) -> list[dict]:
    return db.query(
        "SELECT role, content FROM checkreq.feedback_messages "
        "WHERE conversation_id = %s ORDER BY id",
        (conversation_id,),
    )


def _user_turn_count(conversation_id: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM checkreq.feedback_messages "
        "WHERE conversation_id = %s AND role = 'user'",
        (conversation_id,),
    )
    return row["c"] if row else 0


def _insert_message(conn, conversation_id: int, role: str, content: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkreq.feedback_messages (conversation_id, role, content) "
            "VALUES (%s, %s, %s)",
            (conversation_id, role, content),
        )


# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, submitted: bool = False):
    """Replaces the old one-shot textarea with the chat UI. If the user
    already has an open conversation (e.g. they navigated away mid-chat),
    resume it with its history intact rather than starting over."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")

    convo = db.query_one(
        "SELECT id FROM checkreq.feedback_conversations "
        "WHERE user_id = %s AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    )
    conversation_id = convo["id"] if convo else None
    messages = _load_messages(conversation_id) if conversation_id else []

    return _render(request, "feedback.html", user, {
        "submitted": submitted,
        "conversation_id": conversation_id,
        "messages": messages,
        "max_user_turns": MAX_USER_TURNS,
    })


@router.post("/feedback/message")
async def feedback_message(request: Request):
    """Send one user message, get Claude's reply, append both to
    feedback_messages. Creates a new open conversation on first use."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in."}, status_code=401)

    body = await request.json()
    text = (body.get("message") or "").strip()
    conversation_id = body.get("conversation_id")
    if not text:
        return JSONResponse({"error": "Message can't be empty."}, status_code=400)

    # Re-validate any client-supplied conversation_id against this user's
    # own open conversations -- never trust it blindly, same convention as
    # every other row-ownership check in this app.
    if conversation_id:
        row = db.query_one(
            "SELECT id FROM checkreq.feedback_conversations "
            "WHERE id = %s AND user_id = %s AND status = 'open'",
            (conversation_id, user["id"]),
        )
        if not row:
            conversation_id = None

    if not conversation_id:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkreq.feedback_conversations (user_id) "
                    "VALUES (%s) RETURNING id",
                    (user["id"],),
                )
                conversation_id = cur.fetchone()["id"]

    if _user_turn_count(conversation_id) >= MAX_USER_TURNS:
        return JSONResponse({
            "conversation_id": conversation_id,
            "reply": _WRAP_UP_MESSAGE,
            "limit_reached": True,
        })

    with db.connect() as conn:
        _insert_message(conn, conversation_id, "user", text)

    history = _load_messages(conversation_id)
    api_messages = [{"role": m["role"], "content": m["content"]} for m in history]

    try:
        reply = _call_claude(INTERVIEW_SYSTEM_PROMPT, api_messages, MAX_TOKENS)
    except Exception:
        reply = (
            "Sorry, I had trouble responding just now -- could you try sending "
            "that again in a moment?"
        )

    with db.connect() as conn:
        _insert_message(conn, conversation_id, "assistant", reply)

    return JSONResponse({
        "conversation_id": conversation_id,
        "reply": reply,
        "limit_reached": _user_turn_count(conversation_id) >= MAX_USER_TURNS,
    })


@router.post("/feedback/{conversation_id}/close")
async def feedback_close(conversation_id: int, request: Request):
    """Ends the conversation: one final Claude call over the full
    transcript produces the structured summary, which becomes the
    checkreq.app_feedback row's comment. Conversation is marked closed and
    linked to that row either way -- feedback_messages itself is never
    touched here, preserving the raw transcript for the admin "view full
    conversation" link."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not signed in."}, status_code=401)

    convo = db.query_one(
        "SELECT id, status FROM checkreq.feedback_conversations "
        "WHERE id = %s AND user_id = %s",
        (conversation_id, user["id"]),
    )
    if not convo:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)
    if convo["status"] == "closed":
        return JSONResponse({"error": "This conversation was already submitted."}, status_code=400)

    history = _load_messages(conversation_id)
    if not history:
        return JSONResponse({"error": "Say a bit about your feedback before submitting."}, status_code=400)

    api_messages = [{"role": m["role"], "content": m["content"]} for m in history]
    api_messages.append({"role": "user", "content": _SUMMARY_REQUEST_TEXT})

    try:
        summary = _call_claude(SUMMARY_SYSTEM_PROMPT, api_messages, SUMMARY_MAX_TOKENS)
    except Exception:
        return JSONResponse({
            "error": "Couldn't generate a summary just now -- please try submitting again in a moment.",
        }, status_code=502)

    org = _current_org(request)  # nullable -- feedback can be given before any entity is picked
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_feedback (org_id, submitted_by_user_id, comment) "
                "VALUES (%s, %s, %s) RETURNING id",
                (org["id"] if org else None, user["id"], summary),
            )
            feedback_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE checkreq.feedback_conversations "
                "SET status = 'closed', closed_at = NOW(), feedback_id = %s "
                "WHERE id = %s",
                (feedback_id, conversation_id),
            )

    return JSONResponse({"feedback_id": feedback_id, "summary": summary})


@router.get("/admin/feedback/conversation/{conversation_id}", response_class=HTMLResponse)
def admin_feedback_conversation(conversation_id: int, request: Request):
    """Read-only full-transcript view, linked from admin_feedback.html
    wherever a feedback row has a linked conversation. Same CFO-only gate
    as the feedback log itself."""
    import rbac

    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not rbac.user_has_role(user["id"], "cfo", org_id=None):
        return JSONResponse({"error": "CFO access required"}, status_code=403)

    convo = db.query_one(
        "SELECT c.id, c.status, c.created_at, c.closed_at, "
        "u.display_name AS submitter_name, u.email AS submitter_email "
        "FROM checkreq.feedback_conversations c "
        "JOIN checkreq.app_users u ON u.id = c.user_id "
        "WHERE c.id = %s",
        (conversation_id,),
    )
    if not convo:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)

    messages = _load_messages(conversation_id)
    return _render(request, "admin_feedback_conversation.html", user, {
        "convo": convo,
        "messages": messages,
    })
