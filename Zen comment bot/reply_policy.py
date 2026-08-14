from __future__ import annotations

import hashlib
import re

BLOG_INVITES = [
    "Если тема Вам актуальна, больше практических материалов собрали в нашем блоге: {url}",
    "Если захотите разобраться в теме подробнее, дополнительные материалы есть в нашем блоге: {url}",
    "Больше практических разборов по этой и смежным темам публикуем в нашем блоге: {url}",
    "Если будет полезно, загляните также в наш блог — там собрали больше материалов для организаций: {url}",
    "Дополнительные разборы и практические материалы можно посмотреть в нашем блоге: {url}",
]

URL_RE = re.compile(r"https?://\S+", re.I)
MULTISPACE_RE = re.compile(r"[ \t]{2,}")
MULTILINE_RE = re.compile(r"\n{3,}")


def stable_comment_id(publication_url: str, author: str, text: str, dom_id: str = "") -> str:
    if dom_id.strip():
        return dom_id.strip()
    raw = "\x1f".join([publication_url.strip(), author.strip().lower(), " ".join(text.split())])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def clean_ai_reply(text: str) -> str:
    text = text.strip().strip('"').strip()
    # AI should not add its own outbound URLs; the controlled blog invitation is appended below.
    text = URL_RE.sub("", text)
    text = MULTISPACE_RE.sub(" ", text)
    text = MULTILINE_RE.sub("\n\n", text)
    return text.strip(" \n—-")


def choose_blog_invite(comment_id: str, blog_url: str) -> str:
    digest = hashlib.sha256(comment_id.encode("utf-8")).digest()
    idx = digest[0] % len(BLOG_INVITES)
    return BLOG_INVITES[idx].format(url=blog_url)


def compose_final_reply(ai_reply: str, comment_id: str, blog_url: str, max_chars: int = 1500) -> str:
    core = clean_ai_reply(ai_reply)
    invite = choose_blog_invite(comment_id, blog_url)
    result = f"{core}\n\n{invite}".strip()
    if len(result) <= max_chars:
        return result
    # Preserve the CTA and trim only the AI body.
    reserve = len(invite) + 2
    core_limit = max(80, max_chars - reserve)
    trimmed = core[:core_limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return f"{trimmed}\n\n{invite}"
