#!/usr/bin/env python3
# bot_downloader.py

import os
import re
import shutil
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
load_dotenv()
import telebot
import instaloader
from yt_dlp import YoutubeDL

# ========= Конфиг =========
BOT_TOKEN = os.getenv("8185652037:AAGJkZfvuKN3Sl9xHMFOq-aGz3fM6nuhM8U", "8185652037:AAGJkZfvuKN3Sl9xHMFOq-aGz3fM6nuhM8U")
IG_USER = os.getenv("IG_USER")
IG_PASS = os.getenv("IG_PASS")

L = instaloader.Instaloader()
L.login(IG_USER, IG_PASS)
L.save_session_to_file()


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not IG_USER or not IG_PASS:
    raise RuntimeError("IG_USER и IG_PASS должны быть заданы в окружении")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
POOL = ThreadPoolExecutor(max_workers=3)
TELEGRAM_VIDEO_SAFE_LIMIT = 45 * 1024 * 1024  # ~45MB

# ========= Утилиты =========
def is_youtube(url: str) -> bool:
    return any(d in url for d in ("youtube.com", "youtu.be"))

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url

def is_instagram(url: str) -> bool:
    return "instagram.com" in url

def is_instagram_story_url(url: str) -> bool:
    return "/stories/" in urlparse(url).path

def extract_instagram_story_username(url: str) -> str | None:
    m = re.search(r"instagram\.com/stories/([^/]+)/", url)
    return m.group(1) if m else None

def normalize_shortcode(url: str) -> str:
    path = urlparse(url).path
    parts = [p for p in path.split('/') if p]
    if len(parts) >= 2 and parts[-2] in ("p", "reel", "tv"):
        return parts[-1]
    raise ValueError("Не удалось определить shortcode из URL")

# ========= Instagram (Instaloader с логином) =========
def init_instaloader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        save_metadata=False,
        download_comments=False,
        download_video_thumbnails=False,
        download_geotags=False,
        max_connection_attempts=3,
        quiet=True
    )
    try:
        L.load_session_from_file(IG_USER)
        logging.info("Instaloader: загружена сохранённая сессия")
    except FileNotFoundError:
        logging.info("Instaloader: логин по паролю...")
        L.login(IG_USER, IG_PASS)
        L.save_session_to_file()
        logging.info("Instaloader: сессия сохранена")
    return L

def dl_instagram_post(url: str, outdir: Path) -> list[Path]:
    L = init_instaloader()
    shortcode = normalize_shortcode(url)
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.dirname_pattern = str(outdir)
    L.download_post(post, target=str(outdir))
    files = sorted([p for p in outdir.iterdir() if p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png")])
    if not files:
        raise RuntimeError("Instaloader не вернул медиа")
    return files

def dl_instagram_stories_by_username(username: str, outdir: Path) -> list[Path]:
    L = init_instaloader()
    profile = instaloader.Profile.from_username(L.context, username)
    L.dirname_pattern = str(outdir)
    got = []
    for story in L.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            L.download_storyitem(item, target=str(outdir))
    for p in outdir.iterdir():
        if p.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png"):
            got.append(p)
    if not got:
        raise RuntimeError("Нет активных Stories или нет доступа")
    return sorted(got)

# ========= YouTube/TikTok (yt-dlp) =========
def dl_via_ytdlp(url: str, outdir: Path, platform: str) -> list[Path]:
    ydl_opts = {
        "outtmpl": str(outdir / "%(title).80s [%(id)s].%(ext)s"),
        "format": "bv*+ba/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "retries": 3,
        "noprogress": True,
        "quiet": True,
        "concurrent_fragment_downloads": 5,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "extractor_args": {
            "tiktok": {"no_watermark": "1"},
            "youtube": {"player_client": ["android"]}
        }
    }

    cookies_file = "ig_cookies.txt"
    if platform == "instagram" and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    files: list[Path] = []

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        def collect_from_info(i):
            # если есть готовый путь
            if "_filename" in i:
                files.append(Path(i["_filename"]))
            # fallback: сформировать вручную
            else:
                files.append(Path(ydl.prepare_filename(i)))

        # если stories = плейлист
        if "entries" in info and info["entries"]:
            for entry in info["entries"]:
                collect_from_info(entry)
        else:
            collect_from_info(info)

    # привести расширение к .mp4
    files = [f.with_suffix(".mp4") if f.suffix.lower() != ".mp4" else f for f in files]

    # проверить, что реально скачались
    exist = [f for f in files if f.exists()]
    if not exist:
        raise RuntimeError("yt-dlp не вернул файлов")

    return exist


# ========= Отправка в Telegram =========
def send_files(chat_id: int, files: list[Path]):
    for f in files:
        size = f.stat().st_size
        caption = f.name
        try:
            if f.suffix.lower() in (".mp4", ".mov") and size <= TELEGRAM_VIDEO_SAFE_LIMIT:
                bot.send_chat_action(chat_id, "upload_video")
                with f.open("rb") as fh:
                    bot.send_video(chat_id, fh, caption=caption)
            else:
                bot.send_chat_action(chat_id, "upload_document")
                with f.open("rb") as fh:
                    bot.send_document(chat_id, fh, caption=caption)
        except Exception as e:
            logging.exception("Ошибка отправки файла")
            bot.send_message(chat_id, f"Не удалось отправить {f.name}: {e}")

# ========= Обработчики =========
@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(message,
        "Отправь ссылку на Instagram / YouTube / TikTok.\n"
        "- Instagram: посты, Reels и Stories (нужен логин)\n"
        "- YouTube: ссылка на видео\n"
        "- TikTok: видео без водяного знака"
    )

@bot.message_handler(commands=["story"])
def story_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Использование: /story username")
        return
    username = args[1].strip().lstrip("@")
    bot.reply_to(message, f"Скачиваю Stories @{username}…")
    POOL.submit(process_story_username, message.chat.id, username)

def process_story_username(chat_id: int, username: str):
    tmp = Path(tempfile.mkdtemp(prefix="ig_story_"))
    try:
        try:
            story_url = f"https://www.instagram.com/stories/{username}/"
            files = dl_instagram_stories_with_fallback(story_url, username, tmp)
        except Exception as e:
            logging.warning(f"Instaloader stories fail: {e}, пробую yt-dlp…")
            # Пробуем скачать через yt-dlp по прямой ссылке на сторис
            story_url = f"https://www.instagram.com/stories/{username}/"
            files = dl_via_ytdlp(story_url, tmp, platform="instagram")
        send_files(chat_id, files)
    except Exception as e:
        logging.exception("Ошибка скачивания Stories")
        bot.send_message(chat_id, f"Ошибка Stories @{username}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@bot.message_handler(func=lambda m: bool(re.match(r"https?://", m.text or "")))
def handle_url(message):
    url = message.text.strip()
    bot.reply_to(message, "Принял ссылку. Начинаю скачивание…")
    POOL.submit(process_url_download, message.chat.id, url)

def process_url_download(chat_id: int, url: str):
    tmp = Path(tempfile.mkdtemp(prefix="dl_"))
    try:
        files: list[Path] = []
        if is_instagram(url):
            if is_instagram_story_url(url):
                username = extract_instagram_story_username(url)
                if not username:
                    raise RuntimeError("Не смог выделить username из ссылки сторис")
                files = dl_instagram_stories_with_fallback(url, username, tmp)
            else:
                try:
                    files = dl_instagram_post(url, tmp)
                except Exception as e:
                    logging.warning(f"Instaloader не справился ({e}), пробую yt-dlp…")
                    files = dl_via_ytdlp(url, tmp, platform="instagram")
        elif is_youtube(url):
            files = dl_via_ytdlp(url, tmp, platform="youtube")
        elif is_tiktok(url):
            files = dl_via_ytdlp(url, tmp, platform="tiktok")
        else:
            bot.send_message(chat_id, "Не распознал платформу. Поддерживаются Instagram/YouTube/TikTok.")
            return

        bot.send_message(chat_id, f"Готово. Отправляю {len(files)} файл(ов)…")
        send_files(chat_id, files)

    except Exception as e:
        logging.exception("Общая ошибка скачивания")
        bot.send_message(chat_id, f"Ошибка: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def dl_instagram_stories_with_fallback(url: str, username: str, outdir: Path) -> list[Path]:
    """
    Пытаемся скачать сторис через Instaloader.
    Если не получилось — пробуем yt-dlp по прямой ссылке.
    """
    try:
        return dl_instagram_stories_by_username(username, outdir)
    except Exception as e:
        logging.warning(f"Instaloader stories fail: {e}, пробую yt-dlp…")
        return dl_via_ytdlp(url, outdir, platform="instagram")


# ========= Точка входа =========
if __name__ == "__main__":
    logging.info("Бот запущен")
    bot.infinity_polling(skip_pending=True, timeout=60)
