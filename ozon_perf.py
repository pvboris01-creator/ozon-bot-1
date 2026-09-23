import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-performance.ozon.ru"
CLIENT_ID = os.getenv("OZON_PERF_CLIENT_ID")
CLIENT_SECRET = os.getenv("OZON_PERF_CLIENT_SECRET")

_token_cache = {"access_token": None, "expires_at": 0}


async def get_token() -> str:
    """Получить токен. Кэшируется на 30 минут."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/api/client/token",
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 1800)
    return data["access_token"]


async def get_campaigns() -> list:
    """Получить список всех рекламных кампаний."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/api/client/campaign",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            return data
        for key in ("list", "campaigns", "result", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []


async def activate_campaign(campaign_id: int) -> dict:
    """Включить кампанию по её ID."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/api/client/campaign/{campaign_id}/activate",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def deactivate_campaign(campaign_id: int) -> dict:
    """Выключить кампанию по её ID."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/api/client/campaign/{campaign_id}/deactivate",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def get_daily_stats(date_from: str, date_to: str) -> list:
    """
    Дневная статистика по кампаниям. Отдаёт данные и за сегодня.
    Поля: id, title, date, views, clicks, moneySpent, orders, ordersMoney.
    """
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE_URL}/api/client/statistics/daily/json",
            headers=headers,
            params={"dateFrom": date_from, "dateTo": date_to},
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            return data
        for key in ("rows", "result", "items", "list"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []