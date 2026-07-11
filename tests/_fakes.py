"""Test doubles for the herdres_connector source-mode sync loop.

Ported from the upstream herdres test suite (tests/test_source_only.py) — the bridge connector is a
downstream copy whose state.py / telegram_delivery.py / tendwire_client.py are byte-identical, so the
same fakes drive it. FakeTelegram records every side-effecting call (create/rename/delete topic, sends,
pins) for assertions; FakeTendwire returns canned snapshot/turns/pending payloads.
"""
from __future__ import annotations

import json


class FakeTendwire:
    def __init__(self, *, turns=None, pending=None, workers=None, spaces=None):
        self.commands = []
        self._turns = turns if turns is not None else {"turns": []}
        self._pending = pending if pending is not None else {"pending_interactions": []}
        self._workers = workers if workers is not None else []
        self._spaces = spaces if spaces is not None else []

    def snapshot(self):
        return {"ok": True, "spaces": self._spaces, "workers": self._workers}

    def turns(self):
        return self._turns

    def pending(self):
        return self._pending

    def connector_poll(self, **_kwargs):
        return {"ok": True, "items": []}

    def command(self, request):
        self.commands.append(request)
        return {"ok": True, "status": "accepted", "result": {"delivery_state": "submitted"}}


class FakeTelegram:
    dry_run = False

    def __init__(self, token="fake", shared=None):
        self.token = token
        shared = shared or {
            "sent": [], "edited": [], "topics": [], "deleted_topics": [],
            "renamed_topics": [], "pins": [], "api_calls": [], "icon_edits": [],
        }
        shared.setdefault("renamed_topics", [])
        self._shared = shared
        self.sent = shared["sent"]
        self.edited = shared["edited"]
        self.topics = shared["topics"]
        self.deleted_topics = shared["deleted_topics"]
        self.renamed_topics = shared["renamed_topics"]
        self.pins = shared["pins"]
        self.api_calls = shared["api_calls"]
        self.icon_edits = shared["icon_edits"]

    def with_token(self, token):
        return FakeTelegram(token=token, shared=self._shared)

    def api(self, method, payload):
        self.api_calls.append((method, dict(payload), self.token))
        if method == "getForumTopicIconStickers":
            return {"ok": True, "result": [
                {"emoji": "⚡️", "custom_emoji_id": "icon-working"},
                {"emoji": "✅", "custom_emoji_id": "icon-idle"},
                {"emoji": "❓", "custom_emoji_id": "icon-attention"},
                {"emoji": "‼️", "custom_emoji_id": "icon-failed"},
                {"emoji": "\U0001f98a", "custom_emoji_id": "icon-fox"},
            ]}
        if method == "sendRichMessage":
            message_id = str(100 + len(self.sent))
            rich = json.loads(payload.get("rich_message") or "{}")
            kwargs = {"thread_id": str(payload.get("message_thread_id") or ""), "format": "rich", "token": self.token}
            self.sent.append((str(payload.get("chat_id") or ""), str(rich.get("html") or ""), kwargs, message_id))
            return {"ok": True, "result": {"message_id": message_id}}
        if method == "editMessageText":
            rich_payload = payload.get("rich_message")
            rich = json.loads(rich_payload) if rich_payload else {}
            html = str(rich.get("html") or payload.get("text") or "")
            self.edited.append((str(payload.get("chat_id") or ""), str(payload.get("message_id") or ""), html))
            return {"ok": True, "result": {"message_id": str(payload.get("message_id") or "0")}}
        return {"ok": True, "result": {"message_id": 0}}

    def create_topic(self, _chat_id, name, icon_color=None):
        self.topics.append(name)
        return {"ok": True, "topic_id": str(76 + len(self.topics))}

    def rename_topic(self, chat_id, thread_id, name):
        self.renamed_topics.append((str(chat_id), str(thread_id), str(name)))
        return {"ok": True}

    def edit_topic_icon(self, chat_id, thread_id, emoji_id):
        self.icon_edits.append((str(chat_id), str(thread_id), str(emoji_id)))
        return {"ok": True}

    def delete_topic(self, _chat_id, thread_id):
        self.deleted_topics.append(str(thread_id))
        return {"ok": True}

    def send_message(self, chat_id, html, **kwargs):
        message_id = str(100 + len(self.sent))
        payload_kwargs = dict(kwargs)
        payload_kwargs["token"] = self.token
        self.sent.append((chat_id, html, payload_kwargs, message_id))
        return {"ok": True, "message_id": message_id}

    def edit_message(self, chat_id, message_id, html):
        self.edited.append((chat_id, str(message_id), html))
        return {"ok": True, "message_id": str(message_id)}

    def pin_message(self, chat_id, message_id):
        self.pins.append((chat_id, str(message_id)))
        return {"ok": True}


def _store():
    return {
        "enabled": True,
        "telegram": {"chat_id": "-100", "general_thread_id": "1"},
        "panes": {},
        "spaces": {},
        "tendwired_bootstrap_complete": True,
    }
