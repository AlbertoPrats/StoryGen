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
1. PUNCHY SCARY HOOK: the first sentence is under 12 words and is the scariest concrete fact or image of
   the opening, stated plainly: a threat, a betrayal or something impossible, happening to the narrator.
   It must stop a thumb mid-scroll on its own. Start mid-action. No mood, no greeting, no "so", no
   "this is the story of", no "I never thought", no scene-setting, no backstory. Example shapes:
   "My wife's phone lit up with a text from my own number." /
   "The babysitter called to ask why there were two of my daughter." /
   "There was a second toothbrush in my bathroom, and it was still wet."
2. OPEN LOOP: within the first 3 sentences, plant one burning central question (who, what is it, what
   are they hiding). Feed it small, partial answers that make it worse.
3. ZERO FILLER: every single sentence must do at least one of these: bring the threat closer, reveal a
   new fact, or tighten the clock. Cut anything else: backstory, setting the scene, explaining how the
   narrator felt ("I was terrified", "my heart was pounding"), recaps, repeated beats, "little did I know",
   transitions like "a few days later" unless the gap itself is scary. If a sentence can be deleted
   without losing information or tension, it must not be there.
4. ESCALATION AND DREAD: every 2-3 sentences the danger gets closer, the secret gets bigger or the
   narrator loses a safe place or a person they trusted. Add a ticking clock when it fits (a time, a
   deadline, footsteps getting closer). 1-2 micro-cliffhangers ("Then I saw whose name was on the lease.").
5. CONCRETE: specific times, numbers, objects and sensory details (a 3:12 AM text, wet footprints,
   the smell of someone else's perfume). Invented first names only; no real people, brands,
   identifiable places, or politics.
6. SPOKEN FOR A VOICE ACTOR: short sentences (mostly 4-12 words), fragments allowed, contractions, past
   tense, how a scared person actually talks. Use "..." for a held breath before a reveal (2-4 times in
   the story) and a one-line beat on its own for the biggest shocks. No stage directions, emojis, hashtags,
   headings or quotation marks around the whole thing. Short dialogue in plain quotes is allowed.
   Numbers that sound odd when read aloud are written as words.
7. ENDING = CLEAR TWIST + EERIE CLIFFHANGER. The ending is the most important part; it must never be
   underwhelming or vague, or the viewer feels cheated.
   a) The twist: answer the central question clearly and concretely, in plain words. The viewer must know
      exactly what was going on (who, what, why it is worse). It is darker than what they feared, surprising
      but fair (set up by an earlier detail they can find on a rewatch).
   b) The cliffhanger: the final 1-2 sentences start a NEW, specific danger that is happening right now
      to the narrator (a knock from inside the closet, a text that just arrived, the figure is already in
      the room, the voice says the narrator's name), and the story stops at that peak, before the outcome.
      The last sentence is short, concrete and chilling; echoing the first line is a bonus.
   Banned endings: reflections or feelings ("I still think about it", "nothing was ever the same"),
   vague mysteries ("I never found out", "some things are better left unknown", "it's still out there"),
   "it was all a dream", a twist the viewer has to guess, a moral, a question to the audience,
   "follow for part 2".
8. PLATFORM-SAFE: dread, menace, implied violence, infidelity and the supernatural are fine; no gore,
   graphic violence, sexual detail, self-harm, slurs or hate.
9. The narration never claims to be true and never addresses the viewer as "you guys".

YouTube Shorts packaging (title, description, hashtags), written to get the most views, swipes-through
and comments from the Shorts feed and search:
- title (burned into the video): under 60 characters, an ominous curiosity gap built on the most
  shocking concrete detail; never spoils the twist.
- youtube_title: under 70 characters. Front-load the most shocking concrete detail in the first 40
  characters (that is what shows on a phone), first person, curiosity gap, strong words; may differ from
  the on-screen title. Sentence case, no clickbait that the story does not pay off, no ALL CAPS title,
  at most one emoji, no hashtags.
- description: 2-3 short lines, no hashtags (they are added separately). Line 1 is a fresh hook that does
  not repeat the title (it shows under the video). Line 2 is a pointed question that makes people comment
  (a choice or a theory about the story, e.g. "Would you have opened the door?"). Line 3 names what it
  is with natural search words, e.g. "A fictional scary story told before bed." Never claims to be true.
- hashtags: the few most searched tags specific to this story's topic (e.g. creepy texts, stalker,
  night shift, home alone), best first, lowercase, no spaces; the generic channel tags (shorts, scary
  stories, horror story, creepy...) are added automatically, so do not repeat them."""

SCORES = ("hook", "open_loop", "no_filler", "escalation", "dread", "twist", "cliffhanger", "spoken_rhythm",
          "overall")

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "on-screen title, under 60 chars: an ominous curiosity gap that does not spoil the twist"},
        "youtube_title": {"type": "string", "description": "YouTube Shorts title, under 70 chars (see packaging rules)"},
        "premise": {"type": "string", "description": "one sentence summary of the premise and twist"},
        "narrator": {"type": "string", "enum": ["male", "female"]},
        "script": {"type": "string", "description": "the full narration; blank lines between beats"},
        "description": {"type": "string", "description": "YouTube description, 2-3 short lines, no hashtags (see packaging rules)"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "youtube_title", "premise", "narrator", "script", "description", "hashtags"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {k: {"type": "integer", "minimum": 1, "maximum": 10}
                           for k in SCORES},
            "required": list(SCORES),
        },
        "problems": {"type": "array", "items": {"type": "string"}},
        "final_score": {"type": "integer", "minimum": 1, "maximum": 10,
                        "description": "honest overall retention score of YOUR improved version"},
        "title": {"type": "string"},
        "youtube_title": {"type": "string"},
        "script": {"type": "string", "description": "the improved final narration; blank lines between beats"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "problems", "final_score", "title", "youtube_title", "script", "description", "hashtags"],
}


def word_count(text: str) -> int:
    return sum(1 for t in text.split() if re.search(r"\w", t))


def _tag(t) -> str:
    return "#" + re.sub(r"[^\w]", "", str(t).lstrip("#")).lower()


def clean_hashtags(tags: list[str], n: int, core: list[str]) -> list[str]:
    """The fixed channel tags first, then up to `n` topic tags from Claude; at most 15 in total."""
    out = [c for c in dict.fromkeys(_tag(c) for c in core) if len(c) > 1]
    extra = 0
    for t in map(_tag, tags):
        if extra >= n:
            break
        if len(t) > 1 and t not in out:
            out.append(t)
            extra += 1
    return out[:15]


def youtube_text(story: dict) -> str:
    """The full YouTube description: the written lines, then the hashtags."""
    desc = (story.get("description") or story.get("caption") or "").strip()
    return "\n\n".join(x for x in (desc, " ".join(story.get("hashtags") or [])) if x)


def youtube_title(story: dict) -> str:
    t = re.sub(r"\s+", " ", story.get("youtube_title") or story.get("title") or "").strip().strip('"“”')
    t = re.sub(r"\s*#\w+", "", t).strip()
    return t[:100]


def tidy_script(text: str) -> str:
    """Normalise whitespace; keep blank lines between beats; drop markdown and stray quotes."""
    text = text.replace("\r", "").replace("*", "").replace("#", "").strip()
    # Drop quotes wrapped around the whole script, but keep the closing quote of final dialogue.
    if len(text) > 1 and text[0] in '"“' and text[-1] in '"”' and text.count('"') + text.count("“") + text.count("”") == 2:
        text = text[1:-1].strip()
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


def narrator_of(cfg: Config) -> str | None:
    """The forced narrator gender ([voice].narrator), or None when Claude may choose."""
    n = (cfg.voice.narrator or "auto").lower()
    return n if n in ("male", "female") else None


def write_story(cfg: Config, theme: str | None = None, rng: random.Random | None = None,
                state: StoryState | None = None) -> dict:
    """Returns {title, youtube_title, premise, narrator, script, description, hashtags, genre, theme, review}."""
    rng = rng or random.Random()
    sc = cfg.story
    words = int(sc.target_seconds * sc.words_per_second)
    genre = rng.choice(sc.genres) if sc.genres else "twist story"
    avoid = state.recent(sc.avoid_recent) if state else []
    narrator = narrator_of(cfg)
    ask = (f"Write one story. Genre: {genre}." if not theme else
           f"Write one story about this idea from the channel owner: {theme!r}. "
           f"Keep their idea; use the retention rules.")
    prompt = (
        f"{ask}\n\nLength: {words - 12}-{words + 8} words of narration (about {sc.target_seconds:.0f} seconds "
        f"read aloud). Count carefully; going over is worse than going under.\n"
        f"Separate beats with a blank line (4-7 beats).\n"
        + (f"The narrator is a {'man' if narrator == 'male' else 'woman'} (a {narrator} voice reads it); "
           f"write the whole story from that point of view.\n" if narrator else "")
        + f"Hashtags: the {sc.hashtags} best topic-specific ones, lowercase.\n"
        + ("\nThese premises were used recently. Write something clearly different (other setting, "
           "other twist):\n- " + "\n- ".join(avoid) + "\n" if avoid else "")
    )
    draft = ask_json(prompt, STORY_SCHEMA, CRAFT, cfg.llm, what="story draft")
    story = {**draft, "script": tidy_script(draft["script"]), "genre": genre if not theme else None,
             "theme": theme, "review": None}
    if narrator:
        story["narrator"] = narrator
    if sc.review:
        story = review_story(cfg, story, words)
    story["hashtags"] = clean_hashtags(story.get("hashtags") or [], sc.hashtags, sc.core_hashtags)
    story["youtube_title"] = youtube_title(story)
    story["description"] = (story.get("description") or story.get("caption") or "").strip()
    story["words"] = word_count(story["script"])
    log.info("Story: %r (%d words, %s narrator)", story["title"], story["words"], story["narrator"])
    if state:
        state.add(story)
    return story


def review_story(cfg: Config, story: dict, words: int) -> dict:
    prompt = (
        "You are the retention editor. Score this draft 1-10 on each rule, list concrete problems, then "
        "return an improved final version that fixes them, and score that final version honestly. Keep the "
        "premise and the narrator. Check, in this order:\n"
        "- Hook: is the first sentence a punchy, scary, concrete fact under 12 words? Sharpen it if it "
        "scores under 9.\n"
        "- Filler: go sentence by sentence; delete or replace every sentence that does not bring the threat "
        "closer, reveal a new fact or tighten the clock (backstory, feelings, recaps, scene-setting).\n"
        "- Dread: raise it wherever a sentence feels safe.\n"
        "- Twist: is it stated clearly enough that every viewer knows exactly what happened, darker than "
        "expected, and set up by an earlier detail? If it is implied or vague, say it plainly.\n"
        "- Cliffhanger: do the last 1-2 sentences start a new, concrete danger happening right now, and stop "
        "at its peak? If the ending is a reflection, a vague mystery or otherwise underwhelming, rewrite it.\n"
        f"Final length {words - 12}-{words + 8} words. Then rewrite the title, youtube_title, description and "
        "hashtags for the final version, following the packaging rules.\n\n"
        f"TITLE: {story['title']}\nYOUTUBE TITLE: {story.get('youtube_title', '')}\n"
        f"PREMISE: {story['premise']}\n\nDRAFT:\n{story['script']}\n\n"
        f"DESCRIPTION: {story.get('description', '')}\nHASHTAGS: {' '.join(story.get('hashtags') or [])}"
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
            "youtube_title": rev.get("youtube_title") or story.get("youtube_title"),
            "description": rev.get("description") or story.get("description"),
            "hashtags": rev.get("hashtags") or story.get("hashtags"),
            "review": {"draft_scores": rev.get("scores"), "problems": rev.get("problems"),
                       "final_score": rev.get("final_score")}}


def shorten_story(cfg: Config, story: dict, words: int) -> dict:
    """Used when the narration came out too long even at the fastest voice speed."""
    prompt = (f"This narration is too long. Cut it to at most {words} words without losing the hook, the "
              f"open loop, the clear twist or the final cliffhanger; cut filler first. Keep the style. Return the "
              f"title, youtube_title, description and hashtags unchanged unless the cut breaks them.\n\n"
              f"TITLE: {story['title']}\nYOUTUBE TITLE: {story.get('youtube_title', '')}\n\n{story['script']}\n\n"
              f"DESCRIPTION: {story.get('description', '')}")
    rev = ask_json(prompt, REVIEW_SCHEMA, CRAFT, cfg.llm, what="story shorten")
    return {**story, "script": tidy_script(rev["script"]), "words": word_count(rev["script"])}
