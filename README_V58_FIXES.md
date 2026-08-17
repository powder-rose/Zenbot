# v58 — fixes after v57

Исправлено два найденных сбоя.

1. Недельный Dzen responder теперь не анализирует DOM только один раз в самом низу страницы. Он ждёт загрузку карточек, собирает комментарии на каждом шаге прокрутки, поддерживает виртуализированный список и сохраняет `scan_zero_*.html/.png`, если Студия снова вернёт 0 карточек.
2. Убрано слишком жёсткое отбрасывание всех ссылок, где `comments_data` не равен `n_reply`. Режим `all` может быть режимом списка, а не признаком ответа на конкретный комментарий. Повторы, сделанные ботом, по-прежнему блокируются persistent state.
3. `YandexGPTClient.generate_article_from_sources()` снова принимает `system_prompt`, который уже передаёт `tenant_service.py`. Это устраняет ошибку `unexpected keyword argument 'system_prompt'`.

Заменить:
- `dzen_comment_responder.py`
- `yandex_gpt.py`

Проверка:
```bash
python3 -m py_compile dzen_comment_responder.py yandex_gpt.py bot.py tenant_service.py
```

Если недельный скан снова покажет `Просканировано: 0`, прислать самый свежий файл из:
```text
/root/Zenbot/data/dzen_comment_responder_debug/scan_zero_*.html
```
или соответствующий PNG.
