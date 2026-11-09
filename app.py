"""REDSMS · Проверка сообщений.

Страница модерации (static/index.html) и второе мнение языковой модели GigaChat.
Правила работают в браузере, как и раньше. Сервер нужен только для запроса к GigaChat:
ключ хранится в переменной окружения и в браузер не попадает.

Переменные окружения:
  GIGACHAT_AUTH_KEY   ключ авторизации из личного кабинета GigaChat (без него модель выключена)
  GIGACHAT_SCOPE      GIGACHAT_API_PERS (физлица, по умолчанию), GIGACHAT_API_B2B или GIGACHAT_API_CORP
  GIGACHAT_MODEL      модель, по умолчанию GigaChat (младшая)
  APP_BASE_PATH       префикс адреса, его передаёт сервис «Проекты»
  PORT                порт, по умолчанию 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
CA_FILE = HERE / "certs" / "russian_trusted_root_ca.pem"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

CATEGORIES = {
    "TOBACCO": "Табак и никотин",
    "DRUGS": "Наркотические и психоактивные вещества",
    "EXPLOSIVES": "Взрывчатые вещества",
    "ORGANS": "Купля-продажа органов и тканей",
    "ABORTION": "Прерывание беременности",
    "ACADEMIC": "Работы для аттестации",
    "CRYPTO": "Цифровые валюты и права",
    "MEDICAL": "Медицинские услуги и продукция",
    "FINANCE": "Финансовые услуги",
    "ALCOHOL": "Алкоголь",
    "CERTIFICATION": "Оценка соответствия",
    "WEAPONS": "Оружие и опасные товары",
    "GAMBLING": "Азартные игры и ставки",
    "FRAUD": "Признаки мошенничества или фишинга",
    "IMPERSONATION": "Выдаёт себя за другую организацию",
}
VERDICTS = {"pass", "review", "reject"}
TYPES = {"auth", "service", "advertising", "unknown"}

SYSTEM_PROMPT = """Ты помогаешь модератору SMS-рассылок в России. Оцени одно SMS по закону «О рекламе» (в том числе статья 7 — запрещённая реклама) и на признаки мошенничества.

Верни только JSON без пояснений вокруг:
{"verdict": "pass|review|reject", "type": "auth|service|advertising|unknown", "categories": ["КОД", ...], "reason": "одно-два предложения по-русски", "confidence": 0.0-1.0}

verdict:
- pass — обычное сервисное или авторизационное сообщение, рисков нет;
- review — есть реклама регулируемых товаров или услуг, ссылка, неясная цель или сомнение;
- reject — явная реклама запрещённого (наркотики, взрывчатка, органы, заказ дипломов), явное мошенничество или фишинг.

type — фактическое назначение текста, а не заявленное клиентом:
- auth — код подтверждения или входа;
- service — уведомление о заказе, записи, оплате, доставке, балансе;
- advertising — продвижение товара, услуги, акции, скидки;
- unknown — непонятно.

categories — коды только из списка, пустой список если ничего не подходит:
""" + "\n".join(f"{code} — {label}" for code, label in CATEGORIES.items()) + """

Упоминание темы ещё не нарушение: напоминание о записи к стоматологу — это service, а не реклама медицины. Не придумывай фактов, которых нет в тексте. Номера телефонов и почта в тексте заменены на [телефон] и [почта]."""

TYPE_NAMES = {"auth": "авторизационный", "service": "сервисный", "advertising": "рекламный", "unknown": "не определён"}

PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8|7)?[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def mask(text: str) -> str:
    """Номера телефонов и почту в GigaChat не отправляем."""
    return PHONE_RE.sub("[телефон]", EMAIL_RE.sub("[почта]", text))


def parse_answer(content: str) -> dict:
    """Достаёт JSON из ответа модели и приводит поля к допустимым значениям."""
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("модель ответила не в формате JSON")
    data = json.loads(match.group(0))
    verdict = str(data.get("verdict", "review")).lower()
    kind = str(data.get("type", "unknown")).lower()
    categories = [str(c).upper() for c in data.get("categories") or [] if str(c).upper() in CATEGORIES]
    try:
        confidence = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "verdict": verdict if verdict in VERDICTS else "review",
        "type": kind if kind in TYPES else "unknown",
        "categories": [{"code": c, "label": CATEGORIES[c]} for c in dict.fromkeys(categories)],
        "reason": str(data.get("reason") or "").strip()[:600],
        "confidence": round(confidence, 2),
    }


class GigaChat:
    def __init__(self, auth_key: str, scope: str, model: str, client: httpx.AsyncClient | None = None):
        self.auth_key = auth_key.strip()
        self.scope = scope
        self.model = model
        self.client = client or httpx.AsyncClient(verify=str(CA_FILE), timeout=30)
        self.token = ""
        self.token_expires = 0.0
        self.token_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(3)

    async def access_token(self) -> str:
        async with self.token_lock:
            if self.token and time.time() < self.token_expires - 60:
                return self.token
            reply = await self.client.post(
                OAUTH_URL,
                headers={
                    "Authorization": f"Basic {self.auth_key}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self.scope},
            )
            if reply.status_code in (400, 401, 403):
                raise PermissionError("GigaChat не принял ключ: проверьте GIGACHAT_AUTH_KEY и GIGACHAT_SCOPE")
            reply.raise_for_status()
            data = reply.json()
            self.token = data["access_token"]
            self.token_expires = data.get("expires_at", 0) / 1000 or time.time() + 25 * 60
            return self.token

    async def check(self, text: str, sender: str, declared: str) -> dict:
        user = f"Имя отправителя: {sender or 'не указано'}\nЗаявленный тип: {TYPE_NAMES.get(declared, 'не определён')}\nТекст SMS:\n{mask(text)}"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": 300,
        }
        async with self.slots:
            for attempt in range(2):
                token = await self.access_token()
                reply = await self.client.post(
                    CHAT_URL, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, json=body
                )
                if reply.status_code == 401 and attempt == 0:
                    self.token = ""
                    continue
                if reply.status_code == 402:
                    raise RuntimeError("закончился пакет токенов GigaChat")
                if reply.status_code == 429:
                    raise RuntimeError("GigaChat ограничил частоту запросов, повторите позже")
                reply.raise_for_status()
                break
        data = reply.json()
        result = parse_answer(data["choices"][0]["message"]["content"])
        result["model"] = data.get("model", self.model)
        return result


class CheckRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    sender: str = Field(default="", max_length=100)
    type: str = Field(default="unknown", max_length=20)


def create_app(llm: GigaChat | None = None) -> FastAPI:
    base = os.environ.get("APP_BASE_PATH", "").rstrip("/")
    if llm is None and os.environ.get("GIGACHAT_AUTH_KEY", "").strip():
        llm = GigaChat(
            os.environ["GIGACHAT_AUTH_KEY"],
            os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            os.environ.get("GIGACHAT_MODEL", "GigaChat"),
        )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    @router.get("/")
    async def page() -> FileResponse:
        return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    @router.get("/api/status")
    async def status() -> dict:
        return {"llm": llm is not None, "model": llm.model if llm else None, "provider": "GigaChat"}

    @router.post("/api/llm-check")
    async def llm_check(payload: CheckRequest) -> dict:
        if llm is None:
            raise HTTPException(503, "Модель не подключена: не задан GIGACHAT_AUTH_KEY")
        try:
            return await llm.check(payload.text, payload.sender, payload.type)
        except PermissionError as error:
            raise HTTPException(502, str(error))
        except (RuntimeError, ValueError) as error:
            raise HTTPException(502, f"Модель недоступна: {error}")
        except (httpx.HTTPError, KeyError, json.JSONDecodeError):
            raise HTTPException(502, "Модель недоступна: ошибка связи с GigaChat")

    app.include_router(router, prefix=base)
    if base:
        @app.get(base)
        async def with_slash() -> RedirectResponse:
            return RedirectResponse(base + "/")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)
