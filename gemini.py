from __future__ import annotations

import os
import random
import time
from typing import Any, Callable

import httpx
import httpx2
from google import genai
from google.genai import errors, types

# NOTE (carried over from YKYRMOAFIK/engine.js): the gemini-1.5-* and gemini-2.5-*
# families are not available to this API key — Google returns "no longer available
# to new users" for both. Use a floating alias so model rotations don't 404 this
# installation; pin an exact id via GEMINI_MODEL if you need reproducible output.
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# Safety filters off. A prayer is often about illness, debt, death or a failing
# relationship, and the entity answers in the voice of a deity; the default filters
# refuse both halves of that and a refusal on the panel ends the piece.
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold="BLOCK_NONE")
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

# Flash models reason before answering and that reasoning sometimes lands in the
# reply itself. Neither the entity nor the transcriber needs a reasoning budget, so
# it is turned off at the source. Not every model id accepts the field, so a
# rejection disables it for the rest of the run instead of failing the turn.
_thinking_supported = True

# Codes worth retrying: the free tier answers bursts with 429 and the model is
# periodically busy with 503.
_RETRYABLE = {429, 500, 502, 503, 504}
# A dropped connection or a DNS failure (the installation's Wi-Fi blinking) is
# retried the same way rather than ending the session.
NETWORK_ERRORS = (httpx.TransportError, httpx2.TransportError)


def make_client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set — see .env")
    return genai.Client(api_key=key)


def extract_text(response: Any) -> str:
    # Pull all text parts out of a response. response.text is None when the model
    # returned no candidate at all, so the parts are walked defensively.
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            piece = getattr(part, "text", None)
            if isinstance(piece, str) and piece.strip():
                parts.append(piece)
    return " ".join(parts)


def call_with_retry(fn: Callable[[], Any], label: str, tries: int = 5) -> Any:
    # Exponential backoff with jitter: ~2s, 4s, 8s, 16s.
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except (errors.APIError, *NETWORK_ERRORS) as e:
            code = getattr(e, "code", None) or type(e).__name__
            if (isinstance(e, errors.APIError) and code not in _RETRYABLE) or attempt == tries:
                raise
            wait = round(2.0 * 2 ** (attempt - 1) * (0.75 + random.random() * 0.5), 1)
            print(f"  [{label}] {code} retry {attempt}/{tries - 1} in {wait}s")
            time.sleep(wait)


def generate(
    client: genai.Client,
    contents: Any,
    *,
    label: str,
    system: str | None = None,
    max_tokens: int = 300,
    temperature: float = 1.0,
    top_p: float = 0.95,
    response_mime_type: str | None = None,
    response_schema: Any | None = None,
) -> str:
    """
    One Gemini call, with retries, safety off and thinking disabled.

    Returns the raw text of the response (possibly empty — callers decide what an
    empty answer means).
    """
    global _thinking_supported

    while True:
        config = types.GenerateContentConfig(
            system_instruction=system,
            safety_settings=SAFETY_SETTINGS,
            # High temperature: the entity should not regress towards polite neutral
            # assistant prose, which is where low temperature takes it.
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_tokens,
            response_mime_type=response_mime_type,
            response_schema=response_schema,
            thinking_config=(
                types.ThinkingConfig(thinking_budget=0) if _thinking_supported else None
            ),
        )
        try:
            response = call_with_retry(
                lambda: client.models.generate_content(
                    model=MODEL, contents=contents, config=config
                ),
                label,
            )
            return extract_text(response)
        except errors.APIError as e:
            msg = str(e)
            if _thinking_supported and getattr(e, "code", None) == 400 and "think" in msg.lower():
                print(f"  [{label}] {MODEL} rejects thinking_config — continuing without it")
                _thinking_supported = False
                continue
            raise
