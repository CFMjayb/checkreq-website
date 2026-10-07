"""
outreach_kinds.py -- 26-156: the 'poll' kind of the shared Email Response Engine,
and the question types a poll is made of.

A poll is a campaign with 1..25 questions. A *survey* is simply a poll with several
questions -- there is no separate engine for it. Each question has a type; the
built-in types are yes_no, single_choice, multi_choice and text.

HOW TO ADD A CUSTOM QUESTION TYPE (the "more customized, depending on the poll
need" requirement): subclass QuestionType, give it a key, a label, an
input_template (a Jinja partial under templates/ that renders the input), and
implement normalize_config / parse / tally / describe (and email_choices if its
answer can be given straight from an email button). Then call
register_question_type(YourType()) at import time. It is code on purpose: admins
choose among types, they never author HTML or code.

The engine (outreach.py) never knows about polls: it only calls the Kind methods
below. The SMA letters (26-129 plan rev 11) will register their own Kind the same way.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import db
import outreach
from outreach import Kind, OutreachError, esc, text_to_html

MAX_QUESTIONS = 25
MIN_OPTIONS, MAX_OPTIONS = 2, 12
MAX_PROMPT = 500
MAX_LABEL = 120
MAX_EMAIL_BUTTONS = 5


# ---------------------------------------------------------------------------
# Question types
# ---------------------------------------------------------------------------
class QuestionType:
    key = ""
    label = ""
    input_template = ""   # Jinja partial: gets q, field, selected, error

    def normalize_config(self, config: dict) -> tuple[dict, list[str]]:
        return {}, []

    def parse(self, q: dict, values: list[str]) -> tuple[dict | None, str | None]:
        """Raw submitted strings -> (answer, error). (None, None) = skipped optional question."""
        raise NotImplementedError

    def tally(self, q: dict, answers: list[dict]) -> dict:
        raise NotImplementedError

    def describe(self, q: dict, answer: dict | None) -> str:
        """One human-readable line for the results table and the CSV."""
        raise NotImplementedError

    def email_choices(self, q: dict) -> list[tuple[str, str]]:
        """[(choice_key, label)] if this question can be answered from an email button."""
        return []

    def selected_from_answer(self, answer: dict | None):
        return None

    def valid_choice(self, q: dict, key: str) -> bool:
        return key in {k for k, _ in self.email_choices(q)}


QUESTION_TYPES: dict[str, QuestionType] = {}


def register_question_type(qt: QuestionType) -> None:
    QUESTION_TYPES[qt.key] = qt


def _first(values: list[str]) -> str:
    return (values[0] if values else "").strip()


class YesNo(QuestionType):
    key = "yes_no"
    label = "Yes / No"
    input_template = "poll_input_yes_no.html"

    def normalize_config(self, config):
        config = config or {}
        yes = str(config.get("yes_label") or "Yes").strip()[:40] or "Yes"
        no = str(config.get("no_label") or "No").strip()[:40] or "No"
        return {"yes_label": yes, "no_label": no}, []

    def _labels(self, q):
        cfg = q.get("config") or {}
        return {"yes": cfg.get("yes_label", "Yes"), "no": cfg.get("no_label", "No")}

    def email_choices(self, q):
        lab = self._labels(q)
        return [("yes", lab["yes"]), ("no", lab["no"])]

    def parse(self, q, values):
        v = _first(values)
        if not v:
            return (None, "Please choose an answer.") if q["required"] else (None, None)
        if v not in ("yes", "no"):
            return None, "That is not one of the choices."
        return {"choice": v, "label": self._labels(q)[v]}, None

    def selected_from_answer(self, answer):
        return (answer or {}).get("choice")

    def tally(self, q, answers):
        lab = self._labels(q)
        counts = {"yes": 0, "no": 0}
        for a in answers:
            if a.get("choice") in counts:
                counts[a["choice"]] += 1
        return {"kind": "counts", "total": len(answers),
                "rows": [{"label": lab[k], "count": counts[k]} for k in ("yes", "no")]}

    def describe(self, q, answer):
        return (answer or {}).get("label", "")


def _normalize_options(config: dict) -> tuple[list[dict], list[str]]:
    raw = (config or {}).get("options") or []
    labels, seen = [], set()
    for o in raw:
        label = str(o.get("label") if isinstance(o, dict) else o).strip()
        if not label:
            continue
        if len(label) > MAX_LABEL:
            return [], [f"An option is longer than {MAX_LABEL} characters."]
        if label.lower() in seen:
            return [], [f"The option '{label}' is listed twice."]
        seen.add(label.lower())
        labels.append(label)
    if not (MIN_OPTIONS <= len(labels) <= MAX_OPTIONS):
        return [], [f"Give between {MIN_OPTIONS} and {MAX_OPTIONS} options."]
    return [{"key": f"o{i}", "label": lab} for i, lab in enumerate(labels, start=1)], []


class SingleChoice(QuestionType):
    key = "single_choice"
    label = "Multiple choice (pick one)"
    input_template = "poll_input_single_choice.html"

    def normalize_config(self, config):
        opts, errors = _normalize_options(config)
        return {"options": opts}, errors

    def _opts(self, q):
        return {o["key"]: o["label"] for o in (q.get("config") or {}).get("options", [])}

    def email_choices(self, q):
        opts = (q.get("config") or {}).get("options", [])
        return [(o["key"], o["label"]) for o in opts] if len(opts) <= 4 else []

    def valid_choice(self, q, key):
        return key in self._opts(q)

    def parse(self, q, values):
        v = _first(values)
        if not v:
            return (None, "Please choose an answer.") if q["required"] else (None, None)
        opts = self._opts(q)
        if v not in opts:
            return None, "That is not one of the choices."
        return {"choice": v, "label": opts[v]}, None

    def selected_from_answer(self, answer):
        return (answer or {}).get("choice")

    def tally(self, q, answers):
        opts = (q.get("config") or {}).get("options", [])
        counts = {o["key"]: 0 for o in opts}
        for a in answers:
            if a.get("choice") in counts:
                counts[a["choice"]] += 1
        return {"kind": "counts", "total": len(answers),
                "rows": [{"label": o["label"], "count": counts[o["key"]]} for o in opts]}

    def describe(self, q, answer):
        return (answer or {}).get("label", "")


class MultiChoice(QuestionType):
    key = "multi_choice"
    label = "Multiple choice (pick several)"
    input_template = "poll_input_multi_choice.html"

    def normalize_config(self, config):
        opts, errors = _normalize_options(config)
        if errors:
            return {"options": []}, errors
        lo = int((config or {}).get("min_select") or 0)
        hi = int((config or {}).get("max_select") or len(opts))
        if not (0 <= lo <= hi <= len(opts)):
            return {"options": opts}, ["The minimum and maximum number of picks do not fit the options."]
        return {"options": opts, "min_select": lo, "max_select": hi}, []

    def _opts(self, q):
        return {o["key"]: o["label"] for o in (q.get("config") or {}).get("options", [])}

    def parse(self, q, values):
        picked = []
        for v in values:
            v = v.strip()
            if v and v not in picked:
                picked.append(v)
        if not picked:
            return (None, "Please choose at least one.") if q["required"] else (None, None)
        opts = self._opts(q)
        if any(v not in opts for v in picked):
            return None, "That is not one of the choices."
        cfg = q.get("config") or {}
        lo, hi = cfg.get("min_select", 0), cfg.get("max_select", len(opts))
        if len(picked) < max(lo, 1 if q["required"] else 0):
            return None, f"Please choose at least {max(lo, 1)}."
        if len(picked) > hi:
            return None, f"Please choose no more than {hi}."
        order = list(opts)
        picked.sort(key=order.index)
        return {"choices": picked, "labels": [opts[v] for v in picked]}, None

    def selected_from_answer(self, answer):
        return list((answer or {}).get("choices", []))

    def tally(self, q, answers):
        opts = (q.get("config") or {}).get("options", [])
        counts = {o["key"]: 0 for o in opts}
        for a in answers:
            for k in a.get("choices", []):
                if k in counts:
                    counts[k] += 1
        return {"kind": "counts", "total": len(answers),
                "rows": [{"label": o["label"], "count": counts[o["key"]]} for o in opts]}

    def describe(self, q, answer):
        return "; ".join((answer or {}).get("labels", []))


class FreeText(QuestionType):
    key = "text"
    label = "Short written answer"
    input_template = "poll_input_text.html"

    def normalize_config(self, config):
        config = config or {}
        try:
            mx = int(config.get("max_length") or 1000)
        except (TypeError, ValueError):
            return {}, ["The maximum length must be a number."]
        if not (1 <= mx <= 5000):
            return {}, ["The maximum length must be between 1 and 5000 characters."]
        return {"max_length": mx, "multiline": bool(config.get("multiline", True))}, []

    def parse(self, q, values):
        # A browser counts a line break as ONE character in the box but submits it as CRLF (two):
        # normalize before measuring, or a long multi-line answer the box accepted is refused here.
        v = _first(values).replace("\r\n", "\n").replace("\r", "\n")
        if not v:
            return (None, "Please write an answer.") if q["required"] else (None, None)
        mx = (q.get("config") or {}).get("max_length", 1000)
        if len(v) > mx:
            return None, f"Please keep it to {mx} characters or fewer."
        return {"text": v}, None

    def selected_from_answer(self, answer):
        return (answer or {}).get("text")

    def tally(self, q, answers):
        texts = [a.get("text", "") for a in answers if a.get("text")]
        return {"kind": "text", "total": len(texts), "responses": texts[:100]}

    def describe(self, q, answer):
        return (answer or {}).get("text", "")


for _qt in (YesNo(), SingleChoice(), MultiChoice(), FreeText()):
    register_question_type(_qt)


# ---------------------------------------------------------------------------
# The poll kind
# ---------------------------------------------------------------------------
class PollKind(Kind):
    key = "poll"
    label = "Poll"
    template = "respond_poll_form.html"
    allow_list_audience = False   # polls go to role-holders; a typed-in list is for SMA-style kinds

    # -- questions -------------------------------------------------------
    def questions(self, campaign_id: int) -> list[dict]:
        return db.query("SELECT * FROM portal.poll_questions WHERE campaign_id = %s ORDER BY position",
                        (campaign_id,))

    def normalize_questions(self, specs: list[dict]) -> list[tuple]:
        """Validate question specs WITHOUT writing: -> [(qtype, prompt, required, config)].
        Raises OutreachError listing every problem. The admin screen runs this first so a
        bad form never leaves a half-saved draft behind."""
        errors, clean = [], []
        if not isinstance(specs, list) or not (1 <= len(specs) <= MAX_QUESTIONS):
            raise OutreachError(f"A poll needs between 1 and {MAX_QUESTIONS} questions.")
        for i, s in enumerate(specs, start=1):
            if not isinstance(s, dict):
                errors.append(f"Question {i}: not a valid question.")
                continue
            tag = f"Question {i}: "
            qt = QUESTION_TYPES.get(s.get("qtype"))
            prompt = str(s.get("prompt") or "").strip()
            if not qt:
                errors.append(tag + "unknown question type.")
                continue
            if not prompt:
                errors.append(tag + "the question text is empty.")
                continue
            if len(prompt) > MAX_PROMPT:
                errors.append(tag + f"the question is longer than {MAX_PROMPT} characters.")
                continue
            config, cfg_errors = qt.normalize_config(s.get("config") or {})
            errors += [tag + e for e in cfg_errors]
            clean.append((qt.key, prompt, bool(s.get("required", True)), config))
        if errors:
            raise OutreachError(errors)
        return clean

    def set_questions(self, campaign_id: int, specs: list[dict]) -> list[int]:
        """Replace a DRAFT poll's questions. spec = {qtype, prompt, required, config}."""
        c = outreach.get_campaign(campaign_id)
        if not c or c["kind"] != "poll":
            raise OutreachError("Poll not found.")
        if c["status"] != "draft":
            raise OutreachError("The questions can only change while the poll is a draft.")
        clean = self.normalize_questions(specs)
        ids = []
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM portal.poll_questions WHERE campaign_id = %s", (campaign_id,))
                for pos, (qtype, prompt, required, config) in enumerate(clean, start=1):
                    cur.execute(
                        "INSERT INTO portal.poll_questions (campaign_id, position, qtype, prompt, required, config) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (campaign_id, pos, qtype, prompt, required, Jsonb(config)))
                    ids.append(cur.fetchone()["id"])
        return ids

    # -- Kind interface --------------------------------------------------
    def validate_ready(self, campaign):
        return [] if self.questions(campaign["id"]) else ["Add at least one question before sending."]

    def email_parts(self, campaign, recipient, urls):
        qs = self.questions(campaign["id"])
        intro_html = text_to_html(campaign["intro"])
        intro_text = (campaign["intro"] or "").strip()
        if len(qs) == 1:
            q = qs[0]
            choices = QUESTION_TYPES[q["qtype"]].email_choices(q)
            if 2 <= len(choices) <= MAX_EMAIL_BUTTONS:
                body = (intro_html + f'<p style="margin:0 0 8px 0;font-weight:bold;">{esc(q["prompt"])}</p>'
                        '<p style="margin:0 0 8px 0;">Tap your answer. It is recorded right away, and you can change it afterwards.</p>')
                text = (intro_text + "\n\n" if intro_text else "") + q["prompt"] + "\nChoose an answer (it is recorded right away, and you can change it afterwards):"
                return {"headline": campaign["title"], "body_html": body, "body_text": text,
                        "buttons": [{"label": lab, "url": urls["choice"](key), "primary": True}
                                    for key, lab in choices]}
        items = "".join(f'<li style="margin:2px 0;">{esc(q["prompt"])}</li>' for q in qs[:10])
        more = f"<li>and {len(qs) - 10} more</li>" if len(qs) > 10 else ""
        body = (intro_html + f'<p style="margin:0 0 6px 0;">{len(qs)} question{"s" if len(qs) != 1 else ""}:</p>'
                f'<ul style="margin:0 0 8px 18px;padding:0;">{items}{more}</ul>')
        text = ((intro_text + "\n\n") if intro_text else "") + "\n".join(f"- {q['prompt']}" for q in qs[:10])
        return {"headline": campaign["title"], "body_html": body, "body_text": text,
                "buttons": [{"label": "Respond now", "url": urls["respond"], "primary": True}]}

    def page_context(self, campaign, recipient, preselect, submitted=None):
        qs = self.questions(campaign["id"])
        existing = {r["question_id"]: r["answer"] for r in db.query(
            "SELECT question_id, answer FROM portal.poll_answers WHERE recipient_id = %s", (recipient["id"],))}
        items = []
        for q in qs:
            qt = QUESTION_TYPES[q["qtype"]]
            field = f"q{q['id']}"
            if submitted is not None:
                vals = submitted.get(field, [])
                selected = list(vals) if q["qtype"] == "multi_choice" else (vals[0] if vals else None)
            else:
                selected = qt.selected_from_answer(existing.get(q["id"]))
                # An email button ("?a=yes") preselects the answer, but never overrides a recorded one.
                if len(qs) == 1 and preselect and selected is None and qt.valid_choice(q, preselect):
                    selected = [preselect] if q["qtype"] == "multi_choice" else preselect
            items.append({"q": q, "qt": qt, "field": field, "selected": selected})
        return {"items": items, "has_answers": bool(existing)}

    def parse_response(self, campaign, recipient, form):
        answers, errors = {}, []
        for q in self.questions(campaign["id"]):
            ans, err = QUESTION_TYPES[q["qtype"]].parse(q, form.get(f"q{q['id']}", []))
            if err:
                errors.append(f"{q['prompt'][:70]}: {err}")
            elif ans is not None:
                answers[q["id"]] = ans
        return answers, errors

    def save_response(self, cur, campaign, recipient, parsed):
        cur.execute("SELECT question_id, answer FROM portal.poll_answers WHERE recipient_id = %s", (recipient["id"],))
        existing = {r["question_id"]: r["answer"] for r in cur.fetchall()}
        changed = False
        for qid, ans in parsed.items():
            if existing.get(qid) != ans:
                changed = True
                cur.execute(
                    "INSERT INTO portal.poll_answers (recipient_id, question_id, answer) VALUES (%s,%s,%s) "
                    "ON CONFLICT (recipient_id, question_id) DO UPDATE SET answer = EXCLUDED.answer, updated_at = NOW()",
                    (recipient["id"], qid, Jsonb(ans)))
        for qid in set(existing) - set(parsed):   # an optional question was cleared
            changed = True
            cur.execute("DELETE FROM portal.poll_answers WHERE recipient_id = %s AND question_id = %s",
                        (recipient["id"], qid))
        return {"answers": {str(k): v for k, v in parsed.items()}, **({} if changed else {"unchanged": True})}

    def summary(self, campaign):
        out = []
        rows = db.query("SELECT question_id, answer FROM portal.poll_answers a "
                        "JOIN portal.outreach_recipients r ON r.id = a.recipient_id "
                        "WHERE r.campaign_id = %s", (campaign["id"],))
        by_q: dict[int, list[dict]] = {}
        for r in rows:
            by_q.setdefault(r["question_id"], []).append(r["answer"])
        for q in self.questions(campaign["id"]):
            qt = QUESTION_TYPES[q["qtype"]]
            out.append({"q": q, "type_label": qt.label, "tally": qt.tally(q, by_q.get(q["id"], []))})
        return {"questions": out}

    def quick_answer(self, campaign, recipient, choice):
        """A one-question poll whose email offers buttons: the button's choice IS the answer."""
        qs = self.questions(campaign["id"])
        if len(qs) != 1:
            return None
        q = qs[0]
        choices = QUESTION_TYPES[q["qtype"]].email_choices(q)
        if not (2 <= len(choices) <= MAX_EMAIL_BUTTONS) or choice not in {k for k, _ in choices}:
            return None
        return {f"q{q['id']}": [choice]}

    def received_answers(self, campaign, recipient):
        qs = self.questions(campaign["id"])
        got = {r["question_id"]: r["answer"] for r in db.query(
            "SELECT question_id, answer FROM portal.poll_answers WHERE recipient_id = %s", (recipient["id"],))}
        return [(q["prompt"], QUESTION_TYPES[q["qtype"]].describe(q, got.get(q["id"])) or "(no answer)") for q in qs]

    def answers_by_recipient(self, campaign):
        qs = self.questions(campaign["id"])
        by_id = {q["id"]: q for q in qs}
        out: dict[int, list[tuple[str, str]]] = {}
        rows = db.query(
            "SELECT a.recipient_id, a.question_id, a.answer FROM portal.poll_answers a "
            "JOIN portal.outreach_recipients r ON r.id = a.recipient_id WHERE r.campaign_id = %s", (campaign["id"],))
        got = {(r["recipient_id"], r["question_id"]): r["answer"] for r in rows}
        for rid in {r["recipient_id"] for r in rows}:
            out[rid] = [(q["prompt"], QUESTION_TYPES[q["qtype"]].describe(q, got.get((rid, q["id"]))) or "(no answer)")
                        for q in qs]
        return out

    def export_rows(self, campaign) -> tuple[list[str], list[list[str]]]:
        """(headers, rows) for the results CSV: one row per recipient."""
        qs = self.questions(campaign["id"])
        answers = {(r["recipient_id"], r["question_id"]): r["answer"] for r in db.query(
            "SELECT a.recipient_id, a.question_id, a.answer FROM portal.poll_answers a "
            "JOIN portal.outreach_recipients r ON r.id = a.recipient_id WHERE r.campaign_id = %s",
            (campaign["id"],))}
        headers = ["Name", "Email", "Role", "Email sent", "Opened (signal)", "Link visited", "Responded"] + \
                  [q["prompt"] for q in qs]
        rows = []
        for r in outreach.recipients_report(campaign["id"]):
            rows.append([
                r["name"], r["email"], (r.get("meta") or {}).get("via", [""])[0] if (r.get("meta") or {}).get("via") else r["role_label"],
                r["send_status"], "yes" if r["first_open_at"] else "", "yes" if r["first_click_at"] else "",
                r["responded_at"].isoformat(timespec="minutes") if r["responded_at"] else "",
            ] + [QUESTION_TYPES[q["qtype"]].describe(q, answers.get((r["id"], q["id"]))) for q in qs])
        return headers, rows


outreach.register_kind(PollKind())
POLL = outreach.KINDS["poll"]
