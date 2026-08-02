"""
generators.py
Работа с GigaChat API (Сбер): генерация структуры презентации и наполнение слайдов.
Используется бесплатный тариф GigaChat API (персональный, scope GIGACHAT_API_PERS).

Почему GigaChat вместо Gemini/OpenAI: сервис доступен из России без VPN и без карты
недоступного региона — регистрация происходит через обычный российский аккаунт на
developers.sber.ru/studio.
"""

import json
import logging
import os
import re
import time
import uuid
from typing import List, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("slideforge.generators")

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")  # "Authorization key" из личного кабинета
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# GigaChat использует сертификат Минцифры России, которого обычно нет в системном
# доверенном хранилище. Самый простой рабочий вариант — отключить проверку TLS.
# Если хочешь проверять сертификат честно, скачай "Минцифры Root CA" и укажи путь
# в httpx.AsyncClient(verify="path/to/russian_trusted_root_ca.pem") вместо verify=False.
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() == "true"

_token_cache = {"access_token": None, "expires_at": 0}


async def _get_gigachat_token() -> str:
    """Получает (и кэширует) access_token GigaChat через OAuth2 client_credentials."""
    if not GIGACHAT_AUTH_KEY:
        raise GenerationError(
            "GIGACHAT_AUTH_KEY не задан. Добавь Authorization key из личного кабинета "
            "developers.sber.ru/studio в переменные окружения (.env)."
        )

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 30 > now:
        return _token_cache["access_token"]

    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"scope": GIGACHAT_SCOPE}

    async with httpx.AsyncClient(timeout=30.0, verify=GIGACHAT_VERIFY_SSL) as client:
        try:
            resp = await client.post(GIGACHAT_OAUTH_URL, headers=headers, data=data)
        except httpx.RequestError as exc:
            logger.error("Ошибка сети при получении токена GigaChat: %s", exc)
            raise GenerationError(
                "Не удалось связаться с GigaChat OAuth. Проверь интернет-соединение."
            ) from exc

    if resp.status_code != 200:
        logger.error("GigaChat OAuth вернул ошибку %s: %s", resp.status_code, resp.text)
        raise GenerationError(
            "Не удалось авторизоваться в GigaChat. Проверь GIGACHAT_AUTH_KEY и срок его действия."
        )

    payload = resp.json()
    token = payload.get("access_token")
    expires_at = payload.get("expires_at", now + 1800)
    if not token:
        raise GenerationError("GigaChat не вернул access_token.")

    _token_cache["access_token"] = token
    _token_cache["expires_at"] = expires_at / 1000 if expires_at > 10**12 else expires_at
    return token


class SlideOutline(BaseModel):
    title: str
    key_points: List[str] = Field(default_factory=list)


class SlideContent(BaseModel):
    title: str
    bullets: List[str] = Field(default_factory=list)
    image_keywords: List[str] = Field(default_factory=list)
    speaker_notes: Optional[str] = None


class GenerationError(Exception):
    """Ошибка генерации контента (например, AI не ответил)."""


def _extract_json(text: str) -> str:
    """Достаём JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    # На случай, если модель добавила лишний текст до/после JSON
    start = text.find("[") if "[" in text else text.find("{")
    end = max(text.rfind("]"), text.rfind("}"))
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text


async def _call_gigachat(prompt: str, temperature: float = 0.7) -> str:
    token = await _get_gigachat_token()

    payload = {
        "model": GIGACHAT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты помощник, который отвечает СТРОГО валидным JSON без markdown-разметки, "
                    "без ```-оберток и без пояснений до/после JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "n": 1,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0, verify=GIGACHAT_VERIFY_SSL) as client:
        try:
            resp = await client.post(GIGACHAT_CHAT_URL, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Ошибка сети при обращении к GigaChat: %s", exc)
            raise GenerationError(
                "Не удалось связаться с GigaChat API. Проверь интернет-соединение."
            ) from exc

    if resp.status_code == 429:
        raise GenerationError(
            "Превышен бесплатный лимит запросов к GigaChat. Попробуй через минуту."
        )
    if resp.status_code == 401:
        # Токен мог протухнуть между запросами — сбрасываем кэш, чтобы след. запрос обновил его
        _token_cache["access_token"] = None
        raise GenerationError("GigaChat отклонил авторизацию. Попробуй сгенерировать ещё раз.")
    if resp.status_code != 200:
        logger.error("GigaChat вернул ошибку %s: %s", resp.status_code, resp.text)
        raise GenerationError(f"GigaChat API вернул ошибку {resp.status_code}.")

    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        logger.error("Неожиданный формат ответа GigaChat: %s", data)
        raise GenerationError("AI вернул пустой или некорректный ответ.") from exc

    if not text.strip():
        raise GenerationError("AI не ответил. Попробуй ещё раз.")

    return text


async def generate_outline(
    topic: str, slide_count: int, language: str = "ru"
) -> List[SlideOutline]:
    """Этап 1: структура презентации."""
    lang_instruction = "русском" if language == "ru" else "английском"
    prompt = (
        f"Сгенерируй структуру презентации на {lang_instruction} языке на тему: "
        f'"{topic}". Количество слайдов: {slide_count}. '
        f"Верни строго JSON-массив из {slide_count} объектов вида "
        '{"title": "...", "key_points": ["...", "..."]}. '
        "Первый слайд — титульный (название темы + подзаголовок), последний — вывод/спасибо. "
        "Без markdown, без пояснений, только валидный JSON."
    )
    raw = await _call_gigachat(prompt, temperature=0.8)
    raw = _extract_json(raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Не удалось распарсить JSON структуры: %s", raw[:500])
        raise GenerationError("AI вернул некорректный JSON структуры презентации.") from exc

    outline = [
        SlideOutline(title=item.get("title", f"Слайд {i+1}"), key_points=item.get("key_points", []))
        for i, item in enumerate(items)
    ]
    return outline[:slide_count]


async def generate_slide_content(
    topic: str,
    outline_item: SlideOutline,
    style: str,
    language: str = "ru",
    with_notes: bool = False,
) -> SlideContent:
    """Этап 2: наполнение конкретного слайда."""

    lang_instruction = "русском" if language == "ru" else "английском"

    notes_instruction = (
        'Добавь поле "speaker_notes" (2-3 предложения заметок для докладчика).'
        if with_notes
        else 'Поле "speaker_notes" оставь пустой строкой.'
    )

    prompt = (
        f"Контекст: презентация на тему '{topic}' в стиле '{style}'. "
        f"Раскрой слайд с заголовком '{outline_item.title}' и тезисами {outline_item.key_points} "
        f"на {lang_instruction} языке. "

        "Верни ТОЛЬКО один JSON-объект следующего вида:\n"
        "{\n"
        '\"title\": \"Заголовок слайда\",\n'
        '\"bullets\": [\"пункт 1\", \"пункт 2\", \"пункт 3\"],\n'
        '\"image_keywords\": [\"cat\", \"animal\"],\n'
        '\"speaker_notes\": \"\"\n'
        "}\n"

        f"{notes_instruction} "
        "Нельзя возвращать массив. "
        "Нельзя писать текст до или после JSON. "
        "Без markdown."
    )

    raw = await _call_gigachat(prompt, temperature=0.3)

    logger.info("RAW BEFORE PARSE:")
    logger.info(raw)

    raw = _extract_json(raw)

        try:
        item = json.loads(raw)

        # GigaChat иногда возвращает массив вместо объекта
        if isinstance(item, list):
            logger.warning("GigaChat вернул список вместо объекта")

            item = {
                "title": outline_item.title,
                "bullets": item,
                "image_keywords": [],
                "speaker_notes": ""
            }

        # На всякий случай проверяем тип
        if not isinstance(item, dict):
            raise ValueError("Ответ JSON не является объектом")

    except (json.JSONDecodeError, ValueError):
        logger.warning("Попытка восстановления JSON слайда")

        # Восстановление частого бага GigaChat:
        # ["bullet1","bullet2"], "image_keywords":[...]
        try:
            bullets_match = re.search(
                r'\[(.*?)\]\s*,\s*"image_keywords"',
                raw,
                re.DOTALL
            )

            images_match = re.search(
                r'"image_keywords"\s*:\s*(\[.*?\])',
                raw,
                re.DOTALL
            )

            bullets = []
            images = []

            if bullets_match:
                bullets = json.loads(
                    "[" + bullets_match.group(1) + "]"
                )

            if images_match:
                images = json.loads(
                    images_match.group(1)
                )

            item = {
                "title": outline_item.title,
                "bullets": bullets,
                "image_keywords": images,
                "speaker_notes": ""
            }

        except Exception as exc:
            logger.error("RAW GIGACHAT RESPONSE:")
            logger.error(raw)
            logger.exception(exc)

            raise GenerationError(
                "AI вернул некорректный JSON для слайда."
            ) from exc


    return SlideContent(
        title=item.get("title", outline_item.title),
        bullets=item.get(
            "bullets",
            outline_item.key_points
        ),
        image_keywords=item.get(
            "image_keywords",
            []
        )[:3],
        speaker_notes=item.get(
            "speaker_notes"
        ) or None,
    )
