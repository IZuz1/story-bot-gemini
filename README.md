# Gemini Story Bot (Telegram)

Бот-скрипт, который ведёт интерактивную историю в Telegram-канале:
1. Публикует часть истории
2. Создаёт опрос на 4 варианта продолжения
3. В бесконечном цикле закрывает прошлый опрос, берёт победителя и продолжает историю (по умолчанию раз в минуту)

> **Важно:** Скрипт запускается один раз и затем работает непрерывно. Интервал между шагами можно изменить через переменную `STEP_INTERVAL_SECONDS`.

## Быстрый старт локально

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # заполни .env
python story_bot_gemini.py
```

## Переменные окружения

- `BOT_TOKEN` — токен бота из @BotFather
- `CHANNEL_ID` — `@username` канала (бот должен быть админом) или chat id (`-100…`)
- `GEMINI_API_KEY` — ключ Gemini (Google AI Studio)
- `GEMINI_MODEL` — модель (по умолчанию `gemini-2.5-flash`)
- `INITIAL_STORY_IDEA` — начальный текст истории (опционально)
- `STEP_INTERVAL_SECONDS` — интервал между публикациями (в секундах, по умолчанию `60`)

## Render (рекомендовано как Background Worker)

1. Создай **Background Worker**.
2. Build Command:  
   ```
   pip install -r requirements.txt
   ```
3. Start Command:  
   ```
   python story_bot_gemini.py
   ```
4. В Environment добавь `BOT_TOKEN`, `CHANNEL_ID`, `GEMINI_API_KEY` (и при желании `GEMINI_MODEL`, `INITIAL_STORY_IDEA`).

## Состояние
Файл `story_state.json` создаётся рядом со скриптом и хранит:
```json
{ "current_story": "...", "last_poll_message_id": 123 }
```

## Примечания
- Бот публикует **текст** и делает **опрос**. Генерации изображений нет (по твоему запросу).
- Если в опросе ничья — берётся первый из лидеров. Если голосов нет — выбирается случайно.
- Если модель вдруг вернёт не-JSON — сработает фолбэк для опроса, а шаг истории может прерваться (см. логи).
