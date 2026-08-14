from reply_policy import clean_ai_reply, compose_final_reply, stable_comment_id


def test_stable_comment_id():
    a = stable_comment_id("https://dzen.ru/a/1", "Ivan", "  hello   world ")
    b = stable_comment_id("https://dzen.ru/a/1", "ivan", "hello world")
    assert a == b


def test_clean_removes_external_urls():
    assert "http" not in clean_ai_reply("Ответ здесь https://example.com и всё.")


def test_compose_has_blog():
    result = compose_final_reply("Полезный ответ.", "abc", "https://boykovgroup.ru/blog")
    assert "Полезный ответ." in result
    assert "https://boykovgroup.ru/blog" in result
