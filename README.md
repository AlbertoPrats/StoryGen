# storygen

Story-time shorts made from scratch, fully unattended and on a free stack. Each clip has:

- **a made-up mystery / horror / tension story written to hold attention** (dark secrets, cheating partners,
  someone in the house, messages that should not exist), by Claude through headless `claude -p` (your Claude
  Pro plan; no API key). A writer pass drafts the story with a hook, an open loop, escalation and a twist that
  pays off the hook. A retention-editor pass then scores the draft, lists its problems and returns a
  tightened final script.
- **an expressive offline voice-over** by [Chatterbox Turbo](https://github.com/resemble-ai/chatterbox) (MIT),
  in its own environment (`.venv-tts`), one take per story beat. It copies a male or female voice from a short
  reference clip that [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) makes once. On a
  6-core CPU it runs at about 4x slower than real time (~6 min per clip). Kokoro alone (`[voice].engine =
  "kokoro"`, ~2x faster than real time, flatter) is the fallback. Long takes are time-stretched (≤ 1.15x) or
  shortened once by Claude. Chatterbox adds Resemble's inaudible watermark to its audio.
- **word-by-word subtitles**: the spoken word is highlighted, the timings come from faster-whisper aligned
  to the script, and the story title stays in a white box at the top.
- **a satisfying background animation**, rendered frame by frame: a ball bouncing under gravity inside
  layered neon rings that slowly close in. Each hit plays the next note of a minor-key melody over a low drone.
  - `layers`: every ring the ball touches shatters.
  - `escape`: the rings have rotating openings; the ball bounces off the solid parts and a ring shatters
    when the ball slips through its opening.

Output: 1080x1920 H.264 + AAC, at most 58 s, −14 LUFS, plus a thumbnail, the script and the post text
(caption + hashtags).

This is a sister project of clipgen (shorts cut from long
videos) and has its own Telegram bot.

## Install

```bash
./install.sh                # ffmpeg, venv, Chatterbox (.venv-tts, CPU torch), Kokoro, Anton font, Whisper base, Claude Code
                            # (NO_CHATTERBOX=1 ./install.sh skips Chatterbox; Kokoro is then used)
.venv/bin/storygen doctor   # checks everything, including the Claude login
```

## Use

```bash
.venv/bin/storygen make                                  # one clip, random genre and background
.venv/bin/storygen make -n 3 --style escape
.venv/bin/storygen make --theme "a babysitter finds a second staircase"
.venv/bin/storygen make --story work/stories/<id>/story.json   # re-voice / re-render a story, no Claude call
.venv/bin/storygen bot                                   # Telegram bot
.venv/bin/python -m pytest -q                            # tests (offline)
```

Clips land in `output/<timestamp>_<title>/` (`story.mp4`, `story.jpg`, `story.json`, `script.txt`).
Working files (story, voice, word timings, hit sounds, captions) are in `work/stories/<timestamp>/`.
Recent premises are kept in `work/story_state.json` and sent to Claude so it does not repeat itself.

## Telegram bot

Create a **new** bot with @BotFather (do not reuse the clipgen bot) and put its token in
`secrets/telegram_token`, in `[telegram].token` of `storygen.toml`, or in `STORYGEN_BOT_TOKEN`. Start
`storygen bot` and open the pairing link it prints, so that you become the bot's only user.

| Message | Result |
|---|---|
| `story` (or any message) | one clip, random dark genre |
| any other text | used as the theme, e.g. `a locked attic and a cat that won't go in` |
| `nX` | X clips (1-10), e.g. `story n3` |
| `scene1` / `scene2` | background: `layers` / `escape` (default random) |
| `/redo` or the 🔁 button | another story with the same settings |
| `/stop`, `/status`, `/help` | cancel the job and drop the queue / show the queue / help |

Each clip comes with its title, the editor's retention score, the caption and the hashtags. The full
script follows in a collapsed quote. A clip takes about 7 minutes with Chatterbox. To run it as a service, see `deploy/storygen-bot.service`.

## Config

Copy `storygen.example.toml` to `storygen.toml`; every key is optional. The main knobs are
`[story]` (length, genres, review pass), `[voice]` (voices, speed), `[scene]` (style, physics, hit
sound volume) and `[captions]`.

## Licences of the bundled or downloaded assets

Chatterbox Turbo: MIT. Kokoro-82M weights and voices: Apache-2.0. Anton font: SIL Open Font License. The hit sounds and the
animation are synthesised in code. The stories are fiction written by Claude for each clip.
