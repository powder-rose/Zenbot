from __future__ import annotations
import asyncio, base64, ssl, time
from collections.abc import Awaitable, Callable
import httpx, truststore
from ai_usage import record_image

GENERATE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{operation_id}"

def _ssl_context():
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try: ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    except AttributeError: pass
    return ctx

class YandexArtClient:
    def __init__(self, folder_id: str, get_auth_header: Callable[[], Awaitable[str]]):
        self.folder_id = folder_id
        self.get_auth_header = get_auth_header
        self._ssl = _ssl_context()

    def _generate_sync(self, auth: str, prompt: str, aspect_ratio: tuple[int,int]) -> bytes:
        body = {
            "modelUri": f"art://{self.folder_id}/yandex-art/latest",
            "generationOptions": {
                "mimeType": "image/jpeg",
                "aspectRatio": {"widthRatio": str(aspect_ratio[0]), "heightRatio": str(aspect_ratio[1])},
            },
            "messages": [{"text": prompt, "weight": "1"}],
        }
        headers = {"Authorization": auth, "Content-Type": "application/json"}
        with httpx.Client(verify=self._ssl, trust_env=False, http1=True, http2=False, timeout=90) as client:
            response = client.post(GENERATE_URL, headers=headers, json=body)
            if response.status_code >= 400:
                raise RuntimeError(f"YandexART HTTP {response.status_code}: {response.text}")
            op_id = response.json().get("id")
            if not op_id:
                raise RuntimeError(f"YandexART не вернул id операции: {response.text}")
            deadline = time.time() + 180
            while time.time() < deadline:
                op = client.get(OPERATION_URL.format(operation_id=op_id), headers={"Authorization": auth})
                if op.status_code >= 400:
                    raise RuntimeError(f"YandexART operation HTTP {op.status_code}: {op.text}")
                data = op.json()
                if data.get("done"):
                    if data.get("error"):
                        raise RuntimeError(f"YandexART error: {data['error']}")
                    image_b64 = (data.get("response") or {}).get("image")
                    if not image_b64:
                        raise RuntimeError(
                            f"YandexART не вернул изображение: {data}"
                        )

                    image_bytes = base64.b64decode(
                        image_b64
                    )

                    # Учёт не должен ломать генерацию.
                    try:
                        record_image(
                            model="yandex-art/latest",
                            metadata={
                                "aspect_ratio":
                                    f"{aspect_ratio[0]}:{aspect_ratio[1]}",
                            },
                        )
                    except Exception:
                        pass

                    return image_bytes
                time.sleep(3)
        raise TimeoutError("YandexART не завершил генерацию за 180 секунд")

    async def generate_image(self, prompt: str, aspect_ratio: tuple[int,int] = (1,1)) -> bytes:
        prompt = " ".join(prompt.split())
        if len(prompt) > 480:
            prompt = prompt[:480].rsplit(" ",1)[0].rstrip()
        auth = await self.get_auth_header()
        return await asyncio.to_thread(self._generate_sync, auth, prompt, aspect_ratio)
