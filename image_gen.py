from __future__ import annotations

import asyncio
import base64
from collections.abc import (
    Awaitable,
    Callable,
)

import openai

from ai_usage import record_image


ALICE_ART_MODEL = (
    "aliceai-image-art-3.0/latest"
)

YANDEX_AI_BASE_URL = (
    "https://ai.api.cloud.yandex.net/v1/"
)


class YandexArtClient:
    """
    Генерация изображений через актуальный
    OpenAI-compatible API Yandex AI Studio.

    Публичный интерфейс класса оставляем прежним,
    поэтому остальной проект менять не требуется.
    """

    def __init__(
        self,
        folder_id: str,
        get_auth_header: Callable[
            [],
            Awaitable[str],
        ],
    ):
        self.folder_id = str(
            folder_id
        ).strip()

        self.get_auth_header = (
            get_auth_header
        )


    @staticmethod
    def _auth_token(
        auth: str,
    ) -> str:
        """
        Старый общий auth_header() проекта возвращает:

            Api-Key <key>

        либо:

            Bearer <iam_token>

        OpenAI SDK самостоятельно добавляет Bearer,
        поэтому передаём ему только сам секрет.
        """

        value = str(
            auth or ""
        ).strip()

        for prefix in (
            "Api-Key ",
            "Bearer ",
        ):
            if value.startswith(
                prefix
            ):
                value = value[
                    len(prefix):
                ].strip()

                break

        if not value:
            raise RuntimeError(
                "YandexART: пустая авторизация"
            )

        return value


    @staticmethod
    def _image_size(
        aspect_ratio: tuple[int, int],
    ) -> str:
        """
        Преобразуем существующий интерфейс проекта
        aspect_ratio в размер изображения.

        Основной проект использует 4:3.
        """

        width = int(
            aspect_ratio[0]
        )

        height = int(
            aspect_ratio[1]
        )

        if width <= 0 or height <= 0:
            return "1024x1024"

        known = {
            (1, 1): "1024x1024",
            (4, 3): "1024x768",
            (3, 4): "768x1024",
            (16, 9): "1024x576",
            (9, 16): "576x1024",
        }

        return known.get(
            (width, height),
            "1024x1024",
        )


    def _generate_sync(
        self,
        auth: str,
        prompt: str,
        aspect_ratio: tuple[int, int],
    ) -> bytes:

        api_key = self._auth_token(
            auth
        )

        client = openai.OpenAI(
            api_key=api_key,
            base_url=YANDEX_AI_BASE_URL,
            project=self.folder_id,
            timeout=180.0,
        )

        model = (
            f"art://{self.folder_id}/"
            f"{ALICE_ART_MODEL}"
        )

        size = self._image_size(
            aspect_ratio
        )

        response = client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
        )

        if (
            not response.data
            or not response.data[0].b64_json
        ):
            raise RuntimeError(
                "Alice AI ART "
                "не вернула изображение"
            )

        image_bytes = base64.b64decode(
            response.data[0].b64_json
        )

        if not image_bytes:
            raise RuntimeError(
                "Alice AI ART вернула "
                "пустое изображение"
            )

        try:
            record_image(
                model=ALICE_ART_MODEL,
                metadata={
                    "aspect_ratio": (
                        f"{aspect_ratio[0]}"
                        f":{aspect_ratio[1]}"
                    ),
                    "size": size,
                },
            )
        except Exception:
            # Учёт usage не должен ломать
            # саму генерацию изображения.
            pass

        return image_bytes


    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: tuple[int, int] = (
            4,
            3,
        ),
    ) -> bytes:

        prompt = " ".join(
            str(
                prompt or ""
            ).split()
        )

        if not prompt:
            raise RuntimeError(
                "YandexART: пустой prompt"
            )

        # Сохраняем существующую защиту проекта.
        if len(prompt) > 500:
            prompt = prompt[
                :500
            ].rstrip()

        auth = await self.get_auth_header()

        return await asyncio.to_thread(
            self._generate_sync,
            auth,
            prompt,
            aspect_ratio,
        )
