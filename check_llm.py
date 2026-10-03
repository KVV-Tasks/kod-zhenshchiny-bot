"""Проверка ключа нейросети: список моделей и пробный ответ.

    python check_llm.py            # пробный ответ модели из .env
    python check_llm.py --models   # какие модели доступны ключу
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(Path(__file__).parent / ".env")


async def main() -> None:
    llm = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"], timeout=90)
    if "--models" in sys.argv:
        models = await llm.models.list()
        for m in models.data:
            print(m.id)
        return
    model = os.environ["LLM_MODEL"]
    resp = await llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Ответь одной фразой по-русски: ты на связи?"}],
        max_tokens=60,
    )
    print(f"Модель {model} отвечает:", resp.choices[0].message.content)


asyncio.run(main())
