from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass(slots=True)
class HostedImage:
    image_url: str
    preview_url: str


class ImageHostClient:
    """
    Загружает YandexART-картинку на собственный сайт пользователя.
    Серверный обработчик: zenbot_upload.php.

    Переменные .env:
      IMAGE_UPLOAD_ENDPOINT=https://boykovgroup.ru/zenbot_upload.php
      IMAGE_UPLOAD_TOKEN=секретный_токен
    """

    def __init__(
        self,
        endpoint: str,
        token: str,
        timeout: float = 45.0,
    ):
        self.endpoint = endpoint.strip()
        self.token = token.strip()
        self.timeout = timeout

        if not self.endpoint:
            raise RuntimeError(
                "Не задан IMAGE_UPLOAD_ENDPOINT в .env"
            )

        if not self.token:
            raise RuntimeError(
                "Не задан IMAGE_UPLOAD_TOKEN в .env"
            )

    @classmethod
    def from_env(cls) -> "ImageHostClient":
        return cls(
            endpoint=os.getenv(
                "IMAGE_UPLOAD_ENDPOINT",
                "",
            ),
            token=os.getenv(
                "IMAGE_UPLOAD_TOKEN",
                "",
            ),
        )

    async def upload(
        self,
        image_bytes: bytes,
        title: str,
    ) -> HostedImage:
        if not image_bytes:
            raise RuntimeError(
                "Нельзя загрузить пустое изображение"
            )

        headers = {
            "X-Zenbot-Token": self.token,
        }

        files = {
            "image": (
                "article.jpg",
                image_bytes,
                "image/jpeg",
            )
        }

        data = {
            "title": title[:180],
        }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                self.endpoint,
                headers=headers,
                files=files,
                data=data,
            )

        if response.status_code != 200:
            raise RuntimeError(
                "Сайт не принял изображение: "
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                "Сервер загрузки вернул не JSON: "
                f"{response.text[:500]}"
            ) from exc

        if not payload.get("ok"):
            raise RuntimeError(
                "Ошибка загрузки изображения: "
                f"{payload.get('error', 'неизвестная ошибка')}"
            )

        image_url = str(
            payload.get("image_url", "")
        ).strip()
        preview_url = str(
            payload.get("preview_url", "")
        ).strip()

        if not image_url or not preview_url:
            raise RuntimeError(
                "Сервер не вернул image_url/preview_url"
            )

        return HostedImage(
            image_url=image_url,
            preview_url=preview_url,
        )
