import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as moderation


def fake_gigachat(answer: str, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth"):
            assert request.headers["Authorization"] == "Basic test-key"
            assert b"scope=GIGACHAT_API_PERS" in request.content
            return httpx.Response(200, json={"access_token": "tok", "expires_at": 4102444800000})
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"model": "GigaChat", "choices": [{"message": {"content": answer}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return moderation.GigaChat("test-key", "GIGACHAT_API_PERS", "GigaChat", client=client)


def test_page_and_status_without_key(monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY", raising=False)
    monkeypatch.setenv("APP_BASE_PATH", "/apps/redsms-moderation")
    client = TestClient(moderation.create_app())
    page = client.get("/apps/redsms-moderation/")
    assert page.status_code == 200 and "Проверка сообщений" in page.text
    assert client.get("/apps/redsms-moderation/api/status").json()["llm"] is False
    reply = client.post("/apps/redsms-moderation/api/llm-check", json={"text": "привет"})
    assert reply.status_code == 503


def test_llm_check_masks_contacts_and_normalizes_answer(monkeypatch):
    monkeypatch.delenv("APP_BASE_PATH", raising=False)
    calls = []
    answer = 'Вот ответ: {"verdict":"REJECT","type":"advertising","categories":["drugs","NOPE"],"reason":"Реклама веществ.","confidence":1.7}'
    client = TestClient(moderation.create_app(fake_gigachat(answer, calls)))
    reply = client.post("/api/llm-check", json={"text": "Пиши +7 (912) 345-67-89 или a.b@mail.ru", "sender": "X", "type": "service"})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["verdict"] == "reject" and data["type"] == "advertising"
    assert data["categories"] == [{"code": "DRUGS", "label": "Наркотические и психоактивные вещества"}]
    assert data["confidence"] == 1.0
    sent = json.loads(calls[-1].content)["messages"][1]["content"]
    assert "912" not in sent and "mail.ru" not in sent
    assert "[телефон]" in sent and "[почта]" in sent and "сервисный" in sent


def test_token_is_reused(monkeypatch):
    calls = []
    llm = fake_gigachat('{"verdict":"pass","type":"auth","categories":[],"reason":"","confidence":0.9}', calls)
    async def twice():
        await llm.check("Код 1234", "", "auth")
        await llm.check("Код 5678", "", "auth")
    asyncio.run(twice())
    assert sum(c.url.path.endswith("/oauth") for c in calls) == 1


def test_bad_answer_is_reported():
    client = TestClient(moderation.create_app(fake_gigachat("не знаю", [])))
    reply = client.post("/api/llm-check", json={"text": "текст"})
    assert reply.status_code == 502 and "JSON" in reply.json()["detail"]


def test_mask_keeps_codes():
    assert moderation.mask("Ваш код 4821") == "Ваш код 4821"
    assert moderation.mask("Звоните 8 800 555-35-35") == "Звоните [телефон]"
