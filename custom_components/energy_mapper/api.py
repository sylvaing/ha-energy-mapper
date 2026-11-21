"""Client of the internal HTTP API of the Energy Mapper add-on."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import TIMEOUT, TIMEOUT_ANALYZE


class EnergyMapperError(Exception):
    """Base error of the add-on client."""


class EnergyMapperConnectionError(EnergyMapperError):
    """The add-on cannot be reached (stopped, wrong address, timeout)."""


class EnergyMapperApiError(EnergyMapperError):
    """The add-on answered with an error.

    ``code`` is the add-on's error code (``invalid_day``, ``not_found``,
    ``no_grid``, ``no_data``, ``partial_day``, ``ha_error``, ``internal``,
    ``invalid_settings``...), or ``http_<status>`` / ``invalid_response``.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.status = status
        self.code = code
        self.message = message or code


class EnergyMapperClient:
    """Thin asynchronous wrapper of the add-on API (see the add-on DOCS.md)."""

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self.url = url.rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        timeout: float = TIMEOUT,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                params=params,
                json=json,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                if resp.status >= 400:
                    if isinstance(body, dict) and isinstance(body.get("error"), str):
                        raise EnergyMapperApiError(resp.status, body["error"], str(body.get("message") or ""))
                    raise EnergyMapperApiError(
                        resp.status, f"http_{resp.status}", getattr(resp, "reason", None) or ""
                    )
                if not isinstance(body, dict):
                    raise EnergyMapperApiError(
                        resp.status, "invalid_response", "the add-on did not answer JSON"
                    )
                return body
        except TimeoutError as err:
            raise EnergyMapperConnectionError(f"no answer from {self.url} within {timeout:g} s") from err
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as err:
            raise EnergyMapperConnectionError(f"cannot reach {self.url}: {err}") from err

    async def health(self) -> dict[str, Any]:
        """``{"status": "ok", "version", "last_run", "days", "signatures", "storage_bytes"}``."""
        body = await self._request("GET", "/api/health")
        if body.get("status") != "ok" or "version" not in body:
            raise EnergyMapperApiError(200, "invalid_response", "not an Energy Mapper add-on")
        return body

    async def days(self) -> dict[str, Any]:
        return await self._request("GET", "/api/days")

    async def report(self, day: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/report/{day}")

    async def hourly(self, day: str) -> dict[str, Any]:
        """Hourly kWh per appliance of a day with a curve (add-on 0.4.0+)."""
        return await self._request("GET", f"/api/hourly/{day}")

    async def analyze(self, day: str) -> dict[str, Any]:
        return await self._request("POST", "/api/analyze", params={"day": day}, timeout=TIMEOUT_ANALYZE)

    async def signatures(self) -> dict[str, Any]:
        return await self._request("GET", "/api/signatures")

    async def get_settings(self) -> dict[str, Any]:
        return await self._request("GET", "/api/settings")

    async def put_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", "/api/settings", json=settings)

    async def get_appliances(self) -> dict[str, Any]:
        """``{"appliances": [...]}``: the appliances the add-on holds (0.5.0+)."""
        return await self._request("GET", "/api/appliances")

    async def put_appliances(self, appliances: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request("PUT", "/api/appliances", json={"appliances": appliances})

    async def expect_signature(self, kind: str, band: str, power_w: float, label: str) -> dict[str, Any]:
        """The signature of an appliance declared before it is seen:
        ``{"signature", "created"}``; error ``conflict`` (409) when another
        appliance has that power."""
        return await self._request(
            "POST",
            "/api/signatures",
            json={"kind": kind, "band": band, "power_w": power_w, "label": label},
        )
