from typing import Any, Type

import httpx
from async_lru import alru_cache
from creart import AbstractCreator, CreateTargetInfo, exists_module, it
from tenacity import retry_if_exception_type, retry, wait_random_exponential, stop_after_attempt, before_sleep_log

from src.config import Config
from src.logger import GlobalLogger


class WrapperManagerException(Exception):
    def __init__(self, msg: str):
        self.msg = msg


class StatusData:
    regions: list[str]

    def __init__(self, regions: list[str]):
        self.regions = regions


class WrapperManager:
    _client: httpx.AsyncClient

    async def init(self, url: str):
        self._client = httpx.AsyncClient(base_url=url, timeout=60.0)
        return self

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = await self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise WrapperManagerException(body.get("msg", "unknown wrapper-lite error"))
        return body.get("data", {})

    @alru_cache
    async def status(self) -> StatusData:
        data = await self._request("GET", "/status")
        return StatusData(data.get("regions", []))

    @retry(retry=retry_if_exception_type((httpx.HTTPError, WrapperManagerException)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def m3u8(self, adam_id: str) -> str:
        data = await self._request("GET", "/m3u8", params={"adamId": adam_id})
        return data.get("m3u8", "")

    @retry(retry=retry_if_exception_type((httpx.HTTPError, WrapperManagerException)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def key(self, adam_id: str, uri: str) -> dict[str, Any]:
        return await self._request("GET", "/key", params={"adamId": adam_id, "uri": uri})

    @retry(retry=retry_if_exception_type((httpx.HTTPError, WrapperManagerException)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def lyrics(self, adam_id: str, language: str, region: str) -> str:
        data = await self._request("GET", "/lyrics", params={"adamId": adam_id, "language": language, "syllable": "0"})
        return data.get("lyrics", "")

    @retry(retry=retry_if_exception_type((httpx.HTTPError, WrapperManagerException)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def webPlayback(self, adam_id: str) -> str:
        data = await self._request("GET", "/webplayback", params={"adamId": adam_id})
        return data.get("m3u8", "")

    @retry(retry=retry_if_exception_type((httpx.HTTPError, WrapperManagerException)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def license(self, adam_id: str, challenge: str, kid: str) -> str:
        data = await self._request("POST", "/license", json={
            "adamId": adam_id,
            "challenge": challenge,
            "uri": kid,
        })
        return data.get("license", "")


class WMCreator(AbstractCreator):
    targets = (
        CreateTargetInfo("src.wrapper", "WrapperManager"),
    )

    @staticmethod
    def available() -> bool:
        return exists_module("src.wrapper")

    @staticmethod
    def create(create_type: Type[WrapperManager]) -> WrapperManager:
        return create_type()
