"""LLM access. Primary backend: Claude Code headless (`claude -p`, uses your Pro plan login).

Optional free fallback: a local Ollama model. If every backend fails, LLMError is raised
(a story cannot be written without a model).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from .config import LLMCfg
from .utils import log


class LLMError(RuntimeError):
    pass


def _extract_json(text: str):
    """Best-effort: pull the first JSON object/array out of free text."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = text.find(open_c), text.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Model reply did not contain valid JSON")


def _claude(prompt: str, schema: dict, system: str, cfg: LLMCfg) -> dict:
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("`claude` CLI not found on PATH")
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--tools", "",                       # pure reasoning, no file/shell access
        "--no-session-persistence",
        "--append-system-prompt", system,
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]
    # Run from an empty dir so no project CLAUDE.md / settings leak into the prompt.
    with tempfile.TemporaryDirectory(prefix="storygen-llm-") as cwd:
        env = dict(os.environ)
        if cfg.force_subscription:   # never bill an API key; use the Pro plan login
            env.pop("ANTHROPIC_API_KEY", None)
        proc = subprocess.run(cmd, input=prompt.encode(), capture_output=True, cwd=cwd,
                              timeout=cfg.timeout, env=env)
    out = proc.stdout.decode(errors="replace")
    if proc.returncode != 0 and not out.strip():
        raise LLMError(f"claude exited {proc.returncode}: {proc.stderr.decode(errors='replace')[-800:]}")
    try:
        env_json = json.loads(out)
    except json.JSONDecodeError:
        return _extract_json(out)
    if env_json.get("is_error"):
        raise LLMError(f"claude error: {str(env_json.get('result'))[:800]}")
    if isinstance(env_json.get("structured_output"), (dict, list)):
        return env_json["structured_output"]
    return _extract_json(str(env_json.get("result", "")))


def _ollama(prompt: str, schema: dict, system: str, cfg: LLMCfg) -> dict:
    import requests

    r = requests.post(
        f"{cfg.ollama_url.rstrip('/')}/api/chat",
        json={
            "model": cfg.ollama_model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.4, "num_ctx": 32768},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        },
        timeout=cfg.timeout,
    )
    r.raise_for_status()
    return _extract_json(r.json()["message"]["content"])


def ask_json(prompt: str, schema: dict, system: str, cfg: LLMCfg, what: str = "request") -> dict:
    """Ask the configured LLM for JSON matching `schema`. Raises LLMError if all backends fail."""
    order = {"claude": ["claude", "ollama"], "ollama": ["ollama"]}[cfg.backend]
    if not cfg.fallback:
        order = order[:1]
    errors = []
    for backend in order:
        fn = {"claude": _claude, "ollama": _ollama}[backend]
        for attempt in range(cfg.retries + 1):
            t0 = time.time()
            try:
                log.info("LLM[%s] %s (attempt %d)...", backend, what, attempt + 1)
                res = fn(prompt, schema, system, cfg)
                log.info("LLM[%s] %s done in %.0f s", backend, what, time.time() - t0)
                return res
            except Exception as e:  # noqa: BLE001 - any failure -> retry / next backend
                msg = f"{backend}: {e}"
                errors.append(msg)
                log.warning("LLM %s failed: %s", what, msg[:400])
                if backend == "ollama" and "Connection" in str(e):
                    break   # no local server; don't retry
                time.sleep(3 * (attempt + 1))
    raise LLMError("; ".join(errors) or "no LLM backend configured")
