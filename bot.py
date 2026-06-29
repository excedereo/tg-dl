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
from aiogram import Bot, Dispatcher
from aiogram.types import InlineQuery, InlineQueryResultCachedVideo, \
    InlineQueryResultArticle, InputTextMessageContent, FSInputFile

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

# кэш url -> (file_id, title): уже скачанное отдаём мгновенно, не качаем дважды
_cache: dict[str, tuple[str, str]] = {}

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

    async def get_file_id(url: str) -> tuple[str, str] | None:
        """Возвращает (file_id, title) — из кэша или качает и заливает в склад."""
        if url in _cache:
            return _cache[url]
        res = await asyncio.to_thread(_download, url)
        if not res:
            return None
        f, title = res
        try:
            sent = await bot.send_video(storage_id, FSInputFile(f), caption=title[:200])
            file_id = sent.video.file_id
            _cache[url] = (file_id, title)
            log.info("готово: %s", title[:60])
            return file_id, title
        except Exception as e:
            log.error("ошибка заливки в склад: %s", str(e)[:200])
            return None
        finally:
            shutil.rmtree(f.parent, ignore_errors=True)

    @dp.inline_query()
    async def on_inline(q: InlineQuery):
        url_m = URL_RE.search(q.query or "")
        url = url_m.group(0) if url_m else ""
        if not url or not SUPPORTED.search(url):
            await q.answer([], cache_time=1, is_personal=True,
                           switch_pm_text="Вставь ссылку YouTube или TikTok",
                           switch_pm_parameter="start")
            return
        log.info("запрос: %s", url)
        got = await get_file_id(url)
        if not got:
            # не скачалось — показываем «ошибку» как выбираемый результат
            err = InlineQueryResultArticle(
                id=uuid.uuid4().hex, title="Не удалось скачать",
                description="слишком длинное, недоступно или не поддерживается",
                input_message_content=InputTextMessageContent(
                    message_text="❌ Не удалось скачать видео"))
            await q.answer([err], cache_time=1, is_personal=True)
            return
        file_id, title = got
        # готовое видео с превью — тап моментально отправляет
        result = InlineQueryResultCachedVideo(
            id=uuid.uuid4().hex,
            video_file_id=file_id,
            title=title[:100],
            description="Нажми, чтобы отправить",
        )
        await q.answer([result], cache_time=300, is_personal=True)

    log.info("бот запущен (inline)")
    await dp.start_polling(bot, handle_signals=False)
