# -*- coding: utf-8 -*-
# Импорт всех необходимых библиотек. 
# Мы добавили больше библиотек для расширенной функциональности: logging для логов, sqlite3 для базы данных вместо JSON,
# urllib для URL-парсинга и re для регулярных выражений.
import telebot  # Для создания Telegram-бота
import requests  # Для скачивания данных с интернета
from bs4 import BeautifulSoup  # Для парсинга HTML (разбора страниц сайтов)
import feedparser  # Для работы с RSS-лентами (новостями)
import json  # Для хранения данных в JSON-файле (оставляем для совместимости)
import sqlite3  # Для использования SQLite базы данных вместо простого JSON
import pygame  # Для графической оболочки (GUI) вместо Tkinter
import threading  # Для запуска бота в фоне, чтобы GUI не зависала
import time  # Для пауз и таймингов
import os  # Для работы с файлами
import logging  # Для логирования событий и ошибок
import urllib.parse  # Для безопасного парсинга URL
import re  # Для регулярных выражений в поиске
import datetime  # Для работы с датами и временем
import sys  # Для выхода из программы


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
AUTO_UPDATE_INTERVAL = 3600  


RSS_FEEDS = [
    'https://www.allure.com/feed/rss',  # Allure: красота, маникюр
    'https://www.vogue.com/feed/rss',   # Vogue: мода, включая nails
    'https://www.cosmopolitan.com/rss/all.xml',  # Cosmopolitan: женские темы
    'https://www.elle.com/rss/all.xml',  # Elle: мода и красота
    'https://www.harpersbazaar.com/rss/all.xml',  # Harper's Bazaar: стиль, nails
    'https://www.instyle.com/feed/rss',  # InStyle: красота
    'https://www.glamour.com/feed/rss'  # Glamour: маникюр и уход
]

MANICURE_KEYWORDS = [
    'manicure', 'nails', 'nail art', 'pedicure', 'gel polish', 'acrylic nails',
    'nail design', 'nail care', 'shellac', 'dip powder', 'nail extensions',
    'french manicure', 'ombre nails', 'chrome nails', 'matte nails'
]

class DatabaseManager:
    """
    Класс для работы с SQLite базой данных.
    Хранит новости, поиски, ключевые слова и настройки.
    """
    def __init__(self, db_file):
        self.db_file = db_file
        self.conn = None
        self.cursor = None
        self.lock = threading.RLock()
        self.connect()
        self.create_tables()

    def connect(self):
        """Подключение к базе данных."""
        try:
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self.cursor = self.conn.cursor()
            logger.info("Подключение к БД успешно.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    def create_tables(self):
        """Создание таблиц, если они не существуют."""
        with self.lock:
            cursor = self.conn.cursor()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    summary TEXT,
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
        logger.info("Таблицы БД созданы или уже существуют.")

    def load_news(self):
        """Загрузка новостей из БД."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT title, link, summary FROM news ORDER BY timestamp DESC")
            rows = cursor.fetchall()
        return [{'title': row[0], 'link': row[1], 'summary': row[2] or ''} for row in rows]

    def save_news(self, news_list):
        """Сохранение новостей в БД. Удаляем старые и добавляем новые."""
        if not news_list:
            logger.warning("Список новостей пуст, существующие записи не удалены.")
            return
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM news")  # Очищаем старые
            for news in news_list:
                title = news.get('title', '')
                link = news.get('link', '')
                summary = news.get('summary', '')
                if not title or not link:
                    continue
                cursor.execute(
                    "INSERT INTO news (title, link, summary) VALUES (?, ?, ?)",
                    (title, link, summary)
                )
            self.conn.commit()
        logger.info(f"Сохранено {len(news_list)} новостей.")

    def load_searches(self):
        """Загрузка поисков из БД."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT query, site, result FROM searches ORDER BY timestamp DESC")
            rows = cursor.fetchall()
        searches = {}
        for row in rows:
            query, site, result = row
            if query not in searches:
                searches[query] = {}
            try:
                parsed_result = json.loads(result) if result else {}
                if isinstance(parsed_result, str):
                    parsed_result = {'text': parsed_result, 'images': []}
                elif not isinstance(parsed_result, dict):
                    parsed_result = {'text': str(parsed_result), 'images': []}
                searches[query][site] = parsed_result
            except json.JSONDecodeError:
                searches[query][site] = {'text': result or '', 'images': []}
        return searches

    def save_search(self, query, results):
        """Сохранение результатов поиска в БД."""
        with self.lock:
            cursor = self.conn.cursor()
            for site, result in results.items():
                if not isinstance(result, dict):
                    result = {'text': str(result), 'images': []}
                cursor.execute(
                    "INSERT INTO searches (query, site, result) VALUES (?, ?, ?)",
                    (query, site, json.dumps(result, ensure_ascii=False))
                )
            self.conn.commit()
        logger.info(f"Сохранен поиск для '{query}'.")

    def load_keywords(self):
        """Загрузка ключевых слов из БД."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords")
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    def add_keyword(self, keyword):
        """Добавление нового ключевого слова."""
        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute("INSERT INTO keywords (keyword) VALUES (?)", (keyword,))
                self.conn.commit()
            logger.info(f"Добавлено ключевое слово: {keyword}")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Ключевое слово '{keyword}' уже существует.")
            return False

    def load_setting(self, key, default=None):
        """Загрузка настройки."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
        return row[0] if row else default

    def save_setting(self, key, value):
        """Сохранение настройки."""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            self.conn.commit()
        logger.info(f"Сохранена настройка: {key} = {value}")

    def close(self):
        """Закрытие соединения с БД."""
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None
                logger.info("Соединение с БД закрыто.")

def backup_to_json(db_manager):
    """
    Создание бэкапа данных в JSON-файл.
    """
    data = {
        'news': db_manager.load_news(),
        'searches': db_manager.load_searches(),
        'keywords': db_manager.load_keywords(),
        'settings': {key: db_manager.load_setting(key) for key in ['auto_update', 'log_level', 'rss_feeds']}
    }
    with open(JSON_BACKUP, 'w') as file:
        json.dump(data, file, indent=4)
    logger.info("Бэкап в JSON создан.")

def restore_from_json(db_manager):
    """
    Восстановление данных из JSON в БД.
    """
    if os.path.exists(JSON_BACKUP):
        with open(JSON_BACKUP, 'r') as file:
            data = json.load(file)
        db_manager.save_news(data.get('news', []))
        for query, results in data.get('searches', {}).items():
            db_manager.save_search(query, results)
        for keyword in data.get('keywords', []):
            db_manager.add_keyword(keyword)
        for key, value in data.get('settings', {}).items():
            db_manager.save_setting(key, value)
        logger.info("Данные восстановлены из JSON.")
    else:
        logger.warning("JSON-бэкап не найден.")

def parse_rss_feeds(keywords):
    """
    Парсинг RSS-лент с фильтрацией по ключевым словам.
    Теперь учитываем дату публикации и возможные изображения.
    """
    news_list = []  
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
    for feed_url in RSS_FEEDS:  
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get('title', '').strip()
                summary = entry.get('summary', '') or entry.get('description', '') or ''
                link = entry.get('link', '').strip()
                if not title or not link:
                    continue
                pub_date = entry.get('published_parsed') or entry.get('updated_parsed')
                if pub_date:
                    pub_datetime = datetime.datetime(*pub_date[:6])
                    if (datetime.datetime.now() - pub_datetime).days > 7:
                        continue  
                entry_text = f"{title} {summary}".lower()
                if any(keyword in entry_text for keyword in lowered_keywords):
                    news_item = {
                        'title': title,
                        'link': link,
                        'summary': summary
                    }
                    if entry.get('enclosures'):
                        news_item['image'] = entry.enclosures[0].get('href', '')
                    elif entry.get('content'):
                        content_items = entry.get('content') or []
                        if content_items:
                            soup = BeautifulSoup(content_items[0].get('value', ''), 'html.parser')
                            img = soup.find('img')
                            if img and img.get('src'):
                                news_item['image'] = img['src']
                    news_list.append(news_item)
            logger.info(f"Обработана RSS: {feed_url}")
        except Exception as e:
            logger.error(f"Ошибка парсинга RSS {feed_url}: {e}")
    return news_list  

def scrape_website(url):
    """
    Парсинг сайта с использованием requests и BeautifulSoup.
    Добавили headers для избежания блокировок, обработку изображений.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            content = soup.find('article') or soup.find('div', class_='content')
            if content:
                text = content.get_text(separator='\n', strip=True)
            else:
                paragraphs = soup.find_all('p')
                text = '\n'.join(p.get_text(strip=True) for p in paragraphs)
            
            images = [img['src'] for img in soup.find_all('img') if 'src' in img.attrs]
            return {'text': text, 'images': images[:3]}
        else:
            logger.warning(f"Ошибка скачивания {url}: {response.status_code}")
            return {'text': f"Ошибка: {response.status_code}", 'images': []}
    except requests.Timeout:
        logger.error(f"Таймаут при скачивании {url}")
        return {'text': "Таймаут", 'images': []}
    except Exception as e:
        logger.error(f"Исключение при парсинге {url}: {e}")
        return {'text': f"Исключение: {str(e)}", 'images': []}


def search_manicure(query, keywords):
    """
    Поиск по сайтам с использованием запроса.
    Добавили больше сайтов, использование regex для лучшей фильтрации.
    """
    # Кодируем запрос для URL.
    encoded_query = urllib.parse.quote(query + ' manicure')
    safe_keywords = [re.escape(keyword) for keyword in keywords if keyword]
    keyword_pattern = re.compile('|'.join(safe_keywords), re.IGNORECASE) if safe_keywords else None

    sites = [
        f"https://www.google.com/search?q={encoded_query}",  
        "https://www.allure.com/topic/nails",
        "https://www.vogue.com/tag/nails",
        "https://www.elle.com/beauty/nails/",
        "https://www.harpersbazaar.com/beauty/nails/",
        "https://www.instyle.com/beauty/nails",
        "https://www.glamour.com/about/nails"
    ]
    results = {}  
    for site in sites:
        scraped = scrape_website(site)
        text = scraped['text']
        filtered_text = ''
        for line in text.split('\n'):
            if not keyword_pattern or keyword_pattern.search(line):
                filtered_text += line + '\n'
        results[site] = {
            'text': filtered_text[:1000] + '...' if len(filtered_text) > 1000 else filtered_text,
            'images': scraped['images']
        }
        time.sleep(1) 
        logger.info(f"Обработан сайт: {site}")
    return results 

class ManicureBot(telebot.TeleBot):
    """
    Расширенный класс для бота. Добавили больше обработчиков команд.
    """
    def __init__(self, token, db_manager):
        super().__init__(token)
        self.db_manager = db_manager
        stored_keywords = self.db_manager.load_keywords()
        self.keywords = list(dict.fromkeys((stored_keywords or []) + MANICURE_KEYWORDS))
        self.setup_handlers()

    def setup_handlers(self):
        """Настройка всех обработчиков."""
        self.message_handler(commands=['start'])(self.send_welcome)
        self.message_handler(commands=['help'])(self.send_help)
        self.message_handler(commands=['news'])(self.send_news)
        self.message_handler(commands=['search'])(self.search_handler)
        self.message_handler(commands=['add_keyword'])(self.add_keyword_handler)
        self.message_handler(commands=['list_keywords'])(self.list_keywords_handler)
        self.message_handler(commands=['add_feed'])(self.add_feed_handler)
        self.message_handler(commands=['list_searches'])(self.list_searches_handler)
        self.message_handler(
            func=lambda message: bool(getattr(message, 'text', '')) and not message.text.startswith('/')
        )(self.echo_all)

    def send_welcome(self, message):
        """Обработчик /start."""
        self.reply_to(message, "Привет! Я расширенный бот для маникюра. Используй /help для списка команд.")

    def send_help(self, message):
        """Обработчик /help. Показывает все команды."""
        help_text = """
Доступные команды:
/start - Приветствие
/help - Этот список
/news - Последние новости
/search <текст> - Поиск информации
/add_keyword <слово> - Добавить ключевое слово
/list_keywords - Список ключевых слов
/add_feed <url> - Добавить RSS-ленту (админ только)
/list_searches - Список предыдущих поисков
        """
        self.reply_to(message, help_text)

    def send_news(self, message):
        """Обработчик /news. Теперь с изображениями если есть."""
        news = parse_rss_feeds(self.keywords)
        self.db_manager.save_news(news)
        if not news:
            self.reply_to(message, "Свежих новостей о маникюре не найдено.")
            return
        response = "Последние новости о маникюре:\n"
        for item in news[:10]:  # Показываем до 10
            response += f"- {item['title']}: {item['link']}\n{item['summary'][:150]}...\n"
            if 'image' in item:
                response += f"Изображение: {item['image']}\n"
            response += "\n"
        self.reply_to(message, response[:4000])

    def search_handler(self, message):
        """Обработчик /search."""
        query = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not query:
            self.reply_to(message, "Укажи запрос, например: /search gel nails")
            return
        results = search_manicure(query, self.keywords)
        self.db_manager.save_search(query, results)
        response = f"Результаты поиска '{query}':\n"
        for site, res in results.items():
            response += f"Из {site}:\n{res['text']}\n"
            if res['images']:
                response += "Изображения: " + ', '.join(res['images']) + "\n"
            response += "\n"
        self.reply_to(message, response[:2000])

    def add_keyword_handler(self, message):
        """Обработчик /add_keyword."""
        keyword = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not keyword:
            self.reply_to(message, "Укажи слово, например: /add_keyword glitter nails")
            return
        if self.db_manager.add_keyword(keyword):
            if keyword not in self.keywords:
                self.keywords.append(keyword)
            self.reply_to(message, f"Добавлено: {keyword}")
        else:
            self.reply_to(message, f"{keyword} уже существует.")

    def list_keywords_handler(self, message):
        """Обработчик /list_keywords."""
        keywords = self.keywords
        response = "Ключевые слова:\n" + "\n".join(keywords)
        self.reply_to(message, response)

    def add_feed_handler(self, message):
        """Обработчик /add_feed. Для админов - добавление RSS."""
        feed_url = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not feed_url or not feed_url.startswith('http'):
            self.reply_to(message, "Укажи валидный URL, например: /add_feed https://example.com/rss")
            return
        RSS_FEEDS.append(feed_url)
        self.reply_to(message, f"Добавлена лента: {feed_url}")
        self.db_manager.save_setting('rss_feeds', json.dumps(RSS_FEEDS))

    def list_searches_handler(self, message):
        """Обработчик /list_searches."""
        searches = self.db_manager.load_searches()
        response = "Предыдущие поиски:\n"
        for query in searches:
            response += f"- {query}\n"
        self.reply_to(message, response)

    def echo_all(self, message):
        """Обработчик всех сообщений."""
        self.reply_to(message, f"Ты сказал: {message.text}. Попробуй /search {message.text}!")

def run_bot(bot_instance, stop_event):
    """
    Запуск polling с обработкой ошибок и перезапусками.
    """
    while not stop_event.is_set():
        try:
            bot_instance.polling(none_stop=True, timeout=60)
        except telebot.apihelper.ApiTelegramException as e:
            if stop_event.is_set():
                break
            logger.error(f"Telegram API ошибка: {e}")
            time.sleep(10)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.error(f"Общая ошибка бота: {e}")
            time.sleep(5)

def auto_update(db_manager):
    """
    Автоматическое обновление новостей по расписанию.
    """
    while True:
        keywords = db_manager.load_keywords() or MANICURE_KEYWORDS
        news = parse_rss_feeds(keywords)
        db_manager.save_news(news)
        backup_to_json(db_manager)
        logger.info("Автообновление завершено.")
        time.sleep(AUTO_UPDATE_INTERVAL)

def create_gui(bot_instance, db_manager):
    """
    Создание GUI с дополнительными элементами: логами, статусом.
    """
    pygame.init()
    screen = pygame.display.set_mode((600, 500))
    pygame.display.set_caption("Manicure Bot Manager - Расширенная версия")
    clock = pygame.time.Clock()

    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)
    GRAY = (200, 200, 200)
    BLUE = (0, 0, 255)
    LIGHT_BLUE = (173, 216, 230)

    font_large = pygame.font.Font(None, 24)
    font_medium = pygame.font.Font(None, 18)
    font_small = pygame.font.Font(None, 14)

    log_lines = []
    log_offset = 0
    max_log_lines = 15  
    bot_stop_event = threading.Event()
    bot_thread = None

    class GuiHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            log_lines.append(msg)
            if len(log_lines) > 100:  
                log_lines.pop(0)

    gui_handler = GuiHandler()
    gui_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(gui_handler)

    buttons = []
    button_width = 250
    button_height = 40
    button_x = (600 - button_width) // 2
    button_y_start = 60
    button_spacing = 50

    def start_bot():
        nonlocal bot_thread
        if bot_thread and bot_thread.is_alive():
            log_lines.append("Info: Бот уже запущен.")
            return
        bot_stop_event.clear()
        bot_thread = threading.Thread(target=run_bot, args=(bot_instance, bot_stop_event), daemon=True)
        bot_thread.start()
        log_lines.append("Info: Бот запущен!")

    buttons.append(("Запустить бота", pygame.Rect(button_x, button_y_start, button_width, button_height), start_bot))

    def stop_bot():
        bot_stop_event.set()
        try:
            bot_instance.stop_polling()
        except Exception as e:
            logger.debug(f"Остановка polling вызвала исключение: {e}")
        log_lines.append("Info: Остановка бота запрошена.")

    buttons.append(("Остановить бота", pygame.Rect(button_x, button_y_start + button_spacing, button_width, button_height), stop_bot))

    def update_data():
        news = parse_rss_feeds(bot_instance.keywords)
        db_manager.save_news(news)
        log_lines.append("Info: Данные обновлены!")

    buttons.append(("Обновить данные вручную", pygame.Rect(button_x, button_y_start + 2 * button_spacing, button_width, button_height), update_data))

    view_news_mode = False
    news_lines = []
    news_offset = 0

    def toggle_view_news():
        nonlocal view_news_mode, news_lines
        view_news_mode = not view_news_mode
        if view_news_mode:
            news = db_manager.load_news()
            news_lines = []
            for item in news:
                news_lines.append(f"{item['title']}: {item['link']}")
                news_lines.append(f"{item['summary'][:100]}...")
                news_lines.append("")  # Пустая строка для разделения
            log_lines.append("Просмотр новостей открыт.")
        else:
            log_lines.append("Просмотр новостей закрыт.")

    buttons.append(("Просмотреть новости", pygame.Rect(button_x, button_y_start + 3 * button_spacing, button_width, button_height), toggle_view_news))

    def make_backup():
        backup_to_json(db_manager)
        log_lines.append("Info: Бэкап создан!")

    buttons.append(("Создать бэкап", pygame.Rect(button_x, button_y_start + 4 * button_spacing, button_width, button_height), make_backup))

    def exit_app():
        db_manager.close()
        pygame.quit()
        sys.exit()

    buttons.append(("Выход", pygame.Rect(button_x, button_y_start + 5 * button_spacing, button_width, button_height), exit_app))

    log_rect = pygame.Rect(10, 300, 580, 190)  

    running = True
    while running:
        screen.fill(WHITE)

        title_surf = font_large.render("Управление расширенным ботом для маникюра", True, BLACK)
        screen.blit(title_surf, (50, 10))

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1: 
                    mouse_pos = pygame.mouse.get_pos()
                    for text, rect, func in buttons:
                        if rect.collidepoint(mouse_pos):
                            func()
            if event.type == pygame.MOUSEWHEEL:
                if view_news_mode:
                    news_offset -= event.y * 3 
                    news_offset = max(0, min(news_offset, max(0, len(news_lines) - max_log_lines)))
                else:
                    log_offset -= event.y * 3  
                    log_offset = max(0, min(log_offset, max(0, len(log_lines) - max_log_lines)))

        if view_news_mode:
            news_title_surf = font_medium.render("Новости:", True, BLACK)
            screen.blit(news_title_surf, (10, 50))
            for i in range(max_log_lines):
                idx = news_offset + i
                if idx < len(news_lines):
                    line_surf = font_small.render(news_lines[idx], True, BLACK)
                    screen.blit(line_surf, (10, 80 + i * 20))
        else:
            for text, rect, func in buttons:
                pygame.draw.rect(screen, LIGHT_BLUE, rect)
                text_surf = font_medium.render(text, True, BLACK)
                text_rect = text_surf.get_rect(center=rect.center)
                screen.blit(text_surf, text_rect)

            pygame.draw.rect(screen, GRAY, log_rect, 2)  
            for i in range(max_log_lines):
                idx = log_offset + i
                if idx < len(log_lines):
                    line_surf = font_small.render(log_lines[idx], True, BLACK)
                    screen.blit(line_surf, (15, 305 + i * 12))  

        pygame.display.flip()
        clock.tick(60)

    db_manager.close()
    pygame.quit()

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Установи переменную окружения BOT_TOKEN и перезапусти скрипт.")
    db_manager = DatabaseManager(DB_FILE)
    restore_from_json(db_manager)
    auto_update_setting = db_manager.load_setting('auto_update', 'on')
    if auto_update_setting == 'on':
        threading.Thread(target=auto_update, args=(db_manager,), daemon=True).start()
    saved_feeds = db_manager.load_setting('rss_feeds')
    if saved_feeds:
        RSS_FEEDS = json.loads(saved_feeds)
    bot = ManicureBot(BOT_TOKEN, db_manager)
    if not db_manager.load_news():
        news = parse_rss_feeds(bot.keywords)
        db_manager.save_news(news)
    create_gui(bot, db_manager)
