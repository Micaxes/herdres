"""Tests for the remote-answer feature (herdres_connector/decisions.py + source_sync wiring).

The herdr boundaries (`api snapshot`, `pane send-keys/send-text`) are monkeypatched so the tests are
hermetic; the option-mapping goes through the real adapter (claude_decision_turn_fields) and the
stable-key join through the real state resolver.
"""
from __future__ import annotations

import pytest

from herdres_connector import decisions, source_sync, state
from herdres_connector.telegram_delivery import TelegramClient

from _fakes import FakeTelegram, FakeTendwire


# --------------------------------------------------------------------------- helpers

def _pending_record(name="AskUserQuestion", questions=None, session="sess1", tool_id="tid1", plan=None):
    inp: dict = {}
    if questions is not None:
        inp["questions"] = questions
    if plan is not None:
        inp["plan"] = plan
    return {"tool_use_id": tool_id, "name": name, "input": inp, "session_id": session, "ts": 9_999_999_999}


def _single_questions():
    return [{"question": "Deploy target?", "options": [{"label": "Staging"}, {"label": "Production"}]}]


def _multi_questions():
    return [{"question": "Pick fruits", "multiSelect": True,
             "options": [{"label": "Apple"}, {"label": "Banana"}, {"label": "Cherry"}]}]


def _store(stable_key="SK1", topic_id="500", session="sess1"):
    return {
        "panes": {
            "worker:claude:x": {
                "source": "tendwire", "entry_type": "worker", "status": "working",
                "topic_id": topic_id, "tendwire_stable_key": stable_key,
                "tendwire_worker_id": "claude", "agent": "claude",
            }
        },
        "spaces": {},
        "telegram": {"chat_id": "-100", "general_thread_id": "1"},
    }


def _patch_resolution(monkeypatch, *, record, session="sess1", pane="w1:p1", term="term_x", stable_key="SK1"):
    monkeypatch.setattr(decisions, "_read_pending_files", lambda: [record])
    monkeypatch.setattr(decisions, "_run_herdr_snapshot", lambda: {"panes": [
        {"pane_id": pane, "terminal_id": term, "workspace_id": "w1",
         "agent_status": "blocked", "agent_session": {"value": session}},
    ]})
    monkeypatch.setattr(decisions, "stable_key_candidates", lambda *a, **k: [stable_key])


@pytest.fixture
def sent_keys(monkeypatch):
    """Capture herdr send-keys / send-text instead of shelling out."""
    calls: list[tuple[str, str, list]] = []
    monkeypatch.setattr(decisions, "_herdr_send_keys", lambda pane, keys: calls.append(("keys", pane, list(keys))) or True)
    monkeypatch.setattr(decisions, "_herdr_send_text", lambda pane, text: calls.append(("text", pane, [text])) or True)
    monkeypatch.setattr(decisions, "_sleep", lambda *_a: None)
    return calls


# --------------------------------------------------------------------------- resolver join

def test_resolve_join_single(monkeypatch):
    _patch_resolution(monkeypatch, record=_pending_record(questions=_single_questions()))
    resolved = decisions.resolve_decisions(_store(), host_id="H")
    assert len(resolved) == 1
    d = resolved[0]
    assert d.topic_id == "500" and d.pane_id == "w1:p1" and d.kind == "single"
    assert d.decision_id == "tid1"
    assert [o["label"] for o in d.options] == ["Staging", "Production", decisions.FREEFORM_LABEL]


def test_resolve_skips_when_pane_absent(monkeypatch):
    monkeypatch.setattr(decisions, "_read_pending_files", lambda: [_pending_record(questions=_single_questions())])
    monkeypatch.setattr(decisions, "_run_herdr_snapshot", lambda: {"panes": []})  # session not tracked
    monkeypatch.setattr(decisions, "stable_key_candidates", lambda *a, **k: ["SK1"])
    assert decisions.resolve_decisions(_store(), host_id="H") == []


def test_resolve_skips_when_topic_unknown(monkeypatch):
    _patch_resolution(monkeypatch, record=_pending_record(questions=_single_questions()), stable_key="OTHER")
    # store entry has SK1, resolver computes OTHER -> no match -> no decision
    assert decisions.resolve_decisions(_store(stable_key="SK1"), host_id="H") == []


def test_resolve_matches_pane_id_key_when_terminal_key_misses(monkeypatch):
    """Regression (no-reply-keyboard bug): the topic was minted by tendwire keyed on pane_id (the
    turn-adapter view hides terminal_id), but the live `herdr api snapshot` exposes terminal_id, so
    the terminal_id candidate misses. The resolver must fall back to the pane_id candidate."""
    monkeypatch.setattr(decisions, "_read_pending_files", lambda: [_pending_record(questions=_single_questions())])
    monkeypatch.setattr(decisions, "_run_herdr_snapshot", lambda: {"panes": [
        {"pane_id": "w1:p1", "terminal_id": "term_x", "workspace_id": "w1",
         "agent_status": "blocked", "agent_session": {"value": "sess1"}},
    ]})
    # terminal_id hash misses (not in store); pane_id hash "SK1" is what the topic was keyed by.
    monkeypatch.setattr(decisions, "stable_key_candidates", lambda *a, **k: ["TERMINAL_MISS", "SK1"])
    resolved = decisions.resolve_decisions(_store(stable_key="SK1"), host_id="H")
    assert len(resolved) == 1 and resolved[0].topic_id == "500"


def test_stable_key_candidates_tries_terminal_then_pane():
    keys = decisions.stable_key_candidates("H", "term_x", "w1:p1", "w1")
    assert len(keys) == 2 and keys[0] != keys[1]                              # terminal-first, pane-fallback
    assert len(decisions.stable_key_candidates("H", "", "w1:p1", "w1")) == 1  # empty terminal dropped
    assert decisions.stable_key_candidates("", "term_x", "w1:p1", "w1") == []  # no host -> nothing


def test_resolve_multi(monkeypatch):
    _patch_resolution(monkeypatch, record=_pending_record(questions=_multi_questions()))
    resolved = decisions.resolve_decisions(_store(), host_id="H")
    assert len(resolved) == 1 and resolved[0].kind == "multi"
    q = resolved[0].questions[0]
    assert [o["label"] for o in q["options"]] == ["Apple", "Banana", "Cherry"]


# --------------------------------------------------------------------------- keyboard render

def test_reply_keyboard_single_has_options_and_freeform():
    d = decisions.ResolvedDecision(session_id="s", pane_id="p", topic_id="1", entry_key="e",
                                   decision_id="d", kind="single", prompt="q",
                                   options=[{"id": "1", "label": "A", "send_text": "A"},
                                            {"id": "2", "label": "B", "send_text": "B"},
                                            {"id": "custom", "label": decisions.FREEFORM_LABEL, "send_text": ""}])
    kb = decisions.reply_keyboard(d)
    texts = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert texts == ["A", "B", decisions.FREEFORM_LABEL]
    # selective MUST be False — a selective reply keyboard is hidden on standalone forum-topic posts
    # (no @mention / reply_to), which renders the text but no buttons. Regression guard for that bug.
    assert kb["one_time_keyboard"] is True and kb["selective"] is False


def test_reply_keyboard_multi_has_submit_and_markers():
    d = decisions.ResolvedDecision(session_id="s", pane_id="p", topic_id="1", entry_key="e",
                                   decision_id="d", kind="multi", prompt="q",
                                   questions=[{"question_id": "q1", "title": "Pick",
                                               "options": [{"option_id": "1", "label": "A"},
                                                           {"option_id": "2", "label": "B"}]}])
    kb = decisions.reply_keyboard(d, selected=["q1:1"])
    texts = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert decisions.SUBMIT_LABEL in texts
    assert any(t.endswith("A") and t.startswith("✅") for t in texts)  # selected marker
    assert kb["one_time_keyboard"] is False  # stays up until Submit
    assert kb["selective"] is False  # must render on standalone forum-topic posts


# --------------------------------------------------------------------------- answering

def _active_single(store, topic="500", pane="w1:p1"):
    decisions.set_active(store, topic, {
        "decision_id": "d", "session_id": "sess1", "pane_id": pane, "kind": "single",
        "prompt": "Deploy target?", "options": [
            {"id": "1", "label": "Staging", "send_text": "Staging"},
            {"id": "2", "label": "Production", "send_text": "Production"},
            {"id": "custom", "label": decisions.FREEFORM_LABEL, "send_text": ""}],
        "questions": [], "message_id": "10", "selected": [], "await_freeform": False, "content_hash": "h"})


def test_answer_none_when_no_active_decision():
    assert decisions.handle_decision_answer({"decisions": {"active": {}}}, "500", "hi") is None


def test_answer_single_sends_digit_enter(sent_keys):
    store = _store()
    _active_single(store)
    res = decisions.handle_decision_answer(store, "500", "Production")
    assert res["handled"] and res["reply_markup"] == decisions.remove_keyboard()
    assert sent_keys == [("keys", "w1:p1", ["2", "Enter"])]
    assert decisions.get_active(store, "500") is None  # cleared


def test_answer_freeform_writein(sent_keys):
    store = _store()
    _active_single(store)
    res = decisions.handle_decision_answer(store, "500", "Dragonfruit")  # matches no option
    # jump to "Type something" (row 3 = 2 real options + 1), type it, Enter
    assert sent_keys == [("keys", "w1:p1", ["3"]), ("text", "w1:p1", ["Dragonfruit"]), ("keys", "w1:p1", ["Enter"])]
    assert res["reply_markup"] == decisions.remove_keyboard()
    assert decisions.get_active(store, "500") is None


def test_answer_custom_button_then_writein(sent_keys):
    store = _store()
    _active_single(store)
    # tapping the freeform button arms capture, does NOT send keys yet
    res1 = decisions.handle_decision_answer(store, "500", decisions.FREEFORM_LABEL)
    assert sent_keys == [] and decisions.get_active(store, "500")["await_freeform"] is True
    # next message is the typed answer
    decisions.handle_decision_answer(store, "500", "my custom answer")
    assert ("text", "w1:p1", ["my custom answer"]) in sent_keys
    assert decisions.get_active(store, "500") is None


def test_answer_multi_toggle_then_submit(sent_keys):
    store = _store()
    decisions.set_active(store, "500", {
        "decision_id": "d", "session_id": "sess1", "pane_id": "w1:p9", "kind": "multi",
        "prompt": "Pick fruits", "options": [], "questions": [
            {"question_id": "q1", "title": "Pick fruits", "options": [
                {"option_id": "1", "label": "Apple"}, {"option_id": "2", "label": "Banana"},
                {"option_id": "3", "label": "Cherry"}]}],
        "message_id": "10", "selected": [], "await_freeform": False, "content_hash": "h"})
    decisions.handle_decision_answer(store, "500", "Apple")   # toggle pos 1
    decisions.handle_decision_answer(store, "500", "Cherry")  # toggle pos 3
    assert decisions.get_active(store, "500")["selected"] == ["q1:1", "q1:3"]
    decisions.handle_decision_answer(store, "500", decisions.SUBMIT_LABEL)
    # pos1: Enter (delta 0); pos3: Down Down, Enter; then Right, Enter
    assert sent_keys == [
        ("keys", "w1:p9", ["Enter"]),
        ("keys", "w1:p9", ["Down", "Down"]),
        ("keys", "w1:p9", ["Enter"]),
        ("keys", "w1:p9", ["Right"]),
        ("keys", "w1:p9", ["Enter"]),
    ]
    assert decisions.get_active(store, "500") is None


def test_answer_plan_approve_and_revise(sent_keys):
    for label, want in [("✅ Approve & proceed", "1"), ("✍️ Keep planning / revise", "2")]:
        store = _store()
        decisions.set_active(store, "500", {
            "decision_id": "d", "session_id": "sess1", "pane_id": "w1:p1", "kind": "plan",
            "prompt": "Approve this plan?", "options": [
                {"id": "approve", "label": "✅ Approve & proceed", "send_text": "1"},
                {"id": "revise", "label": "✍️ Keep planning / revise", "send_text": ""}],
            "questions": [], "message_id": "10", "selected": [], "await_freeform": False, "content_hash": "h"})
        sent_keys.clear()
        decisions.handle_decision_answer(store, "500", label)
        assert sent_keys == [("keys", "w1:p1", [want, "Enter"])]


# --------------------------------------------------------------------------- delivery + auto-disable

def _runtime(fake_tg):
    return source_sync.SyncRuntime(tendwire=FakeTendwire(), telegram=fake_tg, dry_run=False)


def test_deliver_posts_keyboard_and_is_idempotent(monkeypatch):
    _patch_resolution(monkeypatch, record=_pending_record(questions=_single_questions()))
    store = _store()
    fake = FakeTelegram()
    changed = source_sync._deliver_decisions(store, _runtime(fake), host_id="H", chat_id="-100")
    assert changed == 1 and len(fake.sent) == 1
    _chat, _html, kwargs, _mid = fake.sent[0]
    assert kwargs.get("reply_markup", {}).get("keyboard")  # keyboard attached
    assert decisions.get_active(store, "500") is not None
    # second pass: unchanged -> no new send
    changed2 = source_sync._deliver_decisions(store, _runtime(fake), host_id="H", chat_id="-100")
    assert changed2 == 0 and len(fake.sent) == 1


def test_auto_disable_removes_keyboard_when_file_gone(monkeypatch):
    store = _store()
    _active_single(store)  # active decision on topic 500
    # resolver returns nothing AND the pending file is gone -> auto-disable
    monkeypatch.setattr(decisions, "resolve_decisions", lambda *a, **k: [])
    monkeypatch.setattr(decisions, "pending_file_present", lambda *_a: False)
    fake = FakeTelegram()
    changed = source_sync._deliver_decisions(store, _runtime(fake), host_id="H", chat_id="-100")
    assert changed == 1 and decisions.get_active(store, "500") is None
    _chat, _html, kwargs, _mid = fake.sent[-1]
    assert kwargs.get("reply_markup") == decisions.remove_keyboard()


def test_auto_disable_keeps_keyboard_on_snapshot_hiccup(monkeypatch):
    store = _store()
    _active_single(store)
    monkeypatch.setattr(decisions, "resolve_decisions", lambda *a, **k: [])  # transient empty
    monkeypatch.setattr(decisions, "pending_file_present", lambda *_a: True)  # file still there
    fake = FakeTelegram()
    changed = source_sync._deliver_decisions(store, _runtime(fake), host_id="H", chat_id="-100")
    assert changed == 0 and decisions.get_active(store, "500") is not None
    assert fake.sent == []


# --------------------------------------------------------------------------- telegram reply_markup passthrough

def test_send_message_serializes_reply_markup(monkeypatch):
    captured: dict = {}

    def fake_api(self, method, payload):
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True, "result": {"message_id": 7}}

    monkeypatch.setattr(TelegramClient, "api", fake_api)
    client = TelegramClient(token="t")
    out = client.send_message("-100", "hi", thread_id="5", reply_markup={"remove_keyboard": True})
    assert out["ok"]
    import json as _json
    assert _json.loads(captured["payload"]["reply_markup"]) == {"remove_keyboard": True}
