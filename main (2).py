
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
