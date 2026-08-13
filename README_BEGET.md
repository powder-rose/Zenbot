# Zen Bot v39 — Beget Ubuntu VPS

Это серверная версия текущей рабочей схемы.

## Что делает бот

1. Берёт новую тему из SQLite.
2. Получает источники через Yandex Search API.
3. YandexGPT создаёт:
   - LONG-статью примерно 2800–3200 символов с пробелами;
   - SHORT-версию для Telegram.
4. YandexART создаёт изображение.
5. Через **Telegram Web под вашим Premium-аккаунтом** публикуется один обычный media post:
   - реальная картинка;
   - LONG-caption около 3000 символов.
6. Синхробот Дзена забирает LONG-публикацию.
7. Через 180 секунд бот **удаляет** LONG-пост из Telegram, не редактируя его.
8. После удаления через Bot API публикуется новый SHORT Rich Message с той же картинкой.
9. В Дзене остаётся полная статья; в Telegram остаётся короткая версия.

## Чем v39 отличается от Windows v33

В v33 использовались Telegram Desktop + PyAutoGUI + WinAPI.

В v39 этого нет. Вместо них используется:

- Ubuntu;
- headless Chromium;
- Playwright;
- Telegram Web;
- persistent browser profile.

Мышь, рабочий стол, RDP/VNC и открытое окно Telegram не нужны.

---

# 1. Рекомендуемый VPS

Рекомендуется:

- Ubuntu 24.04;
- 2 vCPU;
- 4 GB RAM;
- 30–40 GB диска.

Минимально Chromium может запуститься и на 2 GB RAM, но 4 GB надёжнее.

---

# 2. Загрузка проекта на Beget

Например:

```bash
mkdir -p /opt/zen_bot
cd /opt/zen_bot
```

Загрузите сюда все файлы из архива.

Если загрузили ZIP:

```bash
unzip zen_bot_beget_v39.zip
cd zen_bot_beget
```

---

# 3. Установка

```bash
chmod +x install_beget.sh run.sh install_systemd.sh
./install_beget.sh
```

Скрипт:

- создаст Python venv;
- установит зависимости проекта;
- установит Playwright;
- установит Chromium и системные библиотеки;
- создаст рабочие каталоги.

---

# 4. Настройка .env

```bash
cp .env.example .env
nano .env
```

Обязательные поля:

```env
TELEGRAM_BOT_TOKEN=...
ADMIN_IDS=...
TELEGRAM_CHANNEL_ID=-100...

TG_WEB_CHANNEL=boykov_nikolay
TG_WEB_PROFILE_DIR=data/telegram_web_profile
TG_WEB_HEADLESS=true

YC_FOLDER_ID=...
YC_API_KEY=...

DB_PATH=data/bot.db
TIMEZONE=Europe/Moscow
```

Не отправляйте `.env` другим людям.

---

# 5. Одноразовая авторизация Telegram Web

Остановите все экземпляры bot.py.

Активируйте окружение:

```bash
source venv/bin/activate
export PLAYWRIGHT_BROWSERS_PATH=0
```

Сначала попробуйте вход по номеру телефона:

```bash
python setup_telegram_web.py
```

Скрипт спросит:

1. номер Premium-аккаунта;
2. код входа, который придёт в Telegram;
3. пароль 2FA, если он включён.

Пароль 2FA нигде не сохраняется.

После успешного входа браузерная Telegram-сессия сохранится в:

```text
data/telegram_web_profile/
```

Этот каталог является секретом: он содержит авторизованную сессию Telegram.

## Если вход по номеру не сработал

Запустите:

```bash
python setup_telegram_web.py --qr
```

Скрипт будет обновлять файл:

```text
data/telegram_login_qr.png
```

Откройте его через файловый менеджер/SFTP Beget и отсканируйте QR телефоном:

Telegram → Настройки → Устройства → Подключить устройство.

---

# 6. Проверка авторизации

```bash
python check_telegram_web.py
```

Нормальный результат:

```text
"authorized": true
```

После этого:

```bash
python server_check.py
```

В конце должно быть:

```text
Все основные проверки пройдены.
```

---

# 7. Первый ручной запуск

```bash
./run.sh
```

Проверьте админку бота и создайте одну срочную тестовую статью.

В логе ожидается:

```text
YandexART: изображение готово...
Telegram Web: long media post опубликован...
```

Через 180 секунд:

```text
Telegram Web: long-post удалён...
Bot API: публикую short Rich Message...
Готово: long Telegram-пост удалён; short Rich Message опубликован...
```

Если Telegram Web изменил интерфейс и какой-то locator перестал работать,
бот сохранит screenshot и HTML сюда:

```text
data/telegram_web_debug/
```

Именно эти файлы нужны для диагностики.

---

# 8. Запуск как systemd service

Когда ручной тест прошёл:

```bash
./install_systemd.sh
sudo systemctl start zenbot
```

Проверить:

```bash
sudo systemctl status zenbot
```

Логи:

```bash
sudo journalctl -u zenbot -f
```

Перезапуск:

```bash
sudo systemctl restart zenbot
```

Остановка:

```bash
sudo systemctl stop zenbot
```

Автозапуск после reboot уже включён скриптом.

---

# 9. Обновление проекта

Перед обновлением:

```bash
sudo systemctl stop zenbot
```

Замените Python-файлы, затем:

```bash
source venv/bin/activate
pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium
python server_check.py
sudo systemctl start zenbot
```

---

# 10. Важные ограничения

Telegram Web — пользовательский интерфейс, а не стабильный публичный API.
Telegram может менять DOM и CSS-классы.

Поэтому publisher использует несколько fallback-locator'ов и при любой ошибке
автоматически сохраняет screenshot + HTML.

После больших обновлений Telegram Web может понадобиться поправить selectors
в `telegram_web_publisher.py`.

Не запускайте одновременно два экземпляра `bot.py` с одним и тем же
`TG_WEB_PROFILE_DIR`: Chromium блокирует один persistent profile от параллельного
использования.

---

# 11. Безопасность

Никому не отправляйте:

- `.env`;
- `data/telegram_web_profile/`;
- Yandex Cloud API key;
- Telegram bot token;
- screenshots страницы логина Telegram.

Добавьте проект в резервное копирование, но храните backup профиля Telegram Web
в закрытом хранилище.

---

# Быстрый путь

```bash
unzip zen_bot_beget_v39.zip
cd zen_bot_beget

chmod +x install_beget.sh run.sh install_systemd.sh
./install_beget.sh

cp .env.example .env
nano .env

source venv/bin/activate
export PLAYWRIGHT_BROWSERS_PATH=0

python setup_telegram_web.py
python check_telegram_web.py
python server_check.py

./run.sh
```

Если всё работает:

```bash
Ctrl+C
./install_systemd.sh
sudo systemctl start zenbot
sudo journalctl -u zenbot -f
```

---

---

# 12. Три автоматические статьи в день

В v39 автоматическая публикация по умолчанию работает три раза в день:

```env
TIMEZONE=Europe/Moscow
DEFAULT_PUBLISH_TIMES=09:00,14:00,19:00
```

Стандартно статьи выходят в 09:00, 14:00 и 19:00 по Москве.

Время можно изменить через `.env`, например:

```env
DEFAULT_PUBLISH_TIMES=10:00,15:00,20:00
```

Каждый конкретный слот выполняется только один раз за календарный день.

## Логика тем

Тема может повторяться позже, но при наличии двух и более активных тем
одна и та же тема не публикуется два раза подряд.

Сначала используются темы, которые ещё ни разу не публиковались.
Когда они заканчиваются, повторы разрешаются; приоритет получает тема,
которая не использовалась дольше всего.

## Срочные статьи

Тема, введённая как срочная:

- используется только для этой статьи;
- НЕ добавляется в таблицу `topics`;
- НЕ попадает в будущую автоматическую ротацию;
- сохраняется только в истории конкретной публикации.

Таким образом, срочные запросы не загрязняют постоянный пул плановых тем.


---

# 13. Жирный заголовок

В v39 заголовок оформляется жирным на уровне публикации, а не через Markdown.

- LONG-пост через Telegram Web: первая строка caption выделяется и форматируется Ctrl+B.
- SHORT Rich Message: заголовок оборачивается в HTML-тег `<b>` только в рендеринге Telegram.
- В тексте статьи нет `*`, `**` и других Markdown-символов.


---

# 14. Чёткое форматирование списков

В v39 списки нормализуются автоматически.

Если модель вернёт:

```text
• пункт 1 • пункт 2 • пункт 3
```

перед публикацией код преобразует это в:

```text
• пункт 1
• пункт 2
• пункт 3
```

Каждый пункт списка всегда начинается с новой строки.

---

# 15. Исправление QR-авторизации

В v39 `setup_telegram_web.py --qr` сначала нажимает
`LOG IN BY QR CODE`, ждёт появления QR-формы и только после этого
создаёт `data/telegram_login_qr.png`.

PNG обновляется каждые 2 секунды, поэтому открывайте свежую копию
и сканируйте сразу.
