from __future__ import annotations
import asyncio, base64, html, re, ssl, xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
import httpx, truststore

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

def _ssl_context():
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try: ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    except AttributeError: pass
    return ctx

def _plain(value: str | None) -> str:
    if not value: return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())

class YandexSearchClient:
    def __init__(self, folder_id: str, get_auth_header: Callable[[], Awaitable[str]]):
        self.folder_id = folder_id
        self.get_auth_header = get_auth_header
        self._ssl = _ssl_context()

    def _search_sync(self, auth: str, query: str, max_results: int):
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query,
                "familyMode": "FAMILY_MODE_MODERATE",
                "fixTypoMode": "FIX_TYPO_MODE_ON",
            },
            "sortSpec": {"sortMode": "SORT_MODE_BY_RELEVANCE", "sortOrder": "SORT_ORDER_DESC"},
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": str(max(5, min(max_results,20))), "docsInGroup": "1"},
            "maxPassages": "3",
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
            "l10n": "LOCALIZATION_RU",
        }
        with httpx.Client(verify=self._ssl, trust_env=False, http1=True, http2=False, timeout=60) as client:
            response = client.post(
                SEARCH_URL,
                headers={"Authorization": auth, "Content-Type": "application/json"},
                json=body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Yandex Search HTTP {response.status_code}: {response.text}")
        data = response.json()
        raw = data.get("rawData")
        if not raw:
            raise RuntimeError(f"Yandex Search не вернул rawData: {data}")
        xml_text = base64.b64decode(raw).decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)
        results = []
        for doc in root.findall(".//doc"):
            title_node = doc.find("title")
            title = _plain("".join(title_node.itertext()) if title_node is not None else "")
            url = _plain(doc.findtext("url"))
            passages = []
            pn = doc.find("passages")
            if pn is not None:
                for p in pn.findall("passage"):
                    passages.append(_plain("".join(p.itertext())))
            if url:
                results.append({"title": title or url, "url": url, "snippet": " ".join(passages)})
            if len(results) >= max_results:
                break
        return results

    async def search(self, query: str, max_results: int = 5):
        auth = await self.get_auth_header()
        return await asyncio.to_thread(self._search_sync, auth, query, max_results)
