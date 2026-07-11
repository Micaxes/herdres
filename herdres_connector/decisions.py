"""Remote answering of Claude Code follow-up prompts (AskUserQuestion / ExitPlanMode).

Claude's interactive prompts block a herdr pane waiting for a keyboard selection. This module
surfaces them into the worker's Telegram topic as a native **reply keyboard** so the owner can
answer from their phone; a tap is replayed as `herdr pane send-keys` on the blocked pane.

Data flow (all host-local — tendwire's neutral projection drops the structured payload, so we do
NOT read it from tendwire; see herdres-remote-interview-arch memory):

  1. A PreToolUse hook (herdres_pending_hook.py) writes ~/.local/share/herdres/pending/<session>.json
     = {tool_use_id, name, input, session_id, ts} while a prompt is pending; removes it when answered.
  2. `herdr api snapshot` maps session_id -> {pane_id, terminal_id, space_id, agent_status}.
  3. tendwire's own hash (worker_binding_private_fingerprint) turns terminal_id into the stable_key
     the connector already keys topics by -> resolve the worker's entry/topic.
  4. The adapter's canonical mapping (claude_decision_turn_fields) turns the raw tool_use into the
     button/option payload (single-select buttons, multi-select toggles, plan approve/revise).

Answering (P3) matches a tapped label against the topic's active decision and runs the calibrated
send-keys sequence; auto-disable (P4) removes the keyboard when the pending file disappears (answered
at the laptop) or the decision changes.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from . import state
from .safe import sanitize_text

# The freeform "write a different answer" option carries an empty send_text; a tap enters a
# type-your-answer capture instead of an immediate selection.
CUSTOM_OPTION_ID = "custom"
FREEFORM_LABEL = "✍️ Write a different answer"
SUBMIT_LABEL = "✅ Submit answer"
SUBMIT_OPTION_ID = "__submit__"


def _herdr_bin() -> str:
    return os.environ.get("HERDR_REAL_BIN") or os.environ.get("HERDR_BIN") or "herdr"


def _pending_dir():
    """The pending-decision directory the hook writes to. Reuses the adapter's resolver so the two
    stay byte-identical; falls back to the documented default if the adapter can't be imported."""
    try:
        from herdr_turn_adapter import pending_decision_dir

        return pending_decision_dir()
    except Exception:  # noqa: BLE001
        from pathlib import Path

        base = os.environ.get("HERDRES_PENDING_DIR")
        return Path(base) if base else (Path.home() / ".local" / "share" / "herdres" / "pending")


def decisions_enabled() -> bool:
    """Master switch for the remote-answer feature (default ON, degrades to the plain attention
    notice when off or when any resolution step fails)."""
    return os.environ.get("HERDRES_REMOTE_DECISIONS", "1").strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# herdr snapshot -> session index -> stable_key
# ---------------------------------------------------------------------------

def _run_herdr_snapshot(timeout: float = 10.0) -> dict[str, Any]:
    try:
        out = subprocess.run(
            [_herdr_bin(), "api", "snapshot"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not isinstance(data, dict):
        return {}
    snap = data.get("result", {}).get("snapshot") if isinstance(data.get("result"), dict) else None
    return snap if isinstance(snap, dict) else (data if "panes" in data else {})


def pane_index_by_session(snapshot: dict[str, Any]) -> dict[str, dict[str, str]]:
    """{ agent_session.value : {pane_id, terminal_id, space_id, agent_status} } for agent panes."""
    index: dict[str, dict[str, str]] = {}
    for pane in snapshot.get("panes", []) if isinstance(snapshot, dict) else []:
        if not isinstance(pane, dict):
            continue
        session = pane.get("agent_session")
        session_id = str(session.get("value") or "") if isinstance(session, dict) else str(session or "")
        if not session_id:
            continue
        index[session_id] = {
            "pane_id": str(pane.get("pane_id") or ""),
            "terminal_id": str(pane.get("terminal_id") or ""),
            "space_id": str(pane.get("workspace_id") or pane.get("space_id") or ""),
            "agent_status": str(pane.get("agent_status") or ""),
        }
    return index


def _stable_key_hash(host_id: str, kind: str, value: str, space_id: str) -> str:
    """tendwire's exact per-pane fingerprint for one (kind, value) identity, or "" if unavailable.
    Uses tendwire's own hash so the result equals worker.meta.stable_key for that identity."""
    if not host_id or not value:
        return ""
    try:
        from tendwire.backends.herdr_cli import _BACKEND_NAME, worker_binding_private_fingerprint

        return worker_binding_private_fingerprint(
            host_id=host_id,
            backend=_BACKEND_NAME,
            identity_material={"stable_pane": {"kind": kind, "value": value, "space_id": space_id}},
        )
    except Exception:  # noqa: BLE001
        return ""


def stable_key_candidates(host_id: str, terminal_id: str, pane_id: str, space_id: str) -> list[str]:
    """Candidate stable_keys for a herdr pane, in match-preference order (terminal_id, then pane_id).

    tendwire hashes a pane by ONE identity — its durable terminal_id when it can see it, else the
    positional pane_id (tendwire's `_stable_pane_identity`). But the connector's live `herdr api
    snapshot` and the *stored* topic state can disagree about which identity minted a topic's key:
    terminal_id is surfaced by the real herdr binary now, yet a topic created earlier — or via the
    turn-adapter view that hides terminal_id, so tendwire fell back to pane_id — stays keyed by
    pane_id. Recomputing only the preferred identity silently misses those topics (the bug that left
    Claude prompts with no reply keyboard). So we return BOTH and let find_entry_key_by_stable_key
    pick the live match; the 96-bit hashes never collide in practice, so trying both is safe."""
    out: list[str] = []
    for kind, value in (("terminal_id", terminal_id), ("pane_id", pane_id)):
        key = _stable_key_hash(host_id, kind, str(value or ""), space_id)
        if key and key not in out:
            out.append(key)
    return out


def stable_key_for(host_id: str, terminal_id: str, pane_id: str, space_id: str) -> str:
    """Preferred (terminal_id-first) stable_key for a pane; "" when none can be computed. Kept for
    single-key callers — the resolver uses stable_key_candidates so it matches either scheme."""
    keys = stable_key_candidates(host_id, terminal_id, pane_id, space_id)
    return keys[0] if keys else ""


def pending_file_present(session_id: str) -> bool:
    """True if a valid, non-stale pending-decision file still exists for this session. This is the
    ground-truth 'still waiting' signal (the hook removes the file the instant the prompt is
    answered) — used for auto-disable so a transient `herdr api snapshot` hiccup can never make an
    active decision look answered."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    try:
        from herdr_turn_adapter import read_pending_decision

        return read_pending_decision(sid) is not None
    except Exception:  # noqa: BLE001
        try:
            return (_pending_dir() / f"{sid}.json").exists()
        except OSError:
            return False


def _read_pending_files() -> list[dict[str, Any]]:
    """Load valid pending-decision files (name stem == session_id). Reuses the adapter's
    read_pending_decision for validation + TTL when available."""
    directory = _pending_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    try:
        from herdr_turn_adapter import read_pending_decision
    except Exception:  # noqa: BLE001
        read_pending_decision = None  # type: ignore[assignment]
    out: list[dict[str, Any]] = []
    for path in paths:
        session_id = path.stem
        data: dict[str, Any] | None = None
        if read_pending_decision is not None:
            data = read_pending_decision(session_id)
        else:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("input"), dict):
                    data = loaded
            except (OSError, ValueError):
                data = None
        if data:
            record = dict(data)
            record.setdefault("session_id", session_id)
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Resolved decision
# ---------------------------------------------------------------------------

@dataclass
class ResolvedDecision:
    session_id: str
    pane_id: str
    topic_id: str
    entry_key: str
    decision_id: str
    kind: str  # "single" | "multi" | "plan"
    prompt: str
    options: list[dict[str, str]] = field(default_factory=list)  # single/plan: {id,label,send_text}
    questions: list[dict[str, Any]] = field(default_factory=list)  # multi: normalized questions

    def content_hash(self) -> str:
        from .safe import short_hash

        return short_hash(
            {"d": self.decision_id, "k": self.kind, "p": self.prompt,
             "o": self.options, "q": self.questions},
            20,
        )


def _decision_from_fields(fields: dict[str, Any]) -> tuple[str, str, str, list, list] | None:
    """(kind, decision_id, prompt, options, questions) from claude_decision_turn_fields output."""
    if "pending_decision" in fields and isinstance(fields["pending_decision"], dict):
        pd = fields["pending_decision"]
        options = [o for o in pd.get("options", []) if isinstance(o, dict)]
        # ExitPlanMode uses fixed approve/revise ids; everything else is a single-select question.
        kind = "plan" if any(str(o.get("id")) in ("approve", "revise") for o in options) else "single"
        return kind, str(pd.get("decision_id") or ""), str(pd.get("prompt") or ""), options, []
    if "pending_interaction" in fields and isinstance(fields["pending_interaction"], dict):
        pi = fields["pending_interaction"]
        questions = [q for q in pi.get("questions", []) if isinstance(q, dict)]
        return "multi", str(pi.get("interaction_id") or ""), str(pi.get("prompt") or "Input needed."), [], questions
    return None


def resolve_decisions(store: dict[str, Any], host_id: str) -> list[ResolvedDecision]:
    """Join every live pending-decision file to its Telegram topic, host-locally. Returns [] on any
    infrastructure gap (no host_id, herdr snapshot unavailable, unknown topic) — a safe no-op."""
    files = _read_pending_files()
    if not files:
        return []
    try:
        from herdr_turn_adapter import claude_decision_turn_fields
    except Exception:  # noqa: BLE001
        return []
    snapshot = _run_herdr_snapshot()
    session_index = pane_index_by_session(snapshot)
    resolved: list[ResolvedDecision] = []
    for record in files:
        session_id = str(record.get("session_id") or "")
        pane = session_index.get(session_id)
        if not pane or not pane.get("pane_id"):
            continue  # pane gone or not tracked -> can't route/answer
        entry_key = ""
        for candidate in stable_key_candidates(host_id, pane["terminal_id"], pane["pane_id"], pane["space_id"]):
            entry_key = state.find_entry_key_by_stable_key(store, candidate) or ""
            if entry_key:
                break
        if not entry_key:
            continue  # unknown/ambiguous topic -> leave to the plain attention notice
        # Worker entries (the key came from find_entry_key_by_stable_key over worker entries) — do NOT
        # use source_entries here, which is topic-mode dependent and would miss it in space mode.
        entry = state.source_worker_entries(store).get(entry_key) or {}
        topic_id = str(entry.get("topic_id") or "")
        if not topic_id:
            continue
        fields = claude_decision_turn_fields(record)
        if not fields:
            continue
        parsed = _decision_from_fields(fields)
        if not parsed:
            continue
        kind, decision_id, prompt, options, questions = parsed
        if not decision_id:
            continue
        resolved.append(
            ResolvedDecision(
                session_id=session_id,
                pane_id=pane["pane_id"],
                topic_id=topic_id,
                entry_key=entry_key,
                decision_id=decision_id,
                kind=kind,
                prompt=prompt,
                options=options,
                questions=questions,
            )
        )
    return resolved


# ---------------------------------------------------------------------------
# Rendering: reply keyboard + message body
# ---------------------------------------------------------------------------

def _keyboard_rows(labels: list[str], per_row: int = 1) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for label in labels:
        button = {"text": sanitize_text(label, 120)}
        if per_row == 1 or not rows or len(rows[-1]) >= per_row:
            rows.append([button])
        else:
            rows[-1].append(button)
    return rows


def reply_keyboard(decision: ResolvedDecision, selected: list[str] | None = None) -> dict[str, Any]:
    """Native ReplyKeyboardMarkup for a decision. Single/plan: one button per option (one-time).
    Multi: options with a ✓ marker on selected ones plus a Submit row (persistent until submit)."""
    selected = selected or []
    if decision.kind == "multi":
        labels: list[str] = []
        # v1 supports a single multi-select question well (calibrated); extra questions are shown but
        # answered together in order.
        for q in decision.questions:
            for opt in q.get("options", []):
                if not isinstance(opt, dict):
                    continue
                oid = f"{q.get('question_id')}:{opt.get('option_id')}"
                mark = "✅ " if oid in selected else "▫️ "
                labels.append(f"{mark}{opt.get('label')}")
        keyboard = _keyboard_rows(labels, per_row=1)
        keyboard.append([{"text": SUBMIT_LABEL}])
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False,
            # selective MUST stay False: a selective reply keyboard is shown ONLY to users @mentioned
            # in the text or the user being replied to. Decision messages are standalone forum-topic
            # posts (no reply_to, no @mention), so selective=True renders the text but hides the
            # keyboard from everyone — "text arrives, buttons never appear". (selective does NOT scope
            # a keyboard per-topic; that's handled by matching a tap to the topic's own active decision.)
            "selective": False,
            "input_field_placeholder": "Tap options, then Submit",
        }
    labels = [str(o.get("label") or "") for o in decision.options if str(o.get("label") or "")]
    return {
        "keyboard": _keyboard_rows(labels, per_row=1),
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "selective": False,  # see multi-branch note: selective=True hides the keyboard on standalone posts
        "input_field_placeholder": "Tap an answer",
    }


def remove_keyboard() -> dict[str, Any]:
    # selective=False so the removal is chat-wide, matching the chat-wide (selective=False) keyboard
    # we posted; a selective removal would only retract for @mentioned/replied users and orphan it.
    return {"remove_keyboard": True, "selective": False}


def render_decision_html(decision: ResolvedDecision, selected: list[str] | None = None) -> str:
    from .safe import html_escape

    def esc(text: str) -> str:
        return html_escape(str(text), 1200)

    head = {"plan": "📋 <b>Plan review</b>", "multi": "🔀 <b>Choose one or more</b>",
            "single": "❓ <b>Claude is asking</b>"}.get(decision.kind, "❓ <b>Claude is asking</b>")
    lines = [head, esc(decision.prompt)]
    if decision.kind == "multi":
        selected = selected or []
        for q in decision.questions:
            title = esc(str(q.get("title") or ""))
            picks = [
                str(o.get("label"))
                for o in q.get("options", [])
                if isinstance(o, dict) and f"{q.get('question_id')}:{o.get('option_id')}" in selected
            ]
            suffix = f" — <i>{esc(', '.join(picks))}</i>" if picks else ""
            lines.append(f"• {title}{suffix}")
        lines.append("<i>Tap to toggle, then Submit.</i>")
    else:
        lines.append("<i>Tap a button below to answer here.</i>")
    return "\n".join(part for part in lines if part)


# ---------------------------------------------------------------------------
# Active-decision state (shared via the connector store / state.json)
# ---------------------------------------------------------------------------

def _active_map(store: dict[str, Any]) -> dict[str, Any]:
    bucket = store.setdefault("decisions", {})
    if not isinstance(bucket, dict):
        bucket = {}
        store["decisions"] = bucket
    active = bucket.setdefault("active", {})
    if not isinstance(active, dict):
        active = {}
        bucket["active"] = active
    return active


def get_active(store: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    entry = _active_map(store).get(str(topic_id))
    return entry if isinstance(entry, dict) else None


def set_active(store: dict[str, Any], topic_id: str, record: dict[str, Any]) -> None:
    _active_map(store)[str(topic_id)] = record


def clear_active(store: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    return _active_map(store).pop(str(topic_id), None)


def decision_from_record(record: dict[str, Any], topic_id: str = "") -> ResolvedDecision:
    """Rebuild a ResolvedDecision from a stored active record (for re-rendering on multi toggle)."""
    return ResolvedDecision(
        session_id=str(record.get("session_id") or ""),
        pane_id=str(record.get("pane_id") or ""),
        topic_id=str(topic_id or ""),
        entry_key=str(record.get("entry_key") or ""),
        decision_id=str(record.get("decision_id") or ""),
        kind=str(record.get("kind") or "single"),
        prompt=str(record.get("prompt") or ""),
        options=[o for o in record.get("options", []) if isinstance(o, dict)],
        questions=[q for q in record.get("questions", []) if isinstance(q, dict)],
    )


# ---------------------------------------------------------------------------
# Answering: replay a tap/typed answer as calibrated herdr send-keys
# ---------------------------------------------------------------------------

_STEP_SLEEP = 0.25  # let the TUI repaint between keystroke groups
_MARKERS = ("✅ ", "▫️ ", "☑️ ", "✔️ ")


def _strip_marker(text: str) -> str:
    out = str(text or "").strip()
    for marker in _MARKERS:
        if out.startswith(marker):
            return out[len(marker):].strip()
    return out


def _herdr_send_keys(pane_id: str, keys: list[str]) -> bool:
    try:
        out = subprocess.run([_herdr_bin(), "pane", "send-keys", pane_id, *keys],
                             capture_output=True, text=True, timeout=10, check=False)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _herdr_send_text(pane_id: str, text: str) -> bool:
    try:
        out = subprocess.run([_herdr_bin(), "pane", "send-text", pane_id, text],
                             capture_output=True, text=True, timeout=10, check=False)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _sleep(seconds: float) -> None:
    # time.sleep is unavailable under the workflow sandbox but always fine in the live connector;
    # guard so unit tests that monkeypatch the herdr calls don't pay the latency.
    import time as _time

    _time.sleep(seconds)


def _num_real_options(options: list[dict[str, Any]]) -> int:
    return len([o for o in options if str(o.get("id")) != CUSTOM_OPTION_ID])


def _option_position(options: list[dict[str, Any]], option: dict[str, Any]) -> int:
    """The 1-based TUI row a digit jumps to. Single-select option ids ARE their position ("1".."N");
    plan ids (approve/revise) map to their list index."""
    oid = str(option.get("id") or "")
    if oid.isdigit():
        return int(oid)
    for index, opt in enumerate(options):
        if opt is option or str(opt.get("id")) == oid:
            return index + 1
    return 1


def _match_option(options: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    want = _strip_marker(text).casefold()
    for opt in options:
        label = _strip_marker(str(opt.get("label") or "")).casefold()
        if label and label == want:
            return opt
    return None


def answer_single(pane_id: str, position: int) -> bool:
    """Single-select: a digit jumps the cursor to absolute row N, Enter selects+submits."""
    return _herdr_send_keys(pane_id, [str(position), "Enter"])


def answer_freeform(pane_id: str, num_real_options: int, answer: str) -> bool:
    """Write-in: jump to the "Type something" row (immediately after the real options), type the
    answer (which replaces the row label), then Enter submits it."""
    ok = _herdr_send_keys(pane_id, [str(num_real_options + 1)])
    _sleep(_STEP_SLEEP)
    ok = _herdr_send_text(pane_id, answer) and ok
    _sleep(_STEP_SLEEP)
    return _herdr_send_keys(pane_id, ["Enter"]) and ok


def answer_multi(pane_id: str, positions: list[int]) -> bool:
    """Multi-select (single question): cursor starts at row 1; for each selected row (asc) move Down
    the delta then Enter to toggle; then Right to the Submit tab and Enter to submit."""
    current = 1
    ok = True
    for pos in sorted(set(positions)):
        delta = pos - current
        if delta > 0:
            ok = _herdr_send_keys(pane_id, ["Down"] * delta) and ok
            _sleep(_STEP_SLEEP)
        elif delta < 0:
            ok = _herdr_send_keys(pane_id, ["Up"] * (-delta)) and ok
            _sleep(_STEP_SLEEP)
        ok = _herdr_send_keys(pane_id, ["Enter"]) and ok  # toggle current
        _sleep(_STEP_SLEEP)
        current = pos
    ok = _herdr_send_keys(pane_id, ["Right"]) and ok  # to the Submit tab
    _sleep(_STEP_SLEEP)
    return _herdr_send_keys(pane_id, ["Enter"]) and ok


def _multi_option_lookup(questions: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """{ casefolded label : (option_id 'q1:2', 1-based position within its question) }."""
    lookup: dict[str, tuple[str, int]] = {}
    for q in questions:
        for pos, opt in enumerate(q.get("options", []), start=1):
            if not isinstance(opt, dict):
                continue
            label = _strip_marker(str(opt.get("label") or "")).casefold()
            if label:
                lookup[label] = (f"{q.get('question_id')}:{opt.get('option_id')}", pos)
    return lookup


def handle_decision_answer(store: dict[str, Any], topic_id: str, text: str) -> dict[str, Any] | None:
    """If ``topic_id`` has an active decision, replay ``text`` as the answer and return a reply dict
    (with a keyboard-removal reply_markup on completion). Returns None when there is no active
    decision, so command_reply falls through to normal instruction handling.

    A pending decision BLOCKS the pane, so while one is active every message in the topic answers it:
    a matching button label selects that option; any other text is sent as a write-in."""
    record = get_active(store, topic_id)
    if not record:
        return None
    text = str(text or "").strip()
    pane_id = str(record.get("pane_id") or "")
    kind = str(record.get("kind") or "single")
    options = [o for o in record.get("options", []) if isinstance(o, dict)]

    if kind == "multi":
        return _handle_multi_answer(store, topic_id, record, text)

    opt = _match_option(options, text)
    # Tapped (or typed) the "write a different answer" row -> collect the write-in next.
    if opt is not None and str(opt.get("id")) == CUSTOM_OPTION_ID:
        record["await_freeform"] = True
        set_active(store, topic_id, record)
        return {"handled": True, "reply": "✍️ Type your answer as a normal message and I'll send it to Claude."}
    if opt is not None:
        answer_single(pane_id, _option_position(options, opt))
        clear_active(store, topic_id)
        return {"handled": True, "reply": f"✅ Sent “{_strip_marker(str(opt.get('label')))}” to Claude.",
                "reply_markup": remove_keyboard()}
    if kind == "plan":
        # No free-text answer for a plan gate — nudge back to the two buttons.
        return {"handled": True, "reply": "Tap Approve or Revise to answer the plan."}
    # Free text on a single-select -> submit it as a write-in.
    answer_freeform(pane_id, _num_real_options(options), text)
    clear_active(store, topic_id)
    return {"handled": True, "reply": f"✍️ Sent your answer to Claude: “{sanitize_text(text, 200)}”.",
            "reply_markup": remove_keyboard()}


def _handle_multi_answer(store: dict[str, Any], topic_id: str, record: dict[str, Any], text: str) -> dict[str, Any]:
    questions = [q for q in record.get("questions", []) if isinstance(q, dict)]
    selected: list[str] = [str(s) for s in record.get("selected", [])]
    if _strip_marker(text).casefold() == _strip_marker(SUBMIT_LABEL).casefold():
        lookup = {oid: pos for _, (oid, pos) in _multi_option_lookup(questions).items()}
        positions = [pos for oid, pos in lookup.items() if oid in selected]
        answer_multi(str(record.get("pane_id") or ""), positions)
        clear_active(store, topic_id)
        picked = ", ".join(s.split(":", 1)[-1] for s in selected) or "nothing"
        return {"handled": True, "reply": f"✅ Submitted your selections to Claude ({len(selected)} chosen).",
                "reply_markup": remove_keyboard()}
    lookup = _multi_option_lookup(questions)
    hit = lookup.get(_strip_marker(text).casefold())
    if hit is None:
        return {"handled": True, "reply": "Tap an option to toggle it, then tap Submit."}
    oid = hit[0]
    if oid in selected:
        selected.remove(oid)
    else:
        selected.append(oid)
    record["selected"] = selected
    set_active(store, topic_id, record)
    decision = decision_from_record(record, topic_id)
    return {"handled": True, "reply": render_decision_html(decision, selected),
            "reply_markup": reply_keyboard(decision, selected), "reply_html": True}


def active_record_from(decision: ResolvedDecision, message_id: str) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "session_id": decision.session_id,
        "pane_id": decision.pane_id,
        "kind": decision.kind,
        "prompt": decision.prompt,
        "options": decision.options,
        "questions": decision.questions,
        "message_id": str(message_id),
        "selected": [],
        "await_freeform": False,
        "content_hash": decision.content_hash(),
    }
