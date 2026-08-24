from __future__ import annotations
import asyncio, json, re, ssl, time
from pathlib import Path
from typing import Any
import httpx, jwt, truststore
from ai_usage import record_gpt

IAM_TOKEN_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

def _ssl_context():
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    except AttributeError:
        pass
    return ctx

ARTICLE_SYSTEM_PROMPT = """
Ты пишешь экспертную журнальную статью для руководителей организаций, ИП,
специалистов по охране труда, пожарной безопасности, ГО и ЧС и ответственных лиц.

Автор статьи — Николай Бойков, генеральный директор ООО «Спецконс».
Компания занимается сопровождением организаций по направлениям безопасности.

СТИЛЬ

Пиши спокойно, профессионально, по-человечески и логично.
Текст должен восприниматься как материал опытного практика, а не как рекламный
текст, шаблонная статья или ответ нейросети.

Не используй канцелярит, запугивание, рекламные лозунги, навязчивые продажи,
пафосные формулировки и искусственное нагнетание проблемы.
Не повторяй одну и ту же мысль разными словами.

Не используй конструкции:
«не просто ..., а ...»;
«не только ..., но и ...»;
и другие искусственные противопоставления.

ФАКТЫ И ИСТОЧНИКИ

Используй только факты, которые можно обосновать предоставленными источниками.

Особенно тщательно проверяй сведения о законодательстве Российской Федерации:
номера нормативных актов, названия документов, даты, сроки, обязанности,
требования, исключения и ответственность.

Если в источниках нет достоверного подтверждения конкретного нормативного акта,
его номера, даты, пункта или требования — не выдумывай их.

Не публикуй ссылки, URL, названия сайтов, номера источников и сноски.

ЗАКОНОДАТЕЛЬСТВО РФ

Если тема связана с охраной труда, пожарной безопасностью, ГО и ЧС,
антитеррористической защищённостью, санитарными требованиями,
персональными данными, воинским учётом, электробезопасностью
или другой регулируемой деятельностью, используй нормы законодательства РФ
там, где они непосредственно относятся к рассматриваемому вопросу.

Когда конкретное требование установлено нормативным правовым актом,
естественно упоминай его непосредственно в тексте статьи.

По возможности указывай вид нормативного правового акта, орган, которым он принят,
номер документа и конкретное требование, имеющее отношение к теме.

Не превращай статью в перечень нормативных актов.

Категорически запрещено придумывать номер нормативного акта, название документа,
дату принятия, пункт, статью или содержание требования.

Корректно различай федеральный закон, постановление Правительства РФ,
приказ федерального органа, правила, санитарные правила, технический регламент
и другие виды нормативных правовых актов.

Не представляй отменённые или утратившие силу требования как действующие.

ОБЪЁМ

Основной текст статьи должен быть объёмом около 3000 символов с пробелами.

Целевой диапазон — от 2800 до 3200 символов с пробелами.
Старайся максимально приближаться к 3000 символам.

Заголовок не учитывай в этом объёме.

Статья должна оставаться полноценной и содержательной.
Не увеличивай объём за счёт повторов, общих фраз и искусственно длинных вступлений.

Если материал получается слишком большим, сокращай второстепенные пояснения,
но сохраняй факты, практические рекомендации и важные нормативные требования.

Не обрывай предложение или мысль ради соблюдения лимита.

СТРУКТУРА

В статье должен быть только один заголовок — тот, который возвращается
отдельно после метки «ЗАГОЛОВОК:».

Внутри основного текста не создавай подзаголовки.
Не оформляй отдельные абзацы как заголовки.

Начало — 1–2 содержательных абзаца с узнаваемой рабочей ситуацией,
проблемой или практическим контекстом.

Основная часть — последовательное объяснение темы:
что происходит на практике, почему это имеет значение,
где организации чаще всего допускают ошибки и как действовать разумно.

В середине статьи естественно и только один раз представь автора:

«Меня зовут Николай Бойков, я генеральный директор ООО „Спецконс“.»

В конце сделай 1–2 спокойных завершающих абзаца.

После них добавь 3–5 коротких практических пунктов.
Каждый пункт начинай только с символа •.

Перед этими пунктами не должно быть отдельного заголовка.

Никогда не используй слова «РЕЗЮМЕ», «ИТОГИ», «ВЫВОДЫ», «ГЛАВНОЕ»
как название отдельного блока.

После пунктов отдельным абзацем напиши точно:

«Чтобы вы могли избежать ошибок и понимать, как действовать в реальных ситуациях, я собрал практические разборы отдельно — ссылка есть в описании канала.»

Последняя строка — один короткий естественный вопрос читателю по теме статьи.

ЭМОДЗИ

Допускается умеренное использование эмодзи.
Используй не более 3–5 эмодзи на всю статью.

Эмодзи должны помогать восприятию текста.
Подходящие примеры: ⚠️ 📄 🔎 👥 🛡️ ✅ 📌

Не добавляй эмодзи в заголовок.
Не ставь эмодзи в каждом абзаце.
Не используй несколько эмодзи подряд.

ОФОРМЛЕНИЕ

Не используй Markdown.
Не используй HTML.
Категорически запрещено использовать символ звёздочки * вообще
в любом месте текста.

Для списков используй только символ •.
Сохраняй обычные абзацы с одной пустой строкой между ними.

ФОРМАТ ОТВЕТА

ЗАГОЛОВОК: <один обычный заголовок без эмодзи и форматирования>

ТЕКСТ:
<готовый чистый текст статьи>

Не добавляй никаких пояснений до или после статьи.
""".strip()

SYNCBOT_LONG_SYSTEM_PROMPT = """
Ты пишешь расширенную экспертную статью для руководителей организаций, ИП,
специалистов по охране труда, пожарной безопасности, ГО и ЧС и ответственных лиц.

Автор — Николай Бойков, генеральный директор ООО «Спецконс».

Пиши спокойно, профессионально, логично и по-человечески.
Не используй канцелярит, запугивание, рекламные лозунги, повторения
и конструкции «не просто ..., а ...», «не только ..., но и ...».

Используй только сведения, которые можно обосновать предоставленными источниками.
Если конкретный нормативный акт, номер, дата, пункт или требование
не подтверждаются источниками — не выдумывай их.

Если тема регулируется законодательством РФ, естественно упоминай
подтверждённые нормативные правовые акты там, где они помогают объяснить
практическое требование или порядок действий.

Корректно различай федеральные законы, постановления Правительства РФ,
приказы федеральных органов, правила, санитарные правила и технические регламенты.

Не публикуй ссылки, URL, названия сайтов, номера источников и сноски.

Ориентир объёма — 2800–3200 символов с пробелами.
Не превышай 3200 символов с пробелами.

Внутри текста не создавай подзаголовки.

Начало — 1–2 содержательных абзаца с рабочей ситуацией или проблемой.
Основная часть — последовательное практическое объяснение темы.

В середине статьи естественно и только один раз напиши:
«Меня зовут Николай Бойков, я генеральный директор ООО „Спецконс“.»

В конце сделай спокойный практический вывод.
Допускаются 2–4 коротких пункта с символом • без отдельного заголовка.
Последняя строка — короткий вопрос читателю по теме.

Используй 2–4 уместных эмодзи на весь материал.
Подходящие примеры: ⚠️ 📄 🔎 👥 🛡️ ✅ 📌
Не добавляй эмодзи в заголовок и не ставь их в каждом абзаце.

Не используй Markdown и HTML.
Категорически запрещено использовать символ звёздочки * вообще.
Для списков используй только символ •.

ФОРМАТ ОТВЕТА

ЗАГОЛОВОК: <один обычный заголовок до 120 символов без эмодзи и форматирования>

ТЕКСТ:
<готовая расширенная статья>

Не добавляй никаких пояснений до или после статьи.
""".strip()

SYNCBOT_SYSTEM_PROMPT = """
Ты пишешь короткий законченный экспертный материал для Telegram.

Автор — Николай Бойков, генеральный директор ООО «Спецконс».

Пиши спокойно, профессионально, понятно и по-человечески.
Не используй канцелярит, запугивание, рекламные лозунги, повторения
и конструкции «не просто ..., а ...», «не только ..., но и ...».

Используй только сведения, которые можно подтвердить предоставленными источниками.

Если тема регулируется законодательством РФ, естественно упоминай
подтверждённые нормативные правовые акты там, где это действительно помогает
объяснить требование, обязанность или порядок действий.

Не выдумывай номера нормативных актов, даты, сроки, штрафы и требования.

Не публикуй ссылки, URL, названия сайтов, номера источников и сноски,
кроме обязательной ссылки на канал Дзена ниже.

Материал должен быть коротким, но законченным.
Выбери одну основную рабочую ситуацию или проблему,
кратко объясни её смысл и дай полезный практический вывод.

Внутри текста не создавай подзаголовки.

Один раз естественно представь автора:
«Меня зовут Николай Бойков, я генеральный директор ООО „Спецконс“.»

Используй 1–3 уместных эмодзи на весь текст.
Подходящие примеры: ⚠️ 📄 🔎 ✅ 🛡️ 📌 👥
Не добавляй эмодзи в заголовок и не ставь их в каждом абзаце.

В конце отдельным коротким абзацем напиши точно:
«Практические разборы — по ссылке в описании канала.»

После этого отдельным абзацем обязательно напиши точно:
«Больше полезных статей вы можете найти на https://dzen.ru/specons»

После этой подписи последней строкой задай короткий вопрос читателю по теме.

Не используй Markdown.
Не используй HTML.
Категорически запрещено использовать символ звёздочки * вообще.
Для списков используй только символ •.

ФОРМАТ ОТВЕТА

ЗАГОЛОВОК: <обычный заголовок до 120 символов без эмодзи и форматирования>

ТЕКСТ:
<законченный текст>

Не добавляй никаких комментариев до или после материала.
""".strip()

class YandexGPTClient:
    def __init__(self, folder_id: str, api_key: str | None = None, sa_key_file: str | None = None):
        self.folder_id = folder_id
        self.api_key = api_key
        self.sa_key_file = sa_key_file
        self._iam_token = None
        self._iam_expires_at = 0.0
        self._ssl = _ssl_context()

    def _get_iam_token_sync(self) -> str:
        if self._iam_token and time.time() < self._iam_expires_at - 60:
            return self._iam_token
        if not self.sa_key_file:
            raise RuntimeError("Не задан YC_SA_KEY_FILE")
        data = json.loads(Path(self.sa_key_file).read_text(encoding="utf-8"))
        now = int(time.time())
        encoded = jwt.encode(
            {"aud": IAM_TOKEN_URL, "iss": data["service_account_id"], "iat": now, "exp": now + 360},
            data["private_key"],
            algorithm="PS256",
            headers={"kid": data["id"]},
        )
        with httpx.Client(verify=self._ssl, trust_env=False, http1=True, http2=False, timeout=30) as client:
            response = client.post(IAM_TOKEN_URL, json={"jwt": encoded})
        if response.status_code >= 400:
            raise RuntimeError(f"IAM HTTP {response.status_code}: {response.text}")
        result = response.json()
        self._iam_token = result["iamToken"]
        self._iam_expires_at = time.time() + 3500
        return self._iam_token

    async def auth_header(self) -> str:
        if self.api_key:
            return f"Api-Key {self.api_key}"
        token = await asyncio.to_thread(self._get_iam_token_sync)
        return f"Bearer {token}"

    def _complete_sync(self, auth: str, system_prompt: str, user_prompt: str, timeout_seconds: float = 180) -> str:
        body = {
            "modelUri": f"gpt://{self.folder_id}/yandexgpt/latest",
            "completionOptions": {"stream": False, "temperature": 0.35, "maxTokens": "3200"},
            "messages": [
                {"role": "system", "text": system_prompt},
                {"role": "user", "text": user_prompt},
            ],
        }
        with httpx.Client(verify=self._ssl, trust_env=False, http1=True, http2=False, timeout=float(timeout_seconds)) as client:
            response = client.post(
                COMPLETION_URL,
                headers={"Authorization": auth, "Content-Type": "application/json"},
                json=body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"YandexGPT HTTP {response.status_code}: {response.text}")
        data = response.json()

        result = data.get("result") or {}

        alternatives = (
            result.get("alternatives")
            or data.get("alternatives")
        )

        if not alternatives:
            raise RuntimeError(
                f"YandexGPT вернул неожиданный ответ: {data}"
            )

        text = (
            alternatives[0]
            .get("message", {})
            .get("text", "")
            .strip()
        )

        if not text:
            raise RuntimeError(
                "YandexGPT вернул пустой текст"
            )

        # -------------------------------------------------
        # Фактический usage YandexGPT
        # -------------------------------------------------

        usage = (
            result.get("usage")
            or data.get("usage")
            or {}
        )

        def usage_int(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)

                if value is None:
                    continue

                try:
                    return max(
                        int(float(value)),
                        0,
                    )
                except (TypeError, ValueError):
                    continue

            return 0

        input_tokens = usage_int(
            "inputTextTokens",
            "inputTokens",
            "promptTokens",
            "input_tokens",
            "prompt_tokens",
        )

        output_tokens = usage_int(
            "completionTokens",
            "outputTextTokens",
            "outputTokens",
            "completion_tokens",
            "output_tokens",
        )

        cached_tokens = usage_int(
            "cachedTextTokens",
            "cachedTokens",
            "inputCachedTokens",
            "cachedInputTokens",
            "cached_tokens",
            "input_cached_tokens",
        )

        # Некоторые версии API кладут информацию
        # о кешированных токенах во вложенный объект.
        if cached_tokens == 0:
            details = (
                usage.get("inputTextTokensDetails")
                or usage.get("inputTokensDetails")
                or usage.get("promptTokensDetails")
                or {}
            )

            if isinstance(details, dict):
                for key in (
                    "cachedTokens",
                    "cachedTextTokens",
                    "cached_tokens",
                ):
                    value = details.get(key)

                    if value is None:
                        continue

                    try:
                        cached_tokens = max(
                            int(float(value)),
                            0,
                        )
                        break
                    except (TypeError, ValueError):
                        pass

        # Fallback: если API вернул total,
        # но не вернул output отдельно.
        if output_tokens == 0:
            total_tokens = usage_int(
                "totalTokens",
                "total_tokens",
            )

            if (
                total_tokens > 0
                and input_tokens > 0
                and total_tokens >= input_tokens
            ):
                output_tokens = (
                    total_tokens
                    - input_tokens
                )

        # Учёт расходов НЕ должен ломать генерацию,
        # даже если база временно недоступна.
        try:
            record_gpt(
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                model="yandexgpt/latest",
                metadata={
                    "usage_raw": usage,
                },
            )
        except Exception:
            pass

        return text

    @staticmethod
    def _cleanup(text: str) -> str:
        if not text:
            return ""

        text = text.replace("\r\n", "\n")
        text = re.sub(r"(?m)^\s*\*+\s+", "• ", text)
        text = re.sub(r"(?m)^\s*-\s+", "• ", text)
        text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)

        text = text.replace("**", "")
        text = text.replace("*", "")
        text = text.replace("```", "")
        text = text.replace("`", "")
        text = text.replace("__", "")
        text = text.replace("~~", "")

        text = re.sub(r"</?[^>]+>", "", text)
        text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)

        return text.strip()


    def _parse(cls, text: str) -> tuple[str, str]:
        text = cls._cleanup(text)
        m_body = re.search(r"(?is)\bТЕКСТ:\s*(.+)$", text)
        if not m_body:
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if not lines:
                raise RuntimeError("Не удалось разобрать ответ YandexGPT")
            return lines[0], "\n".join(lines[1:]).strip()
        before = text[:m_body.start()]
        title = re.sub(r"(?is)^\s*ЗАГОЛОВОК:\s*", "", before).strip()
        body = cls._cleanup(m_body.group(1))
        first = re.sub(r"<[^>]+>", "", body.split("\n", 1)[0]).strip()
        if first.casefold() == title.casefold():
            body = body.split("\n", 1)[1].lstrip() if "\n" in body else ""
        return re.sub(r"<[^>]+>", "", title).strip(), body

    async def generate_syncbot_long_article_from_sources(
        self,
        topic: str,
        sources: list[dict[str, Any]],
        max_chars: int = 3200,
    ) -> tuple[str, str]:
        blocks = []
        for i, src in enumerate(sources[:8], 1):
            blocks.append(
                f"ИСТОЧНИК {i}\nНазвание: {src.get('title','')}\n"
                f"URL: {src.get('url','')}\nФрагмент: {src.get('snippet','')}"
            )

        user_prompt = (
            f"ТЕМА СТАТЬИ: {topic}\n"
            "\nИСТОЧНИКИ ДЛЯ ПРОВЕРКИ ФАКТОВ:\n\n"
            + "\n\n".join(blocks)
            + f"\n\nТекст после метки «ТЕКСТ:» — не более {max_chars} "
              "символов С ПРОБЕЛАМИ. Материал должен быть законченным."
        )

        auth = await self.auth_header()
        raw = await asyncio.to_thread(
            self._complete_sync,
            auth,
            SYNCBOT_LONG_SYSTEM_PROMPT,
            user_prompt,
        )
        title, body = self._parse(raw)

        title = " ".join(title.split())[:120].rstrip(" ,;:-")
        body = self._cleanup(body)

        if len(body) > max_chars:
            shortened = body[:max_chars].rstrip()
            sentence_end = max(
                shortened.rfind("."),
                shortened.rfind("!"),
                shortened.rfind("?"),
            )
            if sentence_end >= int(max_chars * 0.75):
                shortened = shortened[:sentence_end + 1].rstrip()
            else:
                shortened = shortened.rsplit(" ", 1)[0].rstrip() + "…"
            body = shortened

        return title, body

    async def generate_syncbot_article_from_sources(
        self,
        topic: str,
        sources: list[dict[str, Any]],
        max_chars: int = 820,
        system_prompt: str | None = None,
    ) -> tuple[str, str]:
        blocks = []
        for i, src in enumerate(sources[:6], 1):
            blocks.append(
                f"ИСТОЧНИК {i}\nНазвание: {src.get('title','')}\n"
                f"URL: {src.get('url','')}\nФрагмент: {src.get('snippet','')}"
            )

        user_prompt = (
            f"ТЕМА ПУБЛИКАЦИИ: {topic}\n"
            "\nИСТОЧНИКИ ДЛЯ ПРОВЕРКИ ФАКТОВ:\n\n"
            + "\n\n".join(blocks)
            + f"\n\nТЕКСТ после метки «ТЕКСТ:» — не более {max_chars} символов "
              "с пробелами. Материал обязан быть законченным, не обрывай "
              "последнее предложение."
        )

        auth = await self.auth_header()
        raw = await asyncio.to_thread(
            self._complete_sync,
            auth,
            system_prompt or SYNCBOT_SYSTEM_PROMPT,
            user_prompt,
        )
        title, body = self._parse(raw)

        title = " ".join(title.split())[:120].rstrip(" ,;:-")
        body = self._cleanup(body)

        # Жёсткая страховка на случай, если модель превысила лимит.
        if len(body) > max_chars:
            shortened = body[:max_chars].rstrip()
            sentence_end = max(
                shortened.rfind("."),
                shortened.rfind("!"),
                shortened.rfind("?"),
            )
            if sentence_end >= int(max_chars * 0.68):
                shortened = shortened[:sentence_end + 1].rstrip()
            else:
                shortened = shortened.rsplit(" ", 1)[0].rstrip() + "…"
            body = shortened

        return title, body


    async def select_article_subtopic(
        self,
        topic: str,
        sources: list[dict[str, Any]],
        used_subtopics: list[str],
    ) -> str:
        """
        Выбирает одну конкретную подтему
        для обычной плановой статьи.

        Вход:
        - широкая родительская тема;
        - актуальная поисковая выдача;
        - уже использованные подтемы.

        Выход:
        - одна новая конкретная подтема.
        """

        source_blocks = []

        for i, src in enumerate(
            sources[:12],
            1,
        ):
            source_blocks.append(
                f"ИСТОЧНИК {i}\n"
                f"Название: "
                f"{src.get('title', '')}\n"
                f"Фрагмент: "
                f"{src.get('snippet', '')}"
            )

        used = [
            " ".join(
                str(value).split()
            )
            for value in used_subtopics[:80]
            if str(value).strip()
        ]

        used_text = (
            "\n".join(
                f"- {value}"
                for value in used
            )
            if used
            else "Нет."
        )

        system_prompt = """
Ты редактор экспертного информационного канала.

Тебе дана широкая тема и актуальная поисковая
выдача по ней. Нужно выбрать ОДНУ конкретную
подтему для следующей статьи.

Правила выбора:

1. Сначала выбирай наиболее актуальную и
практически значимую подтему, которая явно
подтверждается представленными источниками.

2. Актуальность определяй по совокупности:
новизны вопроса, изменений требований,
новой практики, текущих проблем, частоты
появления вопроса в источниках и положения
результатов в поисковой выдаче.

3. Не придумывай событие, изменение закона,
дату или новый нормативный акт, если этого
не видно из источников.

4. Подтема должна быть значительно уже
родительской темы. Она должна позволять
написать одну конкретную статью, а не общий
обзор всей области.

5. Уже использованные подтемы запрещены.
Не выбирай не только точное совпадение,
но и смысловой дубль уже использованной
подтемы с немного другими словами.

6. Если наиболее заметная подтема уже была
использована, выбирай следующую по
актуальности и практической значимости.

7. Если использованы несколько основных
подтем, продолжай двигаться к менее
очевидным, но всё ещё полезным и
подтверждаемым источниками аспектам.

8. Не выбирай слишком общие формулировки
вроде «основные требования», «что нужно
знать», «важные правила», если в источниках
можно выделить более конкретный вопрос.

Ответь ТОЛЬКО названием одной подтемы.
Без пояснений, нумерации, кавычек,
меток «ПОДТЕМА» и дополнительного текста.
"""

        user_prompt = (
            f"ШИРОКАЯ ТЕМА:\n{topic}\n\n"
            "УЖЕ ИСПОЛЬЗОВАННЫЕ ПОДТЕМЫ:\n"
            f"{used_text}\n\n"
            "АКТУАЛЬНАЯ ПОИСКОВАЯ ВЫДАЧА:\n\n"
            + "\n\n".join(source_blocks)
        )

        auth = await self.auth_header()

        raw = await asyncio.to_thread(
            self._complete_sync,
            auth,
            system_prompt,
            user_prompt,
        )

        candidate = self._cleanup(
            raw
        )

        if candidate:
            candidate = (
                candidate.splitlines()[0]
                .strip()
            )

        candidate = re.sub(
            r"(?i)^\s*(?:подтема|тема)"
            r"\s*:\s*",
            "",
            candidate,
        )

        candidate = candidate.strip(
            ' "\'«».,;:-'
        )

        candidate = " ".join(
            candidate.split()
        )

        if not candidate:
            raise RuntimeError(
                "YandexGPT не выбрал подтему"
            )

        return candidate[:220].rstrip(
            " ,;:-"
        )


    async def generate_article_from_sources(
        self, topic: str, sources: list[dict[str, Any]],
        subtopic: str | None = None, max_chars: int = 4500,
        system_prompt: str | None = None,
    ) -> tuple[str, str]:
        blocks = []
        for i, src in enumerate(sources[:8], 1):
            blocks.append(
                f"ИСТОЧНИК {i}\nНазвание: {src.get('title','')}\n"
                f"URL: {src.get('url','')}\nФрагмент: {src.get('snippet','')}"
            )
        user_prompt = (
            f"ТЕМА СТАТЬИ: {topic}\n"
            + (f"ПОДТЕМА/АКЦЕНТ: {subtopic}\n" if subtopic else "")
            + "\nИСТОЧНИКИ ДЛЯ ПРОВЕРКИ ФАКТОВ:\n\n"
            + "\n\n".join(blocks)
            + f"\n\nОриентир верхней границы текста: {max_chars} символов."
        )
        auth = await self.auth_header()
        raw = await asyncio.to_thread(
            self._complete_sync,
            auth,
            (system_prompt or ARTICLE_SYSTEM_PROMPT),
            user_prompt,
        )
        return self._parse(raw)
