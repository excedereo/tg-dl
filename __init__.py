"""
TG-DL — Telegram-бот скачивания видео (YouTube/TikTok). Модуль для vaeli-hub.

Фоновый сервис без веб-роутов: ядро запускает bot.run() как фоновую задачу
(Service.startup). В лендинге не показывается (web=False).
"""

from registry import Service
from .bot import run

service = Service(
    name="tg",
    title="TG Downloader",
    description="Telegram-бот: inline-скачивание видео с YouTube и TikTok",
    icon="fa-brands fa-telegram",
    startup=run,            # ядро запустит как фоновую задачу
    web=False,              # бот — не веб-сервис, в лендинге не нужен
    needs=["ffmpeg", "deno"],
)
