"""Stage 1 - write a made-up, highly retentive story-time script with Claude (headless, Pro plan).

Two calls: a writer drafts the story from a genre (or the user's theme), then a retention editor
scores the draft against a checklist and returns a tightened final script. Recent premises are
kept in work/story_state.json and sent along, so the feed does not repeat itself.
"""
from __future__ import annotations

import random
import re
import time

from .config import Config
from .llm import ask_json
from .utils import log, read_json, write_json

CRAFT = """You write short-form "story time" narrations for TikTok / YouTube Shorts / Reels.
The story is fiction, told aloud by one first-person narrator over a satisfying background animation.
The channel only does MYSTERY, HORROR and TENSE stories: dark secrets uncovered, cheating partners, people
who are not who they claim, someone in the house, things that should not be possible. The viewer must feel
dread from the first line and must not be able to swipe away until the last one.

Retention rules (all mandatory):
1. HOOK: the first sentence is under 14 words and puts danger, betrayal or something impossible in front
   of the viewer immediately. Start mid-action or with one wrong, specific detail. No greeting, no "so",
   no "this is the story of", no "I never thought", no scene-setting. Example shapes:
   "My husband's phone lit up with a text from my own number." /
   "The babysitter called to ask why there were two of my daughter." /
   "There was a second toothbrush in my bathroom, and it was still wet."
2. OPEN LOOP: within the first 3 sentences, plant one burning central question (who, what is it, what
   are they hiding). Feed it small, partial answers that make it worse; answer it only in the last
   two sentences.
3. ESCALATION AND DREAD: every 2-3 sentences the danger gets closer, the secret gets bigger or the
   narrator loses a safe place or a person they trusted. Add a ticking clock when it fits (a time, a
   deadline, footsteps getting closer). Never let tension drop; no calm or explaining sentences.
   1-2 micro-cliffhangers ("Then I saw whose name was on the lease.").
4. CONCRETE: specific times, numbers, objects and sensory details (a 3:12 AM text, wet footprints,
   the smell of someone else's perfume). Invented first names only; no real people, brands,
   identifiable places, or politics.
5. SPOKEN FOR A VOICE ACTOR: short sentences (mostly 4-12 words), fragments allowed, contractions, past
   tense, how a scared person actually talks. Use "..." for a held breath before a reveal (2-4 times in
   the story) and a one-line beat on its own for the biggest shocks. No stage directions, emojis, hashtags,
   headings or quotation marks around the whole thing. Short dialogue in plain quotes is allowed.
   Numbers that sound odd when read aloud are written as words.
6. PAYOFF: the last 2 sentences reveal a twist that recontextualises the hook and is darker than what the
   viewer feared. Surprising but fair (set up by an earlier detail they can find on a rewatch). The final
   sentence is short and leaves a chill, ideally echoing the first line so the loop invites a rewatch.
   No moral, no question to the audience, no "follow for part 2".
7. PLATFORM-SAFE: dread, menace, implied violence, infidelity and the supernatural are fine; no gore,
   graphic violence, sexual detail, self-harm, slurs or hate.
8. The narration never claims to be true and never addresses the viewer as "you guys"."""

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "on-screen title, under 60 chars: an ominous curiosity gap that does not spoil the twist"},
        "premise": {"type": "string", "description": "one sentence summary of the premise and twist"},
        "narrator": {"type": "string", "enum": ["male", "female"]},
        "script": {"type": "string", "description": "the full narration; blank lines between beats"},
        "caption": {"type": "string", "description": "post caption, 1-2 sentences, teases without spoiling"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "premise", "narrator", "script", "caption", "hashtags"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {k: {"type": "integer", "minimum": 1, "maximum": 10}
                           for k in ("hook", "open_loop", "escalation", "dread", "twist", "spoken_rhythm", "overall")},
            "required": ["hook", "open_loop", "escalation", "dread", "twist", "spoken_rhythm", "overall"],
        },
        "problems": {"type": "array", "items": {"type": "string"}},
        "final_score": {"type": "integer", "minimum": 1, "maximum": 10,
                        "description": "honest overall retention score of YOUR improved version"},
        "title": {"type": "string"},
        "script": {"type": "string", "description": "the improved final narration; blank lines between beats"},
        "caption": {"type": "string"},
    },
    "required": ["scores", "problems", "final_score", "title", "script", "caption"],
}


def word_count(text: str) -> int:
    return sum(1 for t in text.split() if re.search(r"\w", t))


def clean_hashtags(tags: list[str], n: int) -> list[str]:
    out = ["#storytime"]
    for t in tags:
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#")).lower()
        if len(t) > 1 and t not in out:
            out.append(t)
    for extra in ("#shorts", "#story", "#fyp"):
        if len(out) >= n:
            break
        if extra not in out:
            out.append(extra)
    return out[: min(max(n, 3), 15)]


def tidy_script(text: str) -> str:
    """Normalise whitespace; keep blank lines between beats; drop markdown and stray quotes."""
    text = text.replace("\r", "").replace("*", "").replace("#", "").strip().strip('"“”')
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


class StoryState:
    """Recent premises (and titles), so Claude does not keep writing the same story."""

    def __init__(self, cfg: Config):
        self.path = cfg.path(cfg.work_dir) / "story_state.json"
        self.data = read_json(self.path) if self.path.exists() else {"recent": []}

    def recent(self, n: int) -> list[str]:
        return [f"{r['title']} — {r['premise']}" for r in self.data["recent"][-n:]]

    def add(self, story: dict) -> None:
        self.data["recent"].append({"title": story["title"], "premise": story["premise"], "at": time.time()})
        self.data["recent"] = self.data["recent"][-200:]
        write_json(self.path, self.data)


def write_story(cfg: Config, theme: str | None = None, rng: random.Random | None = None,
                state: StoryState | None = None) -> dict:
    """Returns {title, premise, narrator, script, caption, hashtags, genre, theme, review}."""
    rng = rng or random.Random()
    sc = cfg.story
    words = int(sc.target_seconds * sc.words_per_second)
    genre = rng.choice(sc.genres) if sc.genres else "twist story"
    avoid = state.recent(sc.avoid_recent) if state else []
    ask = (f"Write one story. Genre: {genre}." if not theme else
           f"Write one story about this idea from the channel owner: {theme!r}. "
           f"Keep their idea; use the retention rules.")
    prompt = (
        f"{ask}\n\nLength: {words - 12}-{words + 8} words of narration (about {sc.target_seconds:.0f} seconds "
        f"read aloud). Count carefully; going over is worse than going under.\n"
        f"Separate beats with a blank line (4-7 beats).\n"
        f"Hashtags: {sc.hashtags} relevant ones, lowercase.\n"
        + ("\nThese premises were used recently. Write something clearly different (other setting, "
           "other twist):\n- " + "\n- ".join(avoid) + "\n" if avoid else "")
    )
    draft = ask_json(prompt, STORY_SCHEMA, CRAFT, cfg.llm, what="story draft")
    story = {**draft, "script": tidy_script(draft["script"]), "genre": genre if not theme else None,
             "theme": theme, "review": None}
    if sc.review:
        story = review_story(cfg, story, words)
    story["hashtags"] = clean_hashtags(story.get("hashtags") or [], sc.hashtags)
    story["words"] = word_count(story["script"])
    log.info("Story: %r (%d words, %s narrator)", story["title"], story["words"], story["narrator"])
    if state:
        state.add(story)
    return story


def review_story(cfg: Config, story: dict, words: int) -> dict:
    prompt = (
        "You are the retention editor. Score this draft 1-10 on each rule, list concrete problems, then "
        "return an improved final version that fixes them, and score that final version honestly. Keep the premise and the narrator. Make the hook "
        "sharper if it scores under 9, raise the dread wherever a sentence feels safe, make sure the twist is "
        f"darker than expected and set up by an earlier detail, and cut every sentence that does not raise tension. Final length {words - 12}-{words + 8} words.\n\n"
        f"TITLE: {story['title']}\nPREMISE: {story['premise']}\n\nDRAFT:\n{story['script']}\n\n"
        f"CAPTION: {story['caption']}"
    )
    try:
        rev = ask_json(prompt, REVIEW_SCHEMA, CRAFT, cfg.llm, what="story review")
    except Exception as e:  # noqa: BLE001 - the draft is still usable
        log.warning("Story review failed, keeping the draft: %s", e)
        return story
    new = tidy_script(rev.get("script") or "")
    if word_count(new) < 0.5 * words:          # a broken reply; keep the draft
        log.warning("Story review returned a too-short script; keeping the draft")
        return story
    return {**story, "title": rev.get("title") or story["title"], "script": new,
            "caption": rev.get("caption") or story["caption"],
            "review": {"draft_scores": rev.get("scores"), "problems": rev.get("problems"),
                       "final_score": rev.get("final_score")}}


def shorten_story(cfg: Config, story: dict, words: int) -> dict:
    """Used when the narration came out too long even at the fastest voice speed."""
    prompt = (f"This narration is too long. Cut it to at most {words} words without losing the hook, the "
              f"open loop or the twist. Keep the style.\n\nTITLE: {story['title']}\n\n{story['script']}\n\n"
              f"CAPTION: {story['caption']}")
    rev = ask_json(prompt, REVIEW_SCHEMA, CRAFT, cfg.llm, what="story shorten")
    return {**story, "script": tidy_script(rev["script"]), "words": word_count(rev["script"])}
