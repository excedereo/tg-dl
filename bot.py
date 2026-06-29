"""
TG-DL — Telegram-бот скачивания видео с YouTube и TikTok (без вотермарки).

Работает inline: в любом чате пишешь  @бот <ссылка>  → появляется плашка
«Скачать видео» → тапаешь → в чат вставляется заглушка «качаю...», бот в фоне
качает видео и подменяет заглушку на готовый ролик.

Почему так: inline-ответ Telegram должен прийти мгновенно, а скачать видео
быстро нельзя. Поэтому сначала вставляем заглушку (inline_message_id), потом
правим её на видео через edit_message_media. Чтобы видео можно было вставить
в чужой чат, бот сперва заливает его в канал-склад (TG_STORAGE_CHAT) и берёт
оттуда file_id.

Конфиг (env или .env):
    TG_TOKEN          — токен бота от BotFather
    TG_STORAGE_CHAT   — id канала-склада (бот должен быть там админом)
"""

import re
import uuid
import shutil
import asyncio
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    ChosenInlineResult, InputMediaVideo, FSInputFile,
)

from shared import binaries
from shared.env import get as env
from shared.log import get_logger

log = get_logger("tg")

BASE = Path(__file__).resolve().parent
DL_DIR = BASE / "downloads"
DL_DIR.mkdir(exist_ok=True)

MAX_DURATION = 10 * 60          # лимит длительности, сек (под размер Telegram)
MAX_QUALITY = 720              # потолок качества, чтобы влезть в 50 МБ
URL_RE = re.compile(r"https?://\S+", re.I)
SUPPORTED = re.compile(r"(youtube\.com|youtu\.be|tiktok\.com)", re.I)

# карта id_результата -> ссылка (chosen_inline_result не возвращает текст запроса)
_pending: dict[str, str] = {}


def _build_opts(job: Path):
    opts = {
        "outtmpl": str(job / "%(title)s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "remote_components": ["ejs:github"],
        "socket_timeout": 60, "retries": 10, "fragment_retries": 10,
        "format": (f"bestvideo[height<={MAX_QUALITY}][ext=mp4]+bestaudio[ext=m4a]/"
                   f"best[height<={MAX_QUALITY}][ext=mp4]/best[height<={MAX_QUALITY}]/best"),
        "merge_output_format": "mp4",
        "postprocessor_args": {"merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]},
    }
    if binaries.FFMPEG_DIR:
        opts["ffmpeg_location"] = binaries.FFMPEG_DIR
    return opts


def _download(url: str) -> tuple[Path, str] | None:
    """Качает видео в свою папку. Возвращает (файл, заголовок) или None."""
    job = DL_DIR / uuid.uuid4().hex
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True,
                               "noplaylist": True, "remote_components": ["ejs:github"]}) as ydl:
            meta = ydl.extract_info(url, download=False)
        if (meta.get("duration") or 0) > MAX_DURATION:
            log.info("отказ: длиннее лимита (%ss)", meta.get("duration"))
            return None
        job.mkdir(exist_ok=True)
        with yt_dlp.YoutubeDL(_build_opts(job)) as ydl:
            ydl.extract_info(url, download=True)
        files = [p for p in job.iterdir() if p.is_file()]
        if not files:
            return None
        f = max(files, key=lambda p: p.stat().st_size)
        return f, (meta.get("title") or "видео")
    except Exception as e:
        log.error("ошибка скачивания %s: %s", url, str(e)[:200])
        shutil.rmtree(job, ignore_errors=True)
        return None


async def run() -> None:
    """Точка входа сервиса: поднимает бота на long-polling."""
    token = env("TG_TOKEN")
    storage = env("TG_STORAGE_CHAT")
    if not token or not storage:
        log.error("нет TG_TOKEN / TG_STORAGE_CHAT — бот не запущен")
        return
    storage_id = int(storage)

    bot = Bot(token)
    dp = Dispatcher()

    @dp.inline_query()
    async def on_inline(q: InlineQuery):
        url_m = URL_RE.search(q.query or "")
        url = url_m.group(0) if url_m else ""
        if not url or not SUPPORTED.search(url):
            await q.answer([], cache_time=1, is_personal=True,
                           switch_pm_text="Вставь ссылку YouTube или TikTok",
                           switch_pm_parameter="start")
            return
        rid = uuid.uuid4().hex
        _pending[rid] = url
        result = InlineQueryResultArticle(
            id=rid,
            title="Скачать видео",
            description=url[:60],
            input_message_content=InputTextMessageContent(
                message_text="⏳ Качаю видео..."),
        )
        await q.answer([result], cache_time=1, is_personal=True)

    @dp.chosen_inline_result()
    async def on_chosen(c: ChosenInlineResult):
        url = _pending.pop(c.result_id, None)
        inline_id = c.inline_message_id
        if not url or not inline_id:
            return
        res = await asyncio.to_thread(_download, url)
        if not res:
            try:
                await bot.edit_message_text(
                    "❌ Не удалось скачать (слишком длинное или недоступно)",
                    inline_message_id=inline_id)
            except Exception:
                pass
            return
        f, title = res
        try:
            # заливаем видео в склад -> берём file_id -> подменяем заглушку
            sent = await bot.send_video(storage_id, FSInputFile(f), caption=title[:200])
            file_id = sent.video.file_id
            await bot.edit_message_media(
                InputMediaVideo(media=file_id, caption=title[:200]),
                inline_message_id=inline_id)
            log.info("отправлено: %s", title[:60])
        except Exception as e:
            log.error("ошибка отправки: %s", str(e)[:200])
            try:
                await bot.edit_message_text("❌ Ошибка отправки видео",
                                            inline_message_id=inline_id)
            except Exception:
                pass
        finally:
            shutil.rmtree(f.parent, ignore_errors=True)

    log.info("бот запущен (inline)")
    await dp.start_polling(bot, handle_signals=False)
