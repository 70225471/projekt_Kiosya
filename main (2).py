# -*- coding: utf-8 -*-
import telebot
import requests
from bs4 import BeautifulSoup
import feedparser
import json
import sqlite3
import pygame
import threading
import time
import os
import logging
import urllib.parse
import re
import datetime
import sys
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import OrderedDict
# Добавьте в начало файла после импортов:
import socket
import dns.resolver  # 可能需要安装: pip install dnspython


def check_telegram_api():
    """Проверка доступности Telegram API"""
    try:
        # Проверяем DNS разрешение
        socket.gethostbyname('api.telegram.org')
        return True
    except socket.gaierror:
        logger.error("Не удается разрешить DNS имя api.telegram.org")
        return False


def get_telegram_bot_info(token):
    """Получение информации о боте с повторными попытками"""
    url = f"https://api.telegram.org/bot{token}/getMe"

    for attempt in range(5):
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут при подключении к Telegram API (попытка {attempt + 1}/5)")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Ошибка соединения: {e}")
        except Exception as e:
            logger.warning(f"Неизвестная ошибка: {e}")

        if attempt < 4:
            time.sleep(5)  # Ждем 5 секунд перед следующей попыткой

    return None


# Замените функцию run_bot на эту:
def run_bot(bot_instance, stop_event):
    """Запуск бота с улучшенной обработкой ошибок"""
    # Проверяем доступность Telegram API перед запуском
    logger.info("🔍 Проверка подключения к Telegram API...")

    if not check_telegram_api():
        logger.error("❌ Не удается подключиться к api.telegram.org")
        logger.error("Проверьте:")
        logger.error("  1. Интернет-соединение")
        logger.error("  2. Файрвол/антивирус (может блокировать Telegram)")
        logger.error("  3. DNS настройки")
        return

    # Проверяем валидность токена
    bot_info = get_telegram_bot_info(bot_instance.token)
    if not bot_info or not bot_info.get('ok'):
        logger.error("❌ Неверный BOT_TOKEN или бот не существует!")
        logger.error("Проверьте токен бота в переменной окружения BOT_TOKEN")
        return

    logger.info(f"✅ Бот найден: @{bot_info['result']['username']}")

    # Запускаем polling с обработкой ошибок
    while not stop_event.is_set():
        try:
            logger.info("🤖 Бот запущен и готов к работе")
            bot_instance.polling(none_stop=True, timeout=30, interval=1)
        except requests.exceptions.ConnectionError as e:
            if stop_event.is_set():
                break
            logger.error(f"❌ Ошибка соединения с Telegram: {e}")
            logger.info("Повторная попытка через 10 секунд...")
            time.sleep(10)
        except requests.exceptions.Timeout:
            logger.warning("Таймаут при подключении к Telegram API")
            time.sleep(5)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.error(f"❌ Общая ошибка бота: {e}")
            time.sleep(5)
# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_log.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
DB_FILE = 'manicure_data.db'
JSON_BACKUP = 'manicure_data_backup.json'
AUTO_UPDATE_INTERVAL = 7200  # 2 часа

RSS_FEEDS = [
    'https://www.allure.com/feed/rss',
    'https://www.vogue.com/feed/rss',
    'https://www.cosmopolitan.com/rss/all.xml',
    'https://www.elle.com/rss/all.xml',
    'https://www.harpersbazaar.com/rss/all.xml',
    'https://www.instyle.com/feed/rss',
    'https://www.glamour.com/feed/rss'
]

MANICURE_KEYWORDS = [
    'manicure', 'nails', 'nail art', 'pedicure', 'gel polish', 'acrylic nails',
    'nail design', 'nail care', 'shellac', 'dip powder', 'nail extensions',
    'french manicure', 'ombre nails', 'chrome nails', 'matte nails'
]

# Глобальные переменные для предотвращения дублирования
_rss_parse_lock = threading.Lock()
_rss_last_parse_time = 0
_rss_cached_results = []
_rss_is_parsing = False


def create_session_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class DatabaseManager:
    def __init__(self, db_file):
        self.db_file = db_file
        self.conn = None
        self.lock = threading.RLock()
        self.connect()
        self.create_tables()

    def connect(self):
        try:
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            logger.info("Подключение к БД успешно.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    def create_tables(self):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    link TEXT UNIQUE,
                    summary TEXT,
                    image_url TEXT,
                    source TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    site TEXT NOT NULL,
                    result TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT UNIQUE NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            self.conn.commit()
        logger.info("Таблицы БД готовы")

    def load_news(self, limit=50):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT title, link, summary, image_url, source, timestamp 
                FROM news 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
        return [{
            'title': row[0],
            'link': row[1],
            'summary': row[2] or '',
            'image': row[3] or '',
            'source': row[4] or '',
            'timestamp': row[5]
        } for row in rows]

    def save_news(self, news_list):
        if not news_list:
            return 0

        saved_count = 0
        with self.lock:
            cursor = self.conn.cursor()
            # Удаляем старые новости (старше 14 дней)
            cursor.execute("DELETE FROM news WHERE timestamp < datetime('now', '-14 days')")

            for news in news_list:
                title = news.get('title', '')[:200]
                link = news.get('link', '')
                if not title or not link:
                    continue

                summary = news.get('summary', '')[:500]
                image_url = news.get('image', '')[:500]
                source = news.get('source', '')

                try:
                    cursor.execute("""
                        INSERT OR IGNORE INTO news (title, link, summary, image_url, source) 
                        VALUES (?, ?, ?, ?, ?)
                    """, (title, link, summary, image_url, source))
                    if cursor.rowcount > 0:
                        saved_count += 1
                except sqlite3.Error:
                    pass

            self.conn.commit()

        if saved_count > 0:
            logger.info(f"Сохранено {saved_count} новых новостей")
        return saved_count

    def load_keywords(self):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords")
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    def add_keyword(self, keyword):
        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
                self.conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error:
            return False

    def load_setting(self, key, default=None):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
        return row[0] if row else default

    def save_setting(self, key, value):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            self.conn.commit()

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None


def backup_to_json(db_manager):
    try:
        data = {
            'news': db_manager.load_news(limit=100),
            'keywords': db_manager.load_keywords(),
            'backup_date': datetime.datetime.now().isoformat()
        }
        with open(JSON_BACKUP, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logger.info("Бэкап создан")
        return True
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")
        return False


def restore_from_json(db_manager):
    if os.path.exists(JSON_BACKUP):
        try:
            with open(JSON_BACKUP, 'r', encoding='utf-8') as f:
                data = json.load(f)
            db_manager.save_news(data.get('news', []))
            for keyword in data.get('keywords', []):
                db_manager.add_keyword(keyword)
            logger.info("Данные восстановлены из JSON")
            return True
        except Exception as e:
            logger.error(f"Ошибка восстановления: {e}")
    return False


def parse_rss_feeds_single(keywords):
    """Реальная функция парсинга RSS (без дублирования)"""
    logger.info("🔄 Начинаем парсинг RSS лент...")
    all_news = []
    processed_urls = set()

    for feed_url in RSS_FEEDS:
        if feed_url in processed_urls:
            continue
        processed_urls.add(feed_url)

        try:
            feedparser.USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            feed = feedparser.parse(feed_url)

            source_name = feed_url.split('/')[2] if '//' in feed_url else feed_url
            feed_news_count = 0

            for entry in feed.entries[:15]:
                title = entry.get('title', '').strip()
                link = entry.get('link', '').strip()

                if not title or not link:
                    continue

                summary = entry.get('summary', '') or entry.get('description', '') or ''
                search_text = f"{title} {summary}".lower()

                if any(keyword.lower() in search_text for keyword in keywords):
                    image_url = ''
                    if 'media_content' in entry and entry.media_content:
                        image_url = entry.media_content[0].get('url', '')

                    all_news.append({
                        'title': title[:150],
                        'link': link,
                        'summary': summary[:300],
                        'image': image_url[:200],
                        'source': source_name
                    })
                    feed_news_count += 1

            logger.info(f"✓ {source_name}: найдено {feed_news_count} новостей")
            time.sleep(0.3)

        except Exception as e:
            logger.error(f"✗ Ошибка {feed_url}: {e}")

    logger.info(f"✅ Парсинг завершен. Всего найдено {len(all_news)} новостей")
    return all_news


def parse_rss_feeds(keywords, force=False):
    """Обертка для парсинга RSS с защитой от дублирования"""
    global _rss_parse_lock, _rss_last_parse_time, _rss_cached_results, _rss_is_parsing

    with _rss_parse_lock:
        # Если уже идет парсинг, возвращаем кэш
        if _rss_is_parsing:
            logger.info("⏳ Парсинг RSS уже выполняется, возвращаем кэшированные результаты")
            return _rss_cached_results if _rss_cached_results else []

        # Проверяем кэш
        current_time = time.time()
        if not force and _rss_cached_results and (current_time - _rss_last_parse_time) < 1800:  # 30 минут кэш
            logger.info(f"💾 Используем кэш RSS (обновлен {int((current_time - _rss_last_parse_time) / 60)} мин назад)")
            return _rss_cached_results.copy()

        # Запускаем парсинг
        _rss_is_parsing = True

    try:
        # Выполняем парсинг
        results = parse_rss_feeds_single(keywords)

        with _rss_parse_lock:
            _rss_cached_results = results
            _rss_last_parse_time = time.time()

        return results

    finally:
        with _rss_parse_lock:
            _rss_is_parsing = False


class ManicureBot(telebot.TeleBot):
    def __init__(self, token, db_manager):
        super().__init__(token)
        self.db_manager = db_manager
        self.keywords = self._load_keywords()
        self.setup_handlers()

    def _load_keywords(self):
        stored = self.db_manager.load_keywords()
        return list(OrderedDict.fromkeys(stored + MANICURE_KEYWORDS))

    def setup_handlers(self):
        @self.message_handler(commands=['start'])
        def start_cmd(message):
            self.reply_to(message,
                          "👋 Привет! Я бот для маникюра.\n"
                          "🔍 Используй /help для списка команд."
                          )

        @self.message_handler(commands=['help'])
        def help_cmd(message):
            help_text = """
🤖 *Команды бота:*

/start — Приветствие
/help — Эта справка
/news — Свежие новости о маникюре
/add_keyword <слово> — Добавить ключевое слово
/list_keywords — Список ключевых слов

*Примеры:*
/add_keyword nail art
            """
            self.reply_to(message, help_text, parse_mode='Markdown')

        @self.message_handler(commands=['news'])
        def news_cmd(message):
            status_msg = self.reply_to(message, "🔍 Поиск новостей...")

            try:
                news = parse_rss_feeds(self.keywords)

                if not news:
                    self.edit_message_text(
                        "😕 Новостей не найдено.\nПопробуйте позже.",
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id
                    )
                    return

                self.db_manager.save_news(news)

                response = "📰 *Последние новости:*\n\n"
                for i, item in enumerate(news[:5], 1):
                    response += f"{i}. *{item['title'][:80]}*\n"
                    response += f"🔗 {item['link'][:60]}...\n\n"

                self.edit_message_text(
                    response[:4000],
                    chat_id=status_msg.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode='Markdown'
                )

            except Exception as e:
                logger.error(f"Ошибка в /news: {e}")
                self.edit_message_text(
                    "❌ Ошибка получения новостей",
                    chat_id=status_msg.chat.id,
                    message_id=status_msg.message_id
                )

        @self.message_handler(commands=['add_keyword'])
        def add_keyword_cmd(message):
            parts = message.text.split(maxsplit=1)
            if len(parts) < 2:
                self.reply_to(message, "❓ Укажите слово: /add_keyword glitter")
                return

            keyword = parts[1].lower().strip()
            if self.db_manager.add_keyword(keyword):
                self.keywords = self._load_keywords()
                self.reply_to(message, f"✅ Добавлено: *{keyword}*", parse_mode='Markdown')
            else:
                self.reply_to(message, f"⚠️ Слово *{keyword}* уже есть", parse_mode='Markdown')

        @self.message_handler(commands=['list_keywords'])
        def list_keywords_cmd(message):
            keywords = self.keywords[:20]
            response = "🔑 *Ключевые слова:*\n\n" + "\n".join(f"• {kw}" for kw in keywords)
            if len(self.keywords) > 20:
                response += f"\n\n... и еще {len(self.keywords) - 20}"
            self.reply_to(message, response, parse_mode='Markdown')


def run_bot(bot_instance, stop_event):
    """Запуск бота"""
    while not stop_event.is_set():
        try:
            logger.info("🤖 Бот запущен")
            bot_instance.polling(none_stop=True, timeout=30)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.error(f"Ошибка бота: {e}")
            time.sleep(5)


def auto_update(db_manager):
    """Автоматическое обновление новостей"""
    # Ждем 60 секунд перед первым обновлением
    time.sleep(60)

    while True:
        try:
            logger.info("🔄 Автообновление RSS...")
            keywords = db_manager.load_keywords() or MANICURE_KEYWORDS

            # force=True для принудительного обновления кэша
            news = parse_rss_feeds(keywords, force=True)

            if news:
                saved = db_manager.save_news(news)
                if saved > 0:
                    backup_to_json(db_manager)
                    logger.info(f"✅ Автообновление: +{saved} новостей")
            else:
                logger.info("ℹ️ Новых новостей не найдено")

            time.sleep(AUTO_UPDATE_INTERVAL)

        except Exception as e:
            logger.error(f"❌ Ошибка автообновления: {e}")
            time.sleep(60)


def create_gui(bot_instance, db_manager):
    """Создание GUI"""
    pygame.init()
    screen = pygame.display.set_mode((600, 500))
    pygame.display.set_caption("Manicure Bot Manager")
    clock = pygame.time.Clock()

    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)
    GRAY = (200, 200, 200)
    BLUE = (100, 150, 200)
    GREEN = (100, 200, 100)
    RED = (200, 100, 100)

    font_title = pygame.font.Font(None, 24)
    font_button = pygame.font.Font(None, 18)
    font_log = pygame.font.Font(None, 14)

    log_lines = []
    bot_stop_event = threading.Event()
    bot_thread = None

    class GUILogHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            log_lines.append(msg)
            if len(log_lines) > 100:
                log_lines.pop(0)

    gui_handler = GUILogHandler()
    gui_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    logging.getLogger().addHandler(gui_handler)

    buttons = []
    btn_w, btn_h = 180, 35
    start_x, start_y = 20, 50
    spacing = 45

    def start_bot():
        nonlocal bot_thread
        if bot_thread and bot_thread.is_alive():
            log_lines.append("⚠️ Бот уже работает")
            return
        bot_stop_event.clear()
        bot_thread = threading.Thread(target=run_bot, args=(bot_instance, bot_stop_event), daemon=True)
        bot_thread.start()
        log_lines.append("✅ Бот запущен")

    def stop_bot():
        bot_stop_event.set()
        log_lines.append("🛑 Остановка бота...")

    def update_news():
        try:
            log_lines.append("🔄 Обновление RSS...")
            keywords = db_manager.load_keywords() or MANICURE_KEYWORDS
            news = parse_rss_feeds(keywords, force=True)
            saved = db_manager.save_news(news)
            backup_to_json(db_manager)
            log_lines.append(f"✅ Добавлено {saved} новостей")
        except Exception as e:
            log_lines.append(f"❌ Ошибка: {e}")

    def create_backup():
        if backup_to_json(db_manager):
            log_lines.append("💾 Бэкап создан")
        else:
            log_lines.append("❌ Ошибка бэкапа")

    def exit_app():
        db_manager.close()
        pygame.quit()
        sys.exit()

    btn_config = [
        ("▶ Запустить бота", start_bot, GREEN),
        ("⏹ Остановить бота", stop_bot, RED),
        ("🔄 Обновить RSS", update_news, BLUE),
        ("💾 Создать бэкап", create_backup, BLUE),
        ("🚪 Выход", exit_app, RED)
    ]

    for i, (text, func, color) in enumerate(btn_config):
        buttons.append({
            'text': text,
            'rect': pygame.Rect(start_x, start_y + i * spacing, btn_w, btn_h),
            'func': func,
            'color': color
        })

    log_rect = pygame.Rect(220, 50, 360, 440)

    running = True
    scroll = 0
    max_lines = 30

    while running:
        screen.fill(WHITE)

        title = font_title.render("Manicure Bot Manager", True, BLACK)
        screen.blit(title, (20, 10))

        for btn in buttons:
            pygame.draw.rect(screen, btn['color'], btn['rect'])
            pygame.draw.rect(screen, BLACK, btn['rect'], 1)
            text = font_button.render(btn['text'], True, BLACK)
            text_rect = text.get_rect(center=btn['rect'].center)
            screen.blit(text, text_rect)

        pygame.draw.rect(screen, GRAY, log_rect, 2)

        visible = log_lines[scroll:scroll + max_lines]
        for i, line in enumerate(visible):
            color = BLACK
            if '✅' in line:
                color = (0, 150, 0)
            elif '❌' in line or 'Ошибка' in line:
                color = RED
            elif '⚠️' in line:
                color = (255, 165, 0)

            if len(line) > 50:
                line = line[:47] + '...'

            text = font_log.render(line, True, color)
            screen.blit(text, (log_rect.x + 5, log_rect.y + 5 + i * 14))

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    pos = pygame.mouse.get_pos()
                    for btn in buttons:
                        if btn['rect'].collidepoint(pos):
                            btn['func']()
                elif event.button == 4:
                    scroll = max(0, scroll - 1)
                elif event.button == 5:
                    scroll = min(max(0, len(log_lines) - max_lines), scroll + 1)

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("\n" + "=" * 50)
        print("ОШИБКА: BOT_TOKEN не задан!")
        print("Установите переменную окружения BOT_TOKEN")
        print("Пример: set BOT_TOKEN=your_token_here")
        print("=" * 50 + "\n")
        sys.exit(1)

    try:
        # Инициализация БД
        db = DatabaseManager(DB_FILE)

        # Восстановление из бэкапа
        restore_from_json(db)

        # Загружаем ключевые слова
        keywords = db.load_keywords() or MANICURE_KEYWORDS

        # Проверяем, есть ли новости в БД
        existing_news = db.load_news(limit=1)
        if not existing_news:
            logger.info("📰 Загружаем начальные новости...")
            initial_news = parse_rss_feeds(keywords, force=True)
            db.save_news(initial_news)
            backup_to_json(db)
        else:
            logger.info(f"📊 В базе уже есть новости")

        # Запуск автообновления (один поток)
        update_thread = threading.Thread(target=auto_update, args=(db,), daemon=True)
        update_thread.start()

        # Создание бота
        bot = ManicureBot(BOT_TOKEN, db)

        # Запуск GUI
        create_gui(bot, db)

    except KeyboardInterrupt:
        logger.info("👋 Программа остановлена")
        sys.exit(0)
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
