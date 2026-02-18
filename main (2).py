
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
       