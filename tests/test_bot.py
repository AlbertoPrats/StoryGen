"""Telegram bot against a fake Bot API (no network): access, queue, delivery, /redo, /stop."""
from __future__ import annotations

import pytest

from storygen import telegram as tg
from storygen.config import Config


class FakeBot(tg.Bot):
    def __init__(self, cfg):
        super().__init__(cfg, "123:SECRET")
        self.calls = []
        self.next_id = 100

    def api(self, method, files=None, http_timeout=60, **params):
        self.calls.append((method, params))
        self.next_id += 1
        return {"message_id": self.next_id}


def msg(text, user=7, mid=1):
    return {"message_id": mid, "chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    cfg = Config(work_dir=str(tmp_path))
    cfg.telegram.allowed_users = [7]
    b = FakeBot(cfg)
    made = []

    def fake_make(cfg, theme=None, style=None, progress=None, **kw):
        progress("Writing the story")
        made.append({"theme": theme, "style": style})
        video = tmp_path / f"v{len(made)}.mp4"
        video.write_bytes(b"x")
        return {"video": str(video), "thumbnail": "", "title": "T <b>", "caption": "c", "hashtags": ["#storytime"],
                "style": style or "layers", "voice": "am_michael", "duration": 51.2, "score": 9,
                "script": "Line one.", "words": 2}

    monkeypatch.setattr("storygen.pipeline.make_story", fake_make)
    monkeypatch.setattr(tg, "media_info", lambda p: {"width": 1080, "height": 1920, "duration": 51.2})
    b.made = made
    return b


def run_queue(b):
    while not b.jobs.empty():
        job = b.jobs.get()
        b.current = job
        try:
            b.process(job)
        finally:
            b.current = None
            b.state["pending"] = [j for j in b.state["pending"] if j["id"] != job["id"]]


def test_strangers_are_ignored(bot):
    bot.handle(msg("story", user=99))
    assert bot.calls == [] and bot.jobs.empty()


def test_story_request_is_queued_made_and_delivered(bot):
    bot.handle(msg("a locked attic n2 scene2"))
    job = bot.jobs.queue[0]
    assert (job["count"], job["scene"], job["theme"]) == (2, "escape", "a locked attic")
    run_queue(bot)
    assert bot.made == [{"theme": "a locked attic", "style": "escape"}] * 2
    videos = [p for m, p in bot.calls if m == "sendVideo"]
    assert len(videos) == 2 and "T &lt;b&gt;" in videos[0]["caption"] and "(1/2)" in videos[0]["caption"]
    assert "retention score 9/10" in videos[0]["caption"]
    scripts = [p for m, p in bot.calls if m == "sendMessage" and "Script" in p["text"]]
    assert len(scripts) == 2 and "<blockquote expandable>" in scripts[0]["text"]
    done = [p for m, p in bot.calls if m == "editMessageText"][-1]
    assert "Done: 2/2" in done["text"] and done["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("redo:")
    assert bot.state["history"][-1]["theme"] == "a locked attic"


def test_redo_repeats_the_settings_and_the_button_works(bot):
    bot.handle(msg("haunted elevator scene1"))
    run_queue(bot)
    bot.handle(msg("/redo n2"))
    job = bot.jobs.queue[0]
    assert (job["count"], job["scene"], job["theme"]) == (2, "layers", "haunted elevator")
    run_queue(bot)
    jid = bot.state["history"][-1]["id"]
    bot.handle_callback({"id": "q", "from": {"id": 7}, "data": f"redo:{jid}",
                         "message": {"message_id": 5, "chat": {"id": 7}}})
    assert bot.jobs.queue[0]["theme"] == "haunted elevator"


def test_stop_cancels_the_running_job(bot):
    bot.handle(msg("story n3"))
    bot.handle(msg("story"))
    job = bot.jobs.get()
    bot.current = job
    bot.stop(7)
    assert bot.cancel.is_set() and bot.jobs.empty()
    with pytest.raises(tg.JobCancelled):
        bot.process(job)


def test_token_comes_from_its_own_variable(monkeypatch, tmp_path):
    cfg = Config(work_dir=str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "clipgen-token")
    monkeypatch.delenv("STORYGEN_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cfg, "path", lambda p: tmp_path / p)
    with pytest.raises(tg.TelegramError):
        tg.resolve_token(cfg)                    # never picks up clipgen's bot token
    monkeypatch.setenv("STORYGEN_BOT_TOKEN", "story-token")
    assert tg.resolve_token(cfg) == "story-token"
