"""Telegram bot for storygen (a separate bot from clipgen's, with its own token).

Plain Bot API long polling via requests. One worker thread makes the clips one at a time; the polling
loop keeps answering meanwhile. Pending jobs and the update offset live in work/telegram_state.json,
so a restart resumes where it stopped.

Chat usage:
  story                    -> one story clip, random genre and background
  n3                       -> 3 clips (nX = X clips, 1-10)
  scene1 / scene2          -> background: scene1 = layers (each touched ring shatters),
                              scene2 = escape (rings with a rotating opening); default random
  any other text           -> used as the story's theme, e.g. "a babysitter and a locked attic n2"
  /redo or 🔁 button       -> another story with the same settings
  /stop                    -> cancel the running job and drop the queue
  /status                  -> queue

Access: allowed users from the config, or the owner who claims the bot once with a pairing code that
is only printed in the terminal/log. Everyone else is ignored silently; only private chats are served.
"""
from __future__ import annotations

import html
import os
import queue
import re
import secrets
import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path

import requests

from .config import Config
from .utils import ffmpeg, log, media_info, read_json, write_json

COUNT_RE = re.compile(r"(?<!\w)n(\d{1,2})(?!\w)", re.I)
SCENE_RE = re.compile(r"(?<!\w)scene\s?([12])(?!\w)", re.I)
SCENES = {1: "layers", 2: "escape"}
NO_THEME = {"", "story", "/story", "stories", "go", "new", "another", "one", "please", "a story"}
HELP = (
    "I make story-time shorts from scratch: a made-up story written to keep people watching, a voice-over "
    "with word-by-word subtitles, and a satisfying ball-and-rings animation behind it.\n\n"
    "• <code>story</code> (or anything) → one clip, random genre.\n"
    "• Any text is used as the theme, e.g. <code>a babysitter finds a second staircase</code>.\n"
    "• <code>nX</code> sets the number of clips (1-10), e.g. <code>story n3</code>.\n"
    "• <code>scene1</code> = rings shatter when the ball touches them, <code>scene2</code> = the ball escapes "
    "through openings in spinning rings. Default: random.\n"
    "• /redo (or the 🔁 button) makes another story with the same settings.\n"
    "• /stop cancels the running job and drops the queue. /status shows the queue."
)


class TelegramError(RuntimeError):
    pass


class JobCancelled(BaseException):
    """Raised inside a running job by /stop (a BaseException so `except Exception` blocks let it through)."""


def parse_request(text: str) -> dict:
    """{'count': int|None, 'scene': 'layers'|'escape'|None, 'theme': str|None} from a chat message."""
    text = (text or "").strip()
    m = COUNT_RE.search(text)
    count = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 10 else None
    s = SCENE_RE.search(text)
    scene = SCENES[int(s.group(1))] if s else None
    rest = SCENE_RE.sub(" ", COUNT_RE.sub(" ", text))
    rest = re.sub(r"^/(story|new)(@\w+)?\b", " ", rest.strip(), flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" .,!?")
    theme = None if rest.lower() in NO_THEME else rest[:300]
    return {"count": count, "scene": scene, "theme": theme}


def clip_caption(meta: dict, index: int, of: int, limit: int = 1024) -> str:
    """Telegram caption: the YouTube title and description ready to copy, then the clip facts."""
    e = lambda x: html.escape(str(x), quote=False)  # noqa: E731
    title = meta.get("youtube_title") or meta["title"]
    head = f"<b>{e(title)}</b>" + (f"  <i>({index}/{of})</i>" if of > 1 else "")
    info = f"🎨 {meta['style']} · 🎙 {meta['voice']} · {meta['duration']:.0f} s"
    if meta.get("music"):
        info += f" · 🎵 {Path(meta['music']).stem}"
    if meta.get("score") is not None:
        info += f" · retention score {meta['score']}/10"
    desc = meta.get("description")
    if desc is None:                          # metadata from before the YouTube fields
        desc = "\n\n".join(x for x in (meta.get("caption"), " ".join(meta.get("hashtags") or [])) if x)

    def build(d: str) -> str:
        return "\n\n".join(x for x in (head, f"<code>{e(d)}</code>" if d else "", info) if x)

    text = build(desc)
    while len(text) > limit and desc:
        desc = desc[: max(0, len(desc) - (len(text) - limit) - 2)].rstrip()
        text = build(desc + "…" if desc else "")
    return text[:limit]


def script_message(meta: dict, limit: int = 4000) -> str:
    body = html.escape(meta.get("script") or "", quote=False)[: limit - 120]
    return f"📜 <b>Script</b> ({meta.get('words')} words)\n<blockquote expandable>{body}</blockquote>"


class Bot:
    def __init__(self, cfg: Config, token: str):
        self.cfg, self.tg = cfg, cfg.telegram
        self._token = token
        self.base = f"{self.tg.api_url.rstrip('/')}/bot{token}"
        self._local = threading.local()
        self.state_f = cfg.path(cfg.work_dir) / "telegram_state.json"
        self.state = read_json(self.state_f) if self.state_f.exists() else {}
        for k, v in (("offset", 0), ("owners", []), ("pending", []), ("history", [])):
            self.state.setdefault(k, v)
        self.lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self.current: dict | None = None
        self.cancel = threading.Event()

    # ------------------------------------------------------------------ Bot API plumbing
    @property
    def http(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            self._local.s = requests.Session()
        return self._local.s

    def api(self, method: str, files=None, http_timeout: float = 60, **params):
        for attempt in range(5):
            try:
                r = self.http.post(f"{self.base}/{method}", data=params if files else None,
                                   json=None if files else params, files=files, timeout=http_timeout)
            except requests.RequestException as e:
                if attempt == 4:
                    raise TelegramError(f"{method}: {self._mask(e)}") from None
                time.sleep(2 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                raise TelegramError(f"{method}: HTTP {r.status_code}") from None
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry:
                time.sleep(retry + 1)
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        raise TelegramError(f"{method}: too many retries")

    def _mask(self, e) -> str:
        return str(e).replace(self._token, "<token>")

    def send(self, chat_id: int, text: str, reply_to: int | None = None, **extra) -> dict | None:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}, **extra}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.api("sendMessage", **params)
        except TelegramError as e:
            log.warning("Telegram send failed: %s", e)
            return None

    def edit(self, chat_id: int, message_id: int, text: str, **extra) -> None:
        try:
            self.api("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                     parse_mode="HTML", link_preview_options={"is_disabled": True}, **extra)
        except TelegramError as e:
            if "not modified" not in str(e):
                log.debug("Telegram edit failed: %s", e)

    def save_state(self) -> None:
        with self.lock:
            write_json(self.state_f, self.state)

    # ------------------------------------------------------------------ access control
    def authorized(self, user: dict) -> bool:
        allowed = {str(a).lstrip("@").lower() for a in self.tg.allowed_users}
        if allowed:
            return str(user.get("id")) in allowed or (user.get("username") or "").lower() in allowed
        return user.get("id") in self.state["owners"]

    def pairing_open(self) -> bool:
        return not self.tg.allowed_users and not self.state["owners"]

    def pairing_code(self) -> str:
        if not self.state.get("pair_code"):
            self.state["pair_code"] = secrets.token_hex(4)
            self.save_state()
        return self.state["pair_code"]

    def try_pair(self, user: dict, code: str) -> bool:
        if not self.pairing_open() or not code or not secrets.compare_digest(code, self.pairing_code()):
            return False
        self.state["owners"] = [user["id"]]
        self.state.pop("pair_code", None)
        self.save_state()
        log.info("Telegram: paired with owner id %s", user["id"])
        return True

    # ------------------------------------------------------------------ incoming
    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        text = msg.get("text") or ""
        if msg["chat"].get("type", "private") != "private":
            return
        if text.startswith("/start") and self.pairing_open():
            code = text.split(maxsplit=1)[1].strip() if " " in text else ""
            if self.try_pair(user, code):
                self.send(chat_id, "✅ You are now the only user of this bot.\nTo make that permanent put\n"
                                   f"<code>allowed_users = [{user['id']}]</code>\nunder [telegram] in storygen.toml."
                                   "\n\n" + HELP)
            return
        if not self.authorized(user):
            log.info("Telegram: ignored user id %s", user.get("id"))
            return
        if text.startswith(("/start", "/help")):
            self.send(chat_id, HELP)
            return
        if text.startswith("/status"):
            self.send(chat_id, self.status_text())
            return
        if text.split(maxsplit=1)[:1] == ["/stop"]:
            self.stop(chat_id, msg["message_id"])
            return
        if text.startswith(("/redo", "/again")):
            prev = self.last_job(chat_id)
            if prev is None:
                self.send(chat_id, "Nothing to redo yet. Send <code>story</code> first.", reply_to=msg["message_id"])
                return
            req = parse_request(text[5:] if text.startswith("/redo") else text[6:])
            self.enqueue(chat_id, {"count": req["count"] or prev.get("count"),
                                   "scene": req["scene"] or prev.get("scene"),
                                   "theme": req["theme"] or prev.get("theme")}, msg["message_id"])
            return
        if not text:
            self.send(chat_id, "Send <code>story</code> or a theme. /help for details.", reply_to=msg["message_id"])
            return
        self.enqueue(chat_id, parse_request(text), msg["message_id"])

    def handle_callback(self, cq: dict) -> None:
        user = cq.get("from") or {}
        if not self.authorized(user):
            return
        try:
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Writing another story…")
        except TelegramError:
            pass
        data = cq.get("data") or ""
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id", user.get("id"))
        if data.startswith("redo:"):
            prev = next((h for h in reversed(self.state["history"]) if h["id"] == data[5:]), None) \
                or self.last_job(chat_id)
            if prev:
                self.enqueue(chat_id, {k: prev.get(k) for k in ("count", "scene", "theme")},
                             (cq.get("message") or {}).get("message_id"))

    def last_job(self, chat_id: int) -> dict | None:
        hist = [h for h in self.state["history"] if h["chat_id"] == chat_id]
        return hist[-1] if hist else None

    def enqueue(self, chat_id: int, req: dict, reply_to: int | None) -> None:
        job = {"id": secrets.token_hex(4), "chat_id": chat_id, "reply_to": reply_to, "queued_at": time.time(),
               "count": req.get("count"), "scene": req.get("scene"), "theme": req.get("theme")}
        ahead = self.jobs.qsize() + (1 if self.current else 0)
        st = self.send(chat_id, f"⏳ Queued: {html.escape(self._label(job))}"
                       + (f" ({ahead} job(s) ahead)" if ahead else ""), reply_to=reply_to)
        job["status_id"] = st["message_id"] if st else None
        with self.lock:
            self.state["pending"].append(job)
        self.save_state()
        self.jobs.put(job)

    def stop(self, chat_id: int, reply_to: int | None = None) -> None:
        dropped = 0
        while True:
            try:
                self.jobs.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        with self.lock:
            running = self.current is not None
            if running:
                self.cancel.set()
            self.state["pending"] = [j for j in self.state["pending"] if j is self.current]
        self.save_state()
        if running:
            text = "⏹ Stopping the running job (it stops at its next step)." + \
                (f" Dropped {dropped} queued job(s)." if dropped else "")
        else:
            text = f"⏹ Dropped {dropped} queued job(s)." if dropped else "Nothing to stop: the bot is idle."
        self.send(chat_id, text, reply_to=reply_to)

    def status_text(self) -> str:
        lines = [f"▶️ Working on: {html.escape(self._label(self.current))}"] if self.current else []
        with self.lock:
            waiting = [j for j in self.state["pending"] if j is not self.current]
        lines += [f"{k}. {html.escape(self._label(j))}" for k, j in enumerate(waiting, 1)]
        return "\n".join(lines) or "Idle. Send <code>story</code>."

    def _label(self, job: dict) -> str:
        n = job.get("count") or self.cfg.count
        s = f"{n} story clip{'s' if n != 1 else ''}"
        if job.get("theme"):
            s += f" about “{job['theme'][:80]}”"
        if job.get("scene"):
            s += f" ({job['scene']} background)"
        return s

    # ------------------------------------------------------------------ work
    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            with self.lock:
                self.current = job
            try:
                self.process(job)
            except JobCancelled:
                self.status(job, "⏹ Stopped.")
            except Exception as e:  # noqa: BLE001
                log.error("Telegram job failed: %s", e)
                log.debug(traceback.format_exc())
                self.status(job, f"❌ Failed: {html.escape(str(e))[:3000]}")
            finally:
                with self.lock:
                    self.current = None
                    self.cancel.clear()
                    self.state["pending"] = [j for j in self.state["pending"] if j.get("id") != job.get("id")]
                self.save_state()

    def status(self, job: dict, text: str, **extra) -> None:
        if job.get("status_id"):
            self.edit(job["chat_id"], job["status_id"], text, **extra)
        else:
            st = self.send(job["chat_id"], text, reply_to=job.get("reply_to"), **extra)
            job["status_id"] = st["message_id"] if st else None

    def progress_hook(self, job: dict, t0: float, index: int, of: int):
        last = [0.0]

        def progress(msg: str) -> None:
            if self.cancel.is_set():
                raise JobCancelled()
            now = time.time()
            if now - last[0] < 3:
                return
            last[0] = now
            part = f"{index}/{of} · " if of > 1 else ""
            self.status(job, f"⚙️ {part}{html.escape(msg)}… ({(now - t0) / 60:.0f} min)")
        return progress

    def process(self, job: dict) -> None:
        from .pipeline import make_story

        cfg = deepcopy(self.cfg)
        n = job.get("count") or cfg.count
        t0 = time.time()
        sent = 0
        for i in range(1, n + 1):
            meta = make_story(cfg, theme=job.get("theme"), style=job.get("scene"),
                              progress=self.progress_hook(job, t0, i, n))
            if self.cancel.is_set():
                raise JobCancelled()
            try:
                r = self.send_clip(job["chat_id"], meta, i, n, job.get("reply_to"))
                if r:
                    sent += 1
                    self.send(job["chat_id"], script_message(meta), reply_to=r.get("message_id"))
            except Exception as e:  # noqa: BLE001
                log.error("Sending story clip failed: %s", e)
                self.send(job["chat_id"], f"⚠️ Clip {i} could not be sent: {html.escape(str(e))[:500]}")
        with self.lock:
            self.state["history"].append({k: job.get(k) for k in ("id", "chat_id", "count", "scene", "theme")}
                                         | {"done_at": time.time()})
            self.state["history"] = self.state["history"][-50:]
        self.save_state()
        button = {"inline_keyboard": [[{"text": "🔁 Another story", "callback_data": f"redo:{job['id']}"}]]}
        self.status(job, f"✅ Done: {sent}/{n} clip(s) in {(time.time() - t0) / 60:.1f} min.", reply_markup=button)

    def send_clip(self, chat_id: int, meta: dict, index: int, of: int, reply_to: int | None) -> dict | None:
        original = Path(meta["video"])
        video = self.fit_size(original)
        info = media_info(video)
        params = {"chat_id": chat_id, "caption": clip_caption(meta, index, of), "parse_mode": "HTML",
                  "supports_streaming": "true", "width": info.get("width") or 1080,
                  "height": info.get("height") or 1920, "duration": int(round(info["duration"]))}
        if reply_to:
            params["reply_parameters"] = '{"message_id": %d, "allow_sending_without_reply": true}' % reply_to
        thumb = Path(meta.get("thumbnail") or "")
        with open(video, "rb") as vf:
            files = {"video": (original.name, vf, "video/mp4")}      # the clip's name, never ".tg.mp4"
            th = None
            if thumb.is_file():
                th = thumb.with_name(thumb.stem + ".tg.jpg")
                if not th.exists():
                    ffmpeg(["-i", str(thumb), "-vf", "scale=-2:320", "-q:v", "5", str(th)])
                files["thumbnail"] = (th.name, open(th, "rb"), "image/jpeg")
            try:
                return self.api("sendVideo", files=files, http_timeout=600, **params)
            finally:
                if th:
                    files["thumbnail"][1].close()

    def fit_size(self, video: Path) -> Path:
        limit = self.tg.max_upload_mb * 1024 * 1024
        if video.stat().st_size <= limit:
            return video
        out = video.with_name(video.stem + ".tg.mp4")
        dur = media_info(video)["duration"]
        kbps = int(limit * 8 * 0.92 / dur / 1000) - 128
        ffmpeg(["-i", str(video), "-c:v", "libx264", "-preset", "medium", "-b:v", f"{kbps}k",
                "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
        return out

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        me = self.api("getMe")
        log.info("Telegram bot @%s is running. Ctrl+C to stop.", me.get("username"))
        if self.pairing_open():
            code = self.pairing_code()
            log.info("No owner yet. To claim the bot, open this link in Telegram and press Start:\n"
                     "      https://t.me/%s?start=%s\n  (or send: /start %s)", me.get("username"), code, code)
        try:
            self.api("setMyCommands", commands=[
                {"command": "redo", "description": "another story with the same settings"},
                {"command": "status", "description": "show the queue"},
                {"command": "stop", "description": "cancel the running job and the queue"},
                {"command": "help", "description": "how to use the bot"}])
        except TelegramError as e:
            log.debug("setMyCommands failed: %s", e)
        for job in self.state["pending"]:
            self.jobs.put(job)
            self.send(job["chat_id"], f"🔁 Bot restarted, resuming: {html.escape(self._label(job))}")
        threading.Thread(target=self.worker, daemon=True, name="storygen-worker").start()
        while True:
            try:
                updates = self.api("getUpdates", http_timeout=self.tg.poll_timeout + 15,
                                   offset=self.state["offset"], timeout=self.tg.poll_timeout,
                                   allowed_updates=["message", "callback_query"])
            except TelegramError as e:
                log.warning("Telegram polling error: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                self.state["offset"] = u["update_id"] + 1
                msg, cq = u.get("message"), u.get("callback_query")
                try:
                    if msg:
                        self.handle(msg)
                    elif cq:
                        self.handle_callback(cq)
                except Exception as e:  # noqa: BLE001
                    log.error("Telegram: failed to handle an update: %s", e)
            if updates:
                self.save_state()


def resolve_token(cfg: Config) -> str:
    tok = os.environ.get("STORYGEN_BOT_TOKEN") or cfg.telegram.token
    if not tok:
        f = cfg.path("secrets/telegram_token")
        tok = f.read_text().strip() if f.exists() else ""
    if not tok:
        raise TelegramError("No bot token: create a NEW bot with @BotFather (not the clipgen one), then set "
                            "STORYGEN_BOT_TOKEN, [telegram].token in storygen.toml, or put it in "
                            "secrets/telegram_token.")
    return tok
