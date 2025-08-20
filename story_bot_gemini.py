import asyncio
import json
import logging
import os
import random
from io import BytesIO
from pathlib import Path
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
import telegram
from telegram import Bot, Poll, Message
from google import genai
from google.genai import types

"""
Story Bot (Gemini, no images)
----------------------------
Скрипт, который в бесконечном цикле ведёт интерактивную историю:
- хранит состояние истории в story_state.json
- каждые STEP_INTERVAL_SECONDS (по умолчанию 60) постит часть истории в канал
- создаёт опрос из 4 вариантов (радикально разных)
- перед публикацией закрывает прошлый опрос, берёт победителя и продолжает историю

ENV (рекомендуется):
  BOT_TOKEN          : токен Телеграм-бота
  CHANNEL_ID         : @username канала или chat id (например, -100123456789)
  GEMINI_API_KEY     : ключ Gemini API
  GEMINI_MODEL       : по умолчанию gemini-2.5-flash
  ENABLE_IMAGE_GEN   : если "true", включить генерацию изображений (по умолчанию false)
  GEMINI_IMAGE_MODEL : модель для генерации изображений (по умолчанию gemini-2.0-flash-preview-image-generation)
  INITIAL_STORY_IDEA : стартовый текст истории (опц., иначе дефолт ниже)
  STEP_INTERVAL_SECONDS: интервал между шагами (сек.), по умолчанию 60

Render (Background Worker):
  Build: pip install -r requirements.txt
  Start: python story_bot_gemini.py
"""

# Load .env if present
load_dotenv()

# ---------------------- CONFIG ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="thecode.log",
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ENABLE_IMAGE_GEN = os.getenv("ENABLE_IMAGE_GEN", "false").lower() == "true"
GEMINI_IMAGE_MODEL = os.getenv(
    "GEMINI_IMAGE_MODEL", "gemini-2.0-flash-preview-image-generation"
)

# Стартовая идея (можно переопределить через ENV)
INITIAL_STORY_IDEA = os.getenv(
    "INITIAL_STORY_IDEA",
    (
        "Альтернативная вселенная: Великая Отечественная война + киберпанк\n"
        "Главный герой: Солдат Красной Армии по имени Андрей.\n\n"
        "Берлинский маршрут: Протокол 77-Б. Ночью на них напал дождь — асинхронный.\n"
        "Капли падали до того, как небо начинало хмуриться.\n"
    ),
)

STATE_FILE = Path(__file__).parent / "story_state.json"
POLL_QUESTION_TEMPLATE = "Как продолжится история?"
MAX_CONTEXT_CHARS = 15000
MAX_POST_CHARS = 500
STEP_INTERVAL_SECONDS = int(os.getenv("STEP_INTERVAL_SECONDS", "60"))

# Инициализация клиента Gemini (берёт ключ из GEMINI_API_KEY)
client = genai.Client()

# ------------------ Helpers ------------------
def validate_config() -> bool:
    ok = True
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN отсутствует.")
        ok = False
    if not CHANNEL_ID:
        logging.error("CHANNEL_ID отсутствует.")
        ok = False
    if not INITIAL_STORY_IDEA:
        logging.error("INITIAL_STORY_IDEA пустой.")
        ok = False
    return ok


def load_state() -> Dict[str, Any]:
    default_state = {"current_story": "", "last_poll_message_id": None}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "current_story": data.get("current_story", ""),
                "last_poll_message_id": data.get("last_poll_message_id"),
            }
        except Exception as e:
            logging.error(f"Не удалось загрузить состояние: {e}. Начинаем заново.")
            return default_state
    return default_state


def save_state(state: Dict[str, Any]) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        logging.info(f"Состояние сохранено: {state}")
    except Exception as e:
        logging.error(f"Не удалось сохранить состояние: {e}")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Пытаемся аккуратно достать JSON-объект из произвольного текста."""
    text = (text or "").strip()
    if not text:
        return None
    # Прямой парсинг
    try:
        return json.loads(text)
    except Exception:
        pass
    # Эвристика: берём подстроку от первого '{' до последней '}'
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            maybe = text[start : end + 1]
            return json.loads(maybe)
    except Exception:
        pass
    return None


# ------------------ LLM: история ------------------
def generate_story_continuation(current_story: str, user_choice: str) -> Optional[str]:
    """Просим модель выдать три коротких абзаца продолжения.
    Каждый абзац — 1-2 предложения, общий текст не длиннее MAX_POST_CHARS.
    Возвращается JSON с полями reasoning и story_part."""
    truncated = current_story[-MAX_CONTEXT_CHARS:]

    system_instruction = (
        "Ты — креативный писатель на русском. Продолжай интерактивную историю ТРЕМЯ абзацами,\n"
        "учитывая победивший вариант опроса. Каждый абзац отделяй пустой строкой.\n"
        f"Абзацы короткие (1-2 предложения). Общая длина не более {MAX_POST_CHARS} символов.\n"
        "Избегай клише и 'AI slop'. Не меняй стиль рассказчика без причины.\n\n"
        "Формат ответа: ВОЗВРАЩАЙ только JSON-объект без пояснений,\n"
        "с полями: {\"reasoning\": string, \"story_part\": string}. Без Markdown и кодовых блоков."
    )

    user_prompt = (
        f"Предыдущая история:\n{truncated}\n\n"
        f"Выбор пользователя: '{user_choice}'\n\n"
        f"Верни только JSON c полями reasoning и story_part. story_part <= {MAX_POST_CHARS} символов."
    )

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
                max_output_tokens=700,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        data = _extract_json(resp.text)
        if not data:
            logging.error("Модель вернула не-JSON или пустой текст")
            return None
        part = (data.get("story_part") or "").strip()
        part = part[:MAX_POST_CHARS]
        return part or None
    except Exception as e:
        logging.error(f"Ошибка Gemini при генерации истории: {e}")
        return None


# ------------------ LLM: варианты опроса ------------------
def generate_poll_options(full_story_context: str) -> Optional[List[str]]:
    truncated = full_story_context[-MAX_CONTEXT_CHARS:]

    system_instruction = (
        "Ты помогаешь интерактивной истории. На основе ПОЛНОГО текущего текста предложи ровно 4\n"
        "КОРОТКИХ и радикально разных варианта продолжения (<= 90 символов).\n"
        "Верни ТОЛЬКО JSON: {\"options\": string[4]}."
    )

    user_prompt = f"Контекст истории:\n{truncated}\n\nВерни только JSON с массивом 'options' из 4 строк."

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.6,
                max_output_tokens=200,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        data = _extract_json(resp.text)
        if not data or "options" not in data:
            logging.error("Не удалось распарсить JSON с вариантами опроса")
            return None
        options_raw = data.get("options", [])
        options: List[str] = []
        for o in options_raw:
            if isinstance(o, str):
                s = o.strip()[:90]
                if len(s) >= 5:
                    options.append(s)
        if len(options) != 4:
            logging.error(f"Ожидалось 4 варианта, получили {len(options)}")
            return None
        return options
    except Exception as e:
        logging.error(f"Ошибка Gemini при генерации вариантов опроса: {e}")
        return None


# ------------------ Опрос: определение победителя ------------------
async def get_poll_winner(bot: Bot, chat_id: str | int, message_id: int) -> Optional[str]:
    if message_id is None:
        logging.warning("Не передан message_id опроса.")
        return None
    logging.info(f"Пробуем закрыть опрос (message_id={message_id})…")
    try:
        updated_poll: Poll = await bot.stop_poll(chat_id=chat_id, message_id=message_id)
        logging.info("Опрос закрыт.")

        winning_options: List[str] = []
        max_votes = -1
        for option in updated_poll.options:
            if option.voter_count > max_votes:
                max_votes = option.voter_count
                winning_options = [option.text]
            elif option.voter_count == max_votes and max_votes > 0:
                winning_options.append(option.text)

        if max_votes > 0 and len(winning_options) == 1:
            return winning_options[0]
        elif max_votes > 0:
            logging.warning("Ничья в опросе — берём первый из лидеров.")
            return winning_options[0]
        else:
            logging.info("В опросе нет голосов — выбираем случайно.")
            if updated_poll.options:
                return random.choice(updated_poll.options).text
            return None
    except telegram.error.TelegramError as e:
        logging.error(f"Ошибка при остановке опроса: {e}")
        return None


# ------------------ Основной шаг истории ------------------
async def run_story_step():
    logging.info("--- Шаг истории ---")
    state = load_state()
    current_story: str = state.get("current_story", "")
    last_poll_message_id = state.get("last_poll_message_id")

    bot = Bot(token=BOT_TOKEN)

    next_prompt: Optional[str] = None
    new_poll_message_id: Optional[int] = None

    try:
        # 1) Кто победил в прошлом опросе?
        if last_poll_message_id:
            logging.info(f"Проверяем результаты прошлого опроса (id={last_poll_message_id})…")
            poll_winner = await get_poll_winner(bot, CHANNEL_ID, last_poll_message_id)
            if poll_winner:
                next_prompt = poll_winner
            else:
                logging.warning("Победитель не определён — используем стартовую идею как подсказку.")
                next_prompt = INITIAL_STORY_IDEA
        else:
            logging.info("Опроса ещё не было — начинаем со стартовой идеи.")
            next_prompt = INITIAL_STORY_IDEA

        # 2) Публикуем первую часть или генерим продолжение
        if not current_story:
            logging.info(f"Публикуем старт истории в канал {CHANNEL_ID}…")
            await bot.send_message(chat_id=CHANNEL_ID, text=INITIAL_STORY_IDEA)
            current_story = INITIAL_STORY_IDEA
        else:
            if not next_prompt:
                next_prompt = "Продолжай логично и интересно."
            logging.info(f"Генерируем продолжение по выбору: '{next_prompt}'…")
            new_part = generate_story_continuation(current_story, next_prompt)
            if not new_part or not new_part.strip():
                raise RuntimeError("Не удалось сгенерировать продолжение истории.")
            await bot.send_message(chat_id=CHANNEL_ID, text=new_part)
            current_story += ("\n\n" if not current_story.endswith("\n\n") else "") + new_part

        # 3) Генерируем и публикуем опрос
        logging.info("Генерируем варианты опроса…")
        poll_options = generate_poll_options(current_story) or [
            "Продолжить штурмовать позиции",
            "Искать обходной путь",
            "Запросить подкрепление",
            "Перегруппироваться",
        ]
        if len(poll_options) != 4:
            logging.warning("Фолбэк: используем дефолтные варианты.")
            poll_options = [
                "Атаковать с фланга",
                "Укрепить оборону",
                "Провести разведку",
                "Изменить стратегию",
            ]
        sent: Message = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=POLL_QUESTION_TEMPLATE,
            options=[o[:90] for o in poll_options],
            is_anonymous=True,
        )
        new_poll_message_id = sent.message_id
        logging.info(f"Опрос опубликован (id={new_poll_message_id}).")

        # 4) Сохраняем состояние
        save_state({
            "current_story": current_story,
            "last_poll_message_id": new_poll_message_id,
        })
        logging.info("--- Шаг истории успешно завершён ---")

    except RuntimeError as e:
        logging.error(f"Runtime ошибка: {e}")
    except telegram.error.TelegramError as e:
        logging.error(f"Telegram ошибка: {e}")
    except Exception as e:
        logging.error(f"Непредвиденная ошибка: {e}", exc_info=True)


async def run_forever():
    """Бесконечный цикл шагов истории."""
    while True:
        await run_story_step()
        logging.info(
            f"Ждём {STEP_INTERVAL_SECONDS} сек. до следующего шага истории…"
        )
        await asyncio.sleep(STEP_INTERVAL_SECONDS)


# ------------------ Запуск ------------------
if __name__ == "__main__":
    logging.info("Старт скрипта.")
    if not validate_config():
        logging.critical("Проверь BOT_TOKEN / CHANNEL_ID / GEMINI_API_KEY. Выходим.")
    else:
        logging.info("Конфигурация ок. Запускаем цикл истории.")
        asyncio.run(run_forever())
    logging.info("Завершение скрипта.")
