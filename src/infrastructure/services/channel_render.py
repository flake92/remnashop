"""Telegram HTML -> e-mail (HTML + plain text) and Web Push text.

Bot texts use Telegram's HTML subset. Only that subset is carried over;
everything else is reduced to its text, so a translation or an admin
broadcast cannot inject markup into an e-mail.
"""

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit

PUSH_TITLE_MAX = 80
PUSH_BODY_MAX = 180
SUBJECT_MAX = 120

_INLINE_TAGS = {
    "b": "strong",
    "strong": "strong",
    "i": "em",
    "em": "em",
    "u": "u",
    "ins": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "code": "code",
}
_BLOCK_TAGS = {"pre": "pre", "blockquote": "blockquote"}
_SAFE_SCHEMES = {"http", "https", "mailto"}


def safe_link(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _SAFE_SCHEMES:
        return None
    if parsed.scheme.lower() != "mailto" and not parsed.hostname:
        return None
    return url


class _TelegramHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_parts: list[str] = []
        self.text_parts: list[str] = []
        self._open: list[Optional[str]] = []
        self._links: list[Optional[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.handle_data("\n")
            return
        mapped = _INLINE_TAGS.get(tag) or _BLOCK_TAGS.get(tag)
        if tag == "a":
            href = safe_link(dict(attrs).get("href"))
            self._links.append(href)
            if href:
                self.html_parts.append(
                    f'<a href="{html.escape(href, quote=True)}" style="color:#2563eb">'
                )
                self._open.append("a")
            else:
                self._open.append(None)
            return
        if mapped:
            style = ""
            if mapped == "blockquote":
                style = ' style="margin:8px 0;padding:4px 12px;border-left:3px solid #cbd5e1"'
            elif mapped == "pre":
                style = ' style="white-space:pre-wrap;font-family:monospace"'
            self.html_parts.append(f"<{mapped}{style}>")
        # Unknown tags (tg-emoji, tg-spoiler, span...) keep only their text.
        self._open.append(mapped)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._open:
            return
        mapped = self._open.pop()
        if tag == "a" and self._links:
            href = self._links.pop()
            if mapped == "a":
                self.html_parts.append("</a>")
            if href and not href.startswith("mailto:"):
                self.text_parts.append(f" ({href})")
            return
        if mapped:
            self.html_parts.append(f"</{mapped}>")

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        self.html_parts.append(html.escape(data, quote=False).replace("\n", "<br>\n"))


@dataclass(frozen=True)
class RenderedText:
    html: str
    text: str


def render_telegram_html(source: str) -> RenderedText:
    parser = _TelegramHtml()
    parser.feed(source)
    parser.close()
    while parser._open:
        mapped = parser._open.pop()
        if mapped:
            parser.html_parts.append(f"</{mapped}>")
    text = re.sub(r"[ \t]+\n", "\n", "".join(parser.text_parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return RenderedText(html="".join(parser.html_parts).strip(), text=text)


def _shorten(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def subject_for(text: str, fallback: str) -> str:
    line = _first_line(text)
    # Very short first lines are emoji or a greeting; they make a poor subject.
    if len(re.sub(r"\W", "", line)) < 3:
        return fallback
    return _shorten(line, SUBJECT_MAX)


def push_text(text: str, fallback_title: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return fallback_title, ""
    title = lines[0]
    rest = lines[1:]
    if len(re.sub(r"\W", "", title)) < 3:
        title, rest = fallback_title, lines
    return _shorten(title, PUSH_TITLE_MAX), _shorten(" ".join(rest), PUSH_BODY_MAX)


__all__ = [
    "PUSH_BODY_MAX",
    "PUSH_TITLE_MAX",
    "RenderedText",
    "push_text",
    "render_telegram_html",
    "safe_link",
    "subject_for",
]
