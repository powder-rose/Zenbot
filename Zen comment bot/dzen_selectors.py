"""Centralized Dzen UI selectors.

Dzen has no stable public comments API documented for this workflow, so v1 uses
browser automation. All fragile selectors live here to make UI updates local.
"""

COMMENT_CONTAINER_SELECTORS = [
    '[data-testid*="comment" i]',
    '[data-qa*="comment" i]',
    'article:has(button:has-text("Ответить"))',
    'div:has(> div button:has-text("Ответить"))',
]

COMMENT_TEXT_SELECTORS = [
    '[data-testid*="comment-text" i]',
    '[data-qa*="comment-text" i]',
    '[class*="commentText"]',
    '[class*="comment__text"]',
    'p',
]

AUTHOR_SELECTORS = [
    '[data-testid*="author" i]',
    '[data-qa*="author" i]',
    '[class*="author"]',
    'a[href*="/id/"]',
]

REPLY_BUTTON_SELECTORS = [
    'button:has-text("Ответить")',
    '[role="button"]:has-text("Ответить")',
]

REPLY_INPUT_SELECTORS = [
    'textarea',
    '[contenteditable="true"]',
    '[role="textbox"]',
]

SEND_BUTTON_SELECTORS = [
    'button:has-text("Отправить")',
    'button:has-text("Опубликовать")',
    '[role="button"]:has-text("Отправить")',
]

STUDIO_COMMENTS_LINK_SELECTORS = [
    'a:has-text("Комментарии")',
    'button:has-text("Комментарии")',
    '[role="link"]:has-text("Комментарии")',
]

PUBLICATION_LINK_SELECTORS = [
    'a[href*="/a/"]',
    'a[href*="/video/"]',
    'a[href*="/shorts/"]',
]
