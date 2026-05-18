# -*- coding: utf-8 -*-

import os
import re
import sqlite3
import threading
import time
import datetime
import logging
import urllib.parse
import feedparser
import telebot
from telebot import types

# Конфигурация

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Установите переменную окружения: export BOT_TOKEN=ваш_токен")

DB_FILE = "manicure_ideas.db"

# RSS-источники с beauty/nail контентом
RSS_FEEDS = [
    "https://www.allure.com/feed/rss",
    "https://www.cosmopolitan.com/rss/all.xml",
    "https://www.elle.com/rss/all.xml",
    "https://www.harpersbazaar.com/rss/all.xml",
    "https://www.vogue.com/feed/rss",
    "https://www.instyle.com/feed/rss",
    "https://www.glamour.com/feed/rss",
    "https://www.refinery29.com/en-us/rss.xml",
    "https://www.popsugar.com/beauty/feed",
    "https://www.byrdie.com/feed/rss",
]

MANICURE_KEYWORDS = [
    # English
    "nail", "nails", "manicure", "pedicure", "nail art", "nail design",
    "nail polish", "nail color", "nail trend", "nail look", "nail style",
    "gel nail", "gel polish", "acrylic nail", "shellac", "dip powder",
    "nail extension", "press-on", "nail wrap", "nail sticker",
    "french manicure", "ombre nail", "chrome nail", "matte nail",
    "nail care", "cuticle", "nail salon", "nail tech", "nail inspo",
    "beauty", "polish", "lacquer",
    # Russian
    "маникюр", "педикюр", "ногти", "ногтей", "гель", "шеллак",
    "гель-лак", "покрытие", "наращивание", "дизайн ногтей",
]

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("manicure_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("manicure_bot")

# База данных

class Database:
    """
    Потокобезопасная обёртка над SQLite.

    Таблицы
    -------
    ideas    — все собранные идеи; поле posted=1 означает «уже отправлялась»
    settings — пары ключ/значение для хранения состояния (дата последнего обновления и т.д.)
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # Инициализация схемы
    
    def _migrate(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS ideas (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    title    TEXT    NOT NULL,
                    link     TEXT    NOT NULL UNIQUE,
                    source   TEXT,
                    summary  TEXT,
                    added_at TEXT    NOT NULL,
                    posted   INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            self._conn.commit()

    # Идеи

    def bulk_insert_ideas(self, ideas: list) -> int:
        """Добавить идеи, пропуская дубликаты по ссылке. Возвращает число новых записей."""
        inserted = 0
        updated = 0
        with self._lock:
            cur = self._conn.cursor()
            for idea in ideas:
                try:
                    cur.execute(
                        "INSERT INTO ideas (title, link, source, summary, added_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            idea["title"],
                            idea["link"],
                            idea.get("source", ""),
                            idea.get("summary", ""),
                            idea["added_at"],
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    # Дубликат по ссылке — идея уже в БД
                    # Проверяем, была ли она уже отправлена, и если да — сбрасываем флаг
                    cur.execute("SELECT id, posted FROM ideas WHERE link = ?", (idea["link"],))
                    row = cur.fetchone()
                    if row and row["posted"] == 1:
                        # Идея уже была отправлена — обновляем title и summary, сбрасываем posted
                        cur.execute(
                            "UPDATE ideas SET title = ?, summary = ?, posted = 0 WHERE link = ?",
                            (idea["title"], idea.get("summary", "")[:500], idea["link"]),
                        )
                        updated += 1
            self._conn.commit()
        
        if updated > 0:
            logger.info("Обновлено старых идей (сброшен флаг posted): %d", updated)
        
        return inserted + updated

    def get_unposted(self, limit: int = 5) -> list:
        """Вернуть до `limit` идей с posted=0 (самые свежие сверху)."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, title, link, source, summary "
                "FROM ideas WHERE posted = 0 "
                "ORDER BY added_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_posted(self, idea_ids: list):
        """Пометить идеи как отправленные (posted=1)."""
        if not idea_ids:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                "UPDATE ideas SET posted = 1 WHERE id = ?",
                [(i,) for i in idea_ids],
            )
            self._conn.commit()

    def get_all_recent(self, limit: int = 10) -> list:
        """
        Вернуть последние `limit` идей из базы БЕЗ изменения флага posted.
        Используется для кнопки «Показать сохранённые».
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT title, link, source, summary, posted "
                "FROM ideas ORDER BY added_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def count_unposted(self) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM ideas WHERE posted = 0")
            return cur.fetchone()[0]

    def refresh_old_ideas(self, days: int = 7):
        """
        Сбросить флаг posted для идей старше N дней.
        Это обновляет цикл идей, чтобы они появлялись заново.
        """
        with self._lock:
            cur = self._conn.cursor()
            threshold_time = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
            cur.execute(
                "UPDATE ideas SET posted = 0 WHERE posted = 1 AND added_at < ?",
                (threshold_time,),
            )
            updated = cur.rowcount
            self._conn.commit()
            if updated > 0:
                logger.info("Обновлено %d старых идей (старше %d дней)", updated, days)
            return updated

    # Настройки

    def get_setting(self, key: str, default=None):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()

# Парсинг RSS

def _strip_html(text: str) -> str:
    """Удалить все HTML-теги из текста, чтобы Telegram не падал с ошибкой парсинга."""
    text = re.sub(r"<[^>]+>", "", text)          # убираем теги
    text = re.sub(r"&nbsp;", " ", text)           # неразрывный пробел
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text).strip()      # лишние пробелы
    return text


def _clean_url(url: str) -> str:
    """Убрать UTM-метки из ссылки, чтобы дубликаты лучше определялись."""
    parsed = urllib.parse.urlparse(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if not k.startswith("utm_")]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _is_manicure(text: str) -> bool:
    text = text.lower()
    return any(kw in text for kw in MANICURE_KEYWORDS)


def _parse_feed(feed_url: str) -> list:
    results = []
    try:
        logger.info("Начинаю парсить RSS: %s", feed_url)
        feed = feedparser.parse(feed_url)
        
        if not feed.entries:
            logger.warning("RSS лента пуста или недоступна: %s", feed_url)
            return results
        
        logger.info("Найдено %d статей в %s", len(feed.entries), feed_url)
        
        for i, entry in enumerate(feed.entries[:50]):  # Ограничиваем 50 статьями
            title = _strip_html((entry.get("title") or "").strip())
            raw_summary = (entry.get("summary") or entry.get("description") or "").strip()
            summary = _strip_html(raw_summary)
            link = _clean_url((entry.get("link") or "").strip())
            
            if not title or not link:
                continue
            
            combined_text = title + " " + summary
            
            # Логируем для отладки (только первые 5 статей)
            if i < 5:
                logger.debug("Статья %d: %s | Текст: %s...", i+1, title[:50], combined_text[:100])
            
            if _is_manicure(combined_text):
                logger.debug("✓ Статья подходит: %s", title[:60])
                results.append({
                    "title": title,
                    "link": link,
                    "source": feed_url,
                    "summary": summary[:500],
                    "added_at": datetime.datetime.utcnow().isoformat(),
                })
            else:
                if i < 5:
                    logger.debug("✗ Статья не подходит (нет ключевых слов): %s", title[:60])
        
        logger.info("Из RSS %s получено %d подходящих статей", feed_url, len(results))
    except Exception as exc:
        logger.warning("Ошибка при разборе RSS %s: %s", feed_url, exc)
    
    return results


def fetch_all_ideas() -> list:
    """Собрать маникюрные идеи со всех RSS-лент, дедуплицировав по ссылке."""
    seen = {}
    total_found = 0
    
    logger.info("=== НАЧАЛО ЗАГРУЗКИ ИДЕЙ ===")
    logger.info("Количество RSS источников: %d", len(RSS_FEEDS))
    
    for url in RSS_FEEDS:
        ideas = _parse_feed(url)
        total_found += len(ideas)
        
        for idea in ideas:
            if idea["link"] not in seen:
                seen[idea["link"]] = idea
    
    logger.info("=== ИТОГО ===")
    logger.info("Всего получено статей: %d", total_found)
    logger.info("Уникальных идей (без дубликатов): %d", len(seen))
    logger.info("=== КОНЕЦ ЗАГРУЗКИ ===")
    
    return list(seen.values())

# Ежедневное обновление

def run_daily_update(db: Database) -> int:
    """
    Выполнить обновление, если прошло 12 часов с последнего обновления.
    Возвращает количество добавленных идей (0 если обновлялось недавно).
    """
    last_update = db.get_setting("last_update_time")
    if last_update:
        last_update_dt = datetime.datetime.fromisoformat(last_update)
        elapsed = datetime.datetime.utcnow() - last_update_dt
        if elapsed < datetime.timedelta(hours=12):
            logger.info("Обновление выполнялось %d минут назад, пропускаем (нужно 12 часов).", int(elapsed.total_seconds() / 60))
            return 0

    # Обновляем старые идеи (сбрасываем posted для идей старше 7 дней)
    db.refresh_old_ideas(days=7)

    ideas = fetch_all_ideas()
    added = db.bulk_insert_ideas(ideas)
    now = datetime.datetime.utcnow().isoformat()
    db.set_setting("last_update_time", now)
    logger.info("Обновление каждые 12 часов: найдено %d, добавлено %d новых идей.", len(ideas), added)
    return added


def _daily_scheduler(db: Database):
    """Фоновый поток: запускать обновление каждые 12 часов."""
    while True:
        try:
            run_daily_update(db)
        except Exception:
            logger.exception("Ошибка в планировщике обновлений")

        now = datetime.datetime.now()
        next_run = now + datetime.timedelta(hours=12)
        wait = max(60.0, (next_run - now).total_seconds())
        logger.info("Следующее обновление через %.0f секунд (%s)", wait, next_run)
        time.sleep(wait)


# Telegram-бот

class ManicureBot:
    def __init__(self, token: str, db: Database):
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.db = db
        self._setup_handlers()

    # UI-компоненты
    
    def _main_keyboard(self) -> types.InlineKeyboardMarkup:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("📋 Показать сохранённые идеи", callback_data="show_saved"),
            types.InlineKeyboardButton("🔄 Обновить идеи сейчас",       callback_data="force_update"),
        )
        return kb

    def _commands_keyboard(self) -> types.ReplyKeyboardMarkup:
        """Основная клавиатура с командами."""
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(
            types.KeyboardButton("💅 Новые идеи"),
            types.KeyboardButton("📋 Сохранённые"),
        )
        kb.add(
            types.KeyboardButton("🔄 Обновить"),
            types.KeyboardButton("♻️ Обновить цикл"),
        )
        kb.add(
            types.KeyboardButton("📊 Статистика"),
            types.KeyboardButton("ℹ️ Справка"),
        )
        return kb

    @staticmethod
    def _format_idea(idea: dict, index: int = None, show_status: bool = False) -> str:
        """Форматировать одну идею для отправки в Telegram."""
        lines = []

        prefix = f"<b>#{index}</b>  " if index else ""
        lines.append(f"{prefix}<b>{idea['title']}</b>")

        if idea.get("summary"):
            lines.append(f"\n{idea['summary'][:300]}…" if len(idea["summary"]) > 300 else f"\n{idea['summary']}")

        lines.append(f"\n🔗 <a href='{idea['link']}'>Читать полностью</a>")

        source = idea.get("source", "")
        if source:
            # Оставляем только домен для читаемости
            try:
                domain = urllib.parse.urlparse(source).netloc
            except Exception:
                domain = source
            lines.append(f"<i>Источник: {domain}</i>")

        if show_status:
            status = "✅ отправлена" if idea.get("posted") else "🆕 новая"
            lines.append(f"<i>Статус: {status}</i>")

        return "\n".join(lines)

    # Обработчики команд
  
    def _setup_handlers(self):
        bot = self.bot

        # /start  /help
        @bot.message_handler(commands=["start", "help"])
        def cmd_start(msg):
            text = (
                "💅 <b>Бот актуальных идей маникюра</b>\n\n"
                "База обновляется каждые 12 часов свежими идеями с сайтов:\n"
                "• Allure • Vogue • Elle\n• Cosmopolitan • Harper's Bazaar\n\n"
                "Используй кнопки ниже для навигации 👇"
            )
            bot.send_message(msg.chat.id, text, reply_markup=self._commands_keyboard())

        # Текстовые кнопки (более удобнее чем команды)
        @bot.message_handler(func=lambda msg: msg.text == "💅 Новые идеи")
        def btn_ideas(msg):
            self._send_unposted(msg.chat.id)

        @bot.message_handler(func=lambda msg: msg.text == "📋 Сохранённые")
        def btn_saved(msg):
            self._send_saved(msg.chat.id)

        @bot.message_handler(func=lambda msg: msg.text == "🔄 Обновить")
        def btn_update(msg):
            bot.send_message(msg.chat.id, "⏳ Запускаю обновление…")
            db = self.db
            db.set_setting("last_update_time", "")
            added = run_daily_update(db)
            bot.send_message(
                msg.chat.id,
                f"✅ Обновление завершено.\nДобавлено новых идей: <b>{added}</b>",
                reply_markup=self._commands_keyboard(),
            )

        @bot.message_handler(func=lambda msg: msg.text == "♻️ Обновить цикл")
        def btn_refresh(msg):
            refreshed = self.db.refresh_old_ideas(days=7)
            if refreshed > 0:
                bot.send_message(
                    msg.chat.id,
                    f"♻️ Обновлено! Добавлено {refreshed} старых идей в новый цикл.\n"
                    "Нажми <b>«💅 Новые идеи»</b> чтобы их увидеть.",
                    reply_markup=self._commands_keyboard(),
                )
            else:
                bot.send_message(
                    msg.chat.id,
                    "ℹ️ Нет идей старше 7 дней для обновления.",
                    reply_markup=self._commands_keyboard(),
                )

        @bot.message_handler(func=lambda msg: msg.text == "📊 Статистика")
        def btn_status(msg):
            unposted = self.db.count_unposted()
            last_upd = self.db.get_setting("last_update_time", "—")
            # Если есть время, преобразуем его в читаемый формат
            if last_upd != "—":
                try:
                    dt = datetime.datetime.fromisoformat(last_upd)
                    last_upd = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except:
                    pass
            bot.send_message(
                msg.chat.id,
                f"📊 <b>Статистика базы</b>\n\n"
                f"🆕 Новых идей: <b>{unposted}</b>\n"
                f"⏱️ Последнее обновление:\n<b>{last_upd}</b>",
                reply_markup=self._commands_keyboard(),
            )

        @bot.message_handler(func=lambda msg: msg.text == "ℹ️ Справка")
        def btn_help(msg):
            text = (
                "💅 <b>Справка по боту</b>\n\n"
                "<b>Основные функции:</b>\n"
                "💅 <b>Новые идеи</b> — получить свежие (непросмотренные) идеи маникюра\n"
                "📋 <b>Сохранённые</b> — просмотреть последние идеи без изменений\n"
                "🔄 <b>Обновить</b> — принудительно скачать новые идеи прямо сейчас\n"
                "♻️ <b>Обновить цикл</b> — показать старые идеи заново (каждые 7 дней)\n"
                "📊 <b>Статистика</b> — информация о базе данных\n\n"
                "<b>Интервал обновления:</b> каждые 12 часов\n"
                "<b>Источники:</b> Allure, Vogue, Elle, Cosmopolitan, Harper's Bazaar и другие"
            )
            bot.send_message(msg.chat.id, text, reply_markup=self._commands_keyboard())

        # Команды (для быстрого доступа через /команда)
        @bot.message_handler(commands=["ideas"])
        def cmd_ideas(msg):
            self._send_unposted(msg.chat.id)

        @bot.message_handler(commands=["saved"])
        def cmd_saved(msg):
            self._send_saved(msg.chat.id)

        @bot.message_handler(commands=["update"])
        def cmd_update(msg):
            bot.send_message(msg.chat.id, "⏳ Запускаю обновление…")
            db = self.db
            db.set_setting("last_update_time", "")
            added = run_daily_update(db)
            bot.send_message(
                msg.chat.id,
                f"✅ Обновление завершено.\nДобавлено новых идей: <b>{added}</b>",
                reply_markup=self._commands_keyboard(),
            )

        @bot.message_handler(commands=["refresh"])
        def cmd_refresh(msg):
            refreshed = self.db.refresh_old_ideas(days=7)
            if refreshed > 0:
                bot.send_message(
                    msg.chat.id,
                    f"♻️ Обновлено! Добавлено {refreshed} старых идей в новый цикл.\n"
                    "Нажми /ideas чтобы их увидеть.",
                    reply_markup=self._commands_keyboard(),
                )
            else:
                bot.send_message(
                    msg.chat.id,
                    "ℹ️ Нет идей старше 7 дней для обновления.",
                    reply_markup=self._commands_keyboard(),
                )

        @bot.message_handler(commands=["status"])
        def cmd_status(msg):
            unposted = self.db.count_unposted()
            last_upd = self.db.get_setting("last_update_time", "—")
            if last_upd != "—":
                try:
                    dt = datetime.datetime.fromisoformat(last_upd)
                    last_upd = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except:
                    pass
            bot.send_message(
                msg.chat.id,
                f"📊 <b>Статистика</b>\n\n"
                f"Новых идей: <b>{unposted}</b>\n"
                f"Последнее обновление: <b>{last_upd}</b>",
                reply_markup=self._commands_keyboard(),
            )

        @bot.message_handler(commands=["reset"])
        def cmd_reset(msg):
            self.db.set_setting("last_update_time", "")
            bot.send_message(msg.chat.id, "🔁 Время обновления сброшено. Теперь /update скачает всё заново.")

        @bot.message_handler(commands=["debug"])
        def cmd_debug(msg):
            """Команда для отладки - показывает процесс загрузки идей."""
            bot.send_message(msg.chat.id, "🔍 Запускаю тестовую загрузку всех RSS лент...\nСм. логи бота для деталей.")
            
            # Загружаем идеи с подробным логированием
            ideas = fetch_all_ideas()
            
            # Показываем результат пользователю
            if ideas:
                bot.send_message(
                    msg.chat.id,
                    f"✅ <b>Результат:</b>\n\n"
                    f"Загружено идей: <b>{len(ideas)}</b>\n\n"
                    f"<b>Примеры:</b>"
                )
                for idea in ideas[:3]:
                    bot.send_message(
                        msg.chat.id,
                        f"📌 <b>{idea['title'][:50]}...</b>\n"
                        f"<i>{idea.get('source', 'Unknown')}</i>"
                    )
            else:
                bot.send_message(
                    msg.chat.id,
                    "❌ <b>Проблема:</b> Не удалось загрузить идеи.\n\n"
                    "Возможные причины:\n"
                    "• RSS источники недоступны\n"
                    "• Фильтр ключевых слов слишком строгий\n"
                    "• Ошибка сети\n\n"
                    "Проверьте логи бота для деталей."
                )

        # Обработка всех остальных сообщений
        @bot.message_handler(func=lambda msg: True)
        def handle_unknown(msg):
            bot.send_message(
                msg.chat.id,
                "❓ Используй кнопки внизу экрана для навигации по боту.",
                reply_markup=self._commands_keyboard(),
            )

    # Вспомогательные методы отправки
   
    def _send_unposted(self, chat_id: int):
        """Отправить непросмотренные идеи и пометить их как отправленные."""
        ideas = self.db.get_unposted(limit=5)
        if not ideas:
            self.bot.send_message(
                chat_id,
                "😔 Новых идей пока нет.\n"
                "Попробуй нажать <b>«🔄 Обновить»</b> или вернись позже!",
                reply_markup=self._commands_keyboard(),
            )
            return

        self.bot.send_message(
            chat_id,
            f"💅 <b>Вот {len(ideas)} новых идей для вас:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        for i, idea in enumerate(ideas, start=1):
            try:
                self.bot.send_message(chat_id, self._format_idea(idea, index=i))
                time.sleep(0.1)  # Небольшая задержка между сообщениями
            except Exception as exc:
                logger.warning("Не удалось отправить идею #%d: %s", idea["id"], exc)

        self.db.mark_posted([idea["id"] for idea in ideas])
        remaining = self.db.count_unposted()
        self.bot.send_message(
            chat_id,
            f"✅ <b>Готово!</b>\n"
            f"Ещё новых идей: <b>{remaining}</b>\n\n"
            f"Нажми <b>«💅 Новые идеи»</b> для следующей партии",
            reply_markup=self._commands_keyboard(),
        )

    def _send_saved(self, chat_id: int):
        """
        Показать последние 10 идей из базы.
        НЕ меняет поле posted — только чтение.
        """
        ideas = self.db.get_all_recent(limit=10)
        if not ideas:
            self.bot.send_message(
                chat_id,
                "📭 База пока пуста. Нажми <b>«🔄 Обновить»</b> для загрузки идей.",
                reply_markup=self._commands_keyboard(),
            )
            return

        self.bot.send_message(
            chat_id,
            "📋 <b>Последние сохранённые идеи</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        for i, idea in enumerate(ideas, start=1):
            try:
                self.bot.send_message(
                    chat_id,
                    self._format_idea(idea, index=i, show_status=True),
                )
                time.sleep(0.1)
            except Exception as exc:
                logger.warning("Ошибка отправки сохранённой идеи: %s", exc)

        self.bot.send_message(
            chat_id,
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Выбери действие:",
            reply_markup=self._commands_keyboard(),
        )


    def run(self):
        logger.info("Бот запущен. Ожидаю сообщений…")
        self.bot.infinity_polling(timeout=60, long_polling_timeout=60)


# Точка входа

def main():
    db = Database(DB_FILE)

    # Первый запуск: сразу парсим, не ждём 01:00
    run_daily_update(db)

    # Фоновый планировщик суточных обновлений
    threading.Thread(target=_daily_scheduler, args=(db,), daemon=True).start()

    # Запуск бота (блокирующий вызов)
    bot = ManicureBot(BOT_TOKEN, db)
    try:
        bot.run()
    finally:
        db.close()


if __name__ == "__main__":
    main()
