from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class DzenComment:
    comment_id: str
    author: str
    text: str
    publication_title: str
    publication_url: str
    article_context: str
    source: Literal["studio", "publication"]
    reply_locator_hint: str = ""


@dataclass(slots=True)
class AIReply:
    action: Literal["reply", "skip", "review"]
    reply: str
    reason: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class ProcessResult:
    comment_id: str
    author: str
    comment_text: str
    publication_url: str
    action: str
    reply_text: str = ""
    reason: str = ""
    published: bool = False
