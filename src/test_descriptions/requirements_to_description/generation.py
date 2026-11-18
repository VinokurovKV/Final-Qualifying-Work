import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .requirements import Requirement


FORMAT_PATH = Path(__file__).resolve().parents[3] / "docs/test_description_format.md"
PROMPTS_DIR = Path(__file__).with_name("prompts")
Notify = Callable[[str, str], None]


class TestDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=60)
    short_description: str = Field(min_length=1)
    port_count: int = Field(default=4, ge=1, le=4)

    @field_validator("code", "short_description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Поле не должно быть пустым.")
        if "\n" in value or "\r" in value:
            raise ValueError("Поле должно занимать одну строку.")
        return value.strip()


def request_llm(prompt: str, *, stage: str, notify: Notify | None = None, announce: bool = True,
                system_instructions: str | None = None, output_format: Literal["json", "xml"] = "json",
                on_response: Callable[[dict], None] | None = None) -> str:
    from openai import APIConnectionError, APIStatusError, Omit, OpenAI

    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    report = notify or (lambda message, level: print(message, flush=True))
    if not base_url or not model:
        raise ValueError("Не заданы LLM_BASE_URL и LLM_MODEL.")
    providers = {
        "openrouter.ai": ("OpenRouter", "https://openrouter.ai/keys"),
        "api.deepseek.com": ("DeepSeek", "https://platform.deepseek.com/api_keys"),
    }
    provider = providers.get(urlparse(base_url).hostname)
    if provider and not api_key:
        name, key_url = provider
        raise ValueError(f"Для {name} нужен API-ключ. Создайте его на {key_url} "
                         "и укажите в .env: LLM_API_KEY=ваш_ключ.")

    system_prompt = system_instructions
    if system_prompt is None:
        system_prompt = ((PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
                         + FORMAT_PATH.read_text(encoding="utf-8"))

    # SDK требует ключ даже для API без авторизации. Заглушка не отправляется.
    with OpenAI(
        api_key=api_key or "unused",
        base_url=base_url,
        timeout=60.0,
        max_retries=0,
    ) as client:
        for attempt in range(4):
            if announce:
                report(f"LLM · {stage} · модель {model} · попытка {attempt + 1}/4 · ожидание ответа", "info")
            started = time.monotonic()
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                    extra_headers={} if api_key else {"Authorization": Omit()},
                )
                break
            except (APIConnectionError, APIStatusError) as error:
                status = getattr(error, "status_code", None)
                detail = f"HTTP {status}" if status is not None else "ошибка соединения"
                if attempt == 3 or (status is not None and status < 500 and status not in (408, 409, 429)):
                    report(f"Ошибка LLM · {stage} · {detail} · попытка {attempt + 1}/4. Запрос остановлен.", "error")
                    raise
                delay = 5 * 2 ** attempt
                report(f"Ошибка LLM · {stage} · {detail} · попытка {attempt + 1}/4. Повтор через {delay} с...", "error")
                time.sleep(delay)
    if on_response:
        usage = getattr(response, "usage", None)
        on_response({"model": getattr(response, "model", None), "requested_model": model,
                     "response_id": getattr(response, "id", None),
                     "elapsed_seconds": round(time.monotonic() - started, 2),
                     "usage": usage.model_dump() if usage is not None else None})
    if not response.choices or not response.choices[0].message.content:
        raise ValueError("LLM вернула пустой ответ.")
    text = response.choices[0].message.content.strip()
    # Снимаем Markdown-обёртку; XML проверяется и сохраняется вызывающим этапом.
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].lower() in (f"```{output_format}", "```") and lines[-1] == "```":
        text = "\n".join(lines[1:-1])
    if output_format == "xml":
        report(f"✓ LLM · {stage}: ответ получен за {time.monotonic() - started:.1f} с. Проверка XML...", "success")
        return text
    try:
        json.loads(text)
    except json.JSONDecodeError as error:
        message = " ".join(text.split())[:600]
        if "automated bulk tasks" in text.lower():
            raise ValueError("Провайдер ограничил автоматические пакетные запросы. "
                             "Повторять запрос не нужно: смените провайдера или согласуйте доступ.\n"
                             f"Ответ провайдера: {message}") from error
        raise ValueError(f"Сервис LLM вернул текст вместо JSON.\nОтвет провайдера: {message}") from error
    report(f"✓ LLM · {stage}: ответ получен за {time.monotonic() - started:.1f} с. Проверка структуры...", "success")
    return text


def generate_test_description(
    requirements: list[Requirement], *, stage: str = "Описание теста", notify: Notify | None = None
) -> TestDescription:
    prompt = "".join((PROMPTS_DIR / name).read_text(encoding="utf-8") for name in (
        "test_description.md", "test_code.md", "short_description.md", "topology.md",
    )) + json.dumps(
        [
            {"code": req.code, "modifier": req.modifier, "text": req.description.text}
            for req in requirements
        ],
        ensure_ascii=False,
    )
    return TestDescription.model_validate_json(request_llm(prompt, stage=stage, notify=notify, announce=False))
