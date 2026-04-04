
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
import schedule
import urllib.parse
import re
import datetime
import sys  

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_log.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)



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

    def __init__(self, db_file):
        self.db_file = db_file
        self.conn = None
        self.cursor = None
        self.connect()
        self.create_tables()

    def connect(self):

        try:
            self.conn = sqlite3.connect(self.db_file)
            self.cursor = self.conn.cursor()
            logger.info("Подключение к БД успешно.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    def create_tables(self):
        """Создание таблиц, если они не существуют."""
        # Таблица для новостей
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица для поисков
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                site TEXT NOT NULL,
                result TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица для пользовательских ключевых слов
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL
            )
        ''')
       # Таблица для настроек
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        self.conn.commit()
        logger.info("Таблицы БД созданы или уже существуют.")

    def load_news(self):
        self.cursor.execute("SELECT title, link, summary FROM news ORDER BY timestamp DESC")
        return [{'title': row[0], 'link': row[1], 'summary': row[2]} for row in self.cursor.fetchall()]

    def save_news(self, news_list):
        self.cursor.execute("DELETE FROM news")  
        for news in news_list:
            self.cursor.execute("INSERT INTO news (title, link, summary) VALUES (?, ?, ?)",
                                (news['title'], news['link'], news['summary']))
        self.conn.commit()
        logger.info(f"Сохранено {len(news_list)} новостей.")

    def load_searches(self):
        self.cursor.execute("SELECT query, site, result FROM searches ORDER BY timestamp DESC")
        searches = {}
        for row in self.cursor.fetchall():
            query, site, result = row
            if query not in searches:
                searches[query] = {}
            searches[query][site] = result
        return searches

    def save_search(self, query, results):
        for site, result in results.items():
            self.cursor.execute("INSERT INTO searches (query, site, result) VALUES (?, ?, ?)",
                                (query, site, result))
        self.conn.commit()
        logger.info(f"Сохранен поиск для '{query}'.")

    def load_keywords(self):
        self.cursor.execute("SELECT keyword FROM keywords")
        return [row[0] for row in self.cursor.fetchall()]

    def add_keyword(self, keyword):
        try:
            self.cursor.execute("INSERT INTO keywords (keyword) VALUES (?)", (keyword,))
            self.conn.commit()
            logger.info(f"Добавлено ключевое слово: {keyword}")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Ключевое слово '{keyword}' уже существует.")
            return False

    def load_setting(self, key, default=None):
        self.cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = self.cursor.fetchone()
        return row[0] if row else default

    def save_setting(self, key, value):
        self.cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()
        logger.info(f"Сохранена настройка: {key} = {value}")

    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("Соединение с БД закрыто.")


def backup_to_json(db_manager):
    data = {
        'news': db_manager.load_news(),
        'searches': db_manager.load_searches(),
        'keywords': db_manager.load_keywords(),
        'settings': {key: db_manager.load_setting(key) for key in ['auto_update', 'log_level']}
    }
    with open(JSON_BACKUP, 'w') as file:
        json.dump(data, file, indent=4)
    logger.info("Бэкап в JSON создан.")

def restore_from_json(db_manager):

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
    news_list = []  
    for feed_url in RSS_FEEDS:  
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                pub_date = entry.get('published_parsed', None)
                if pub_date:
                    pub_datetime = datetime.datetime(*pub_date[:6])
                    if (datetime.datetime.now() - pub_datetime).days > 7:
                        continue  
                if any(keyword.lower() in entry.title.lower() or keyword.lower() in entry.summary.lower() for keyword in keywords):
                    news_item = {
                        'title': entry.title,
                        'link': entry.link,
                        'summary': entry.summary
                    }
                    if 'enclosures' in entry and entry.enclosures:
                        news_item['image'] = entry.enclosures[0].get('href', '')
                    elif 'content' in entry:
                        soup = BeautifulSoup(entry.content[0].value, 'html.parser')
                        img = soup.find('img')
                        if img:
                            news_item['image'] = img['src']
                    news_list.append(news_item)
            logger.info(f"Обработана RSS: {feed_url}")
        except Exception as e:
            logger.error(f"Ошибка парсинга RSS {feed_url}: {e}")
    return news_list 
def scrape_website(url):
   
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
            return {'text': text, 'images': images[:3]}  # Возвращаем текст и до 3 изображений.
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
    encoded_query = urllib.parse.quote(query + ' manicure')
    sites = [
        f"https://www.google.com/search?q={encoded_query}",  # Google поиск
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
            if re.search('|'.join(keywords), line.lower(), re.IGNORECASE):
                filtered_text += line + '\n'
        results[site] = {
            'text': filtered_text[:1000] + '...' if len(filtered_text) > 1000 else filtered_text,
            'images': scraped['images']
        }
        time.sleep(1)  
        logger.info(f"Обработан сайт: {site}")
    return results  
class ManicureBot(telebot.TeleBot):
    def __init__(self, token, db_manager):
        super().__init__(token)
        self.db_manager = db_manager
        self.keywords = self.db_manager.load_keywords() or MANICURE_KEYWORDS
        self.setup_handlers()

    def setup_handlers(self):
        self.message_handler(commands=['start'])(self.send_welcome)
        self.message_handler(commands=['help'])(self.send_help)
        self.message_handler(commands=['news'])(self.send_news)
        self.message_handler(commands=['search'])(self.search_handler)
        self.message_handler(commands=['add_keyword'])(self.add_keyword_handler)
        self.message_handler(commands=['list_keywords'])(self.list_keywords_handler)
        self.message_handler(commands=['add_feed'])(self.add_feed_handler)
        self.message_handler(commands=['list_searches'])(self.list_searches_handler)
        self.message_handler(func=lambda message: True)(self.echo_all)

    def send_welcome(self, message):
        bot.reply_to(message, "Привет! Я расширенный бот для маникюра. Используй /help для списка команд.")

    def send_help(self, message):
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
        bot.reply_to(message, help_text)

    def send_news(self, message):
        news = parse_rss_feeds(self.keywords)
        self.db_manager.save_news(news)
        response = "Последние новости о маникюре:\n"
        for item in news[:10]:  # Показываем до 10
            response += f"- {item['title']}: {item['link']}\n{item['summary'][:150]}...\n"
            if 'image' in item:
                response += f"Изображение: {item['image']}\n"
            response += "\n"
        bot.reply_to(message, response)

    def search_handler(self, message):
        query = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not query:
            bot.reply_to(message, "Укажи запрос, например: /search gel nails")
            return
        results = search_manicure(query, self.keywords)
        self.db_manager.save_search(query, results)
        response = f"Результаты поиска '{query}':\n"
        for site, res in results.items():
            response += f"Из {site}:\n{res['text']}\n"
            if res['images']:
                response += "Изображения: " + ', '.join(res['images']) + "\n"
            response += "\n"
        bot.reply_to(message, response[:2000])  

    def add_keyword_handler(self, message):
        keyword = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not keyword:
            bot.reply_to(message, "Укажи слово, например: /add_keyword glitter nails")
            return
        if self.db_manager.add_keyword(keyword):
            self.keywords.append(keyword)
            bot.reply_to(message, f"Добавлено: {keyword}")
        else:
            bot.reply_to(message, f"{keyword} уже существует.")

    def list_keywords_handler(self, message):
        keywords = self.db_manager.load_keywords()
        response = "Ключевые слова:\n" + "\n".join(keywords)
        bot.reply_to(message, response)

    def add_feed_handler(self, message):
        feed_url = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ''
        if not feed_url or not feed_url.startswith('http'):
            bot.reply_to(message, "Укажи валидный URL, например: /add_feed https://example.com/rss")
            return
        RSS_FEEDS.append(feed_url)
        bot.reply_to(message, f"Добавлена лента: {feed_url}")
        self.db_manager.save_setting('rss_feeds', json.dumps(RSS_FEEDS))

    def list_searches_handler(self, message):
        searches = self.db_manager.load_searches()
        response = "Предыдущие поиски:\n"
        for query in searches:
            response += f"- {query}\n"
        bot.reply_to(message, response)

    def echo_all(self, message):
        bot.reply_to(message, f"Ты сказал: {message.text}. Попробуй /search {message.text}!")

def run_bot(bot_instance):
  
    while True:
        try:
            bot_instance.polling(none_stop=True, timeout=60)
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"Telegram API ошибка: {e}")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Общая ошибка бота: {e}")
            time.sleep(5)
def auto_update(db_manager, keywords):
    while True:
        news = parse_rss_feeds(keywords)
        db_manager.save_news(news)
        backup_to_json(db_manager)
        logger.info("Автообновление завершено.")
        time.sleep(AUTO_UPDATE_INTERVAL)

def create_gui(bot_instance, db_manager):
    pygame.init()
    screen = pygame.display.set_mode((600, 500))
    pygame.display.set_caption("Manicure Bot Manager - Расширенная версия")
    clock = pygame.time.Clock()

    # Цвета
    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)
    GRAY = (200, 200, 200)
    BLUE = (0, 0, 255)
    LIGHT_BLUE = (173, 216, 230)

    # Шрифты
    font_large = pygame.font.Font(None, 24)
    font_medium = pygame.font.Font(None, 18)
    font_small = pygame.font.Font(None, 14)

    log_lines = []
    log_offset = 0
    max_log_lines = 15 
    
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
        threading.Thread(target=run_bot, args=(bot_instance,), daemon=True).start()
        log_lines.append("Info: Бот запущен!")

    buttons.append(("Запустить бота", pygame.Rect(button_x, button_y_start, button_width, button_height), start_bot))

    def stop_bot():
        log_lines.append("Info: Бот остановлен (симуляция).")

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


