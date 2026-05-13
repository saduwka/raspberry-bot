# 🤖 GameBot v2 (Multi-Assistant)

A multifunctional Telegram bot combining a news aggregator, trading engine, and job search system. Optimized for **Raspberry Pi (DietPi/Debian)**.

---

## 🇺🇸 English Version

### 🚀 Key Modules

#### 1. 📰 News Aggregator (Gemini AI)
*   **News Collection**: Automatic monitoring of gaming and IT resources via RSS.
*   **AI Summarization**: Uses **Google Gemini** to summarize news into Russian.
*   **Filtering**: Manage keywords (`/addkw`) and tag blacklists (`/blocktag`).
*   **Moderation**: Approve/Reject system for channel posting.

#### 2. 📈 Trading Engine (Binance)
*   **Automation**: Trading on Binance via the `ccxt` library.
*   **Strategies**: Market analysis using technical indicators (`ta`, `pandas`).
*   **AI Analysis**: Additional signal verification via Gemini for decision making.
*   **Paper Mode**: Support for Paper Trading for safe testing.

#### 3. 💼 Job Hunter (AI Scoring)
*   **Job Search**: Automatic collection of vacancies from specialized resources.
*   **AI Scoring**: Relevancy assessment of vacancies against your CV on a 10-point scale.
*   **Filters**: Configure minimum scores and requirements (e.g., Worldwide/Remote).

#### 4. 🛠 System Health
*   **Raspberry Pi Monitoring**: CPU temperature (alert at 65°C+), load, RAM, and power status (Undervoltage).

### 📋 Main Commands (Admin)

| Command | Description |
| :--- | :--- |
| `/status` | System, DB, and Raspberry Pi status |
| `/listrss` | Manage news sources |
| `/jobs_stats` | Statistics on found vacancies |
| `/trades` | Status of trading positions |

### ⚙️ Installation and Setup

1. **Configure Environment**:
   Create a `.env` file based on the example:
   ```env
   BOT_TOKEN=your_token
   ADMIN_ID=your_id
   GEMINI_API_KEY=google_ai_key
   BINANCE_API_KEY=binance_key
   BINANCE_SECRET=binance_secret
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the Bot**:
   ```bash
   python3 bot.py
   ```

---

## 🇷🇺 Русская версия

### 🚀 Основные модули

#### 1. 📰 Агрегатор новостей (Gemini AI)
*   **Сбор новостей**: Автоматический мониторинг игровых и IT ресурсов через RSS.
*   **ИИ-Пересказ**: Использование **Google Gemini** для суммаризации новостей на русский язык.
*   **Фильтрация**: Управление ключевыми словами (`/addkw`) и черным списком тегов (`/blocktag`).
*   **Модерация**: Система Approve/Reject для постов в канал.

#### 2. 📈 Торговый движок (Binance)
*   **Автоматизация**: Торговля на Binance через библиотеку `ccxt`.
*   **Стратегии**: Анализ рынка с помощью технических индикаторов (`ta`, `pandas`).
*   **ИИ-Анализ**: Дополнительная проверка сигналов через Gemini для принятия решений.
*   **Paper Mode**: Поддержка режима симуляции (Paper Trading) для безопасного тестирования.

#### 3. 💼 Поиск работы (AI Scoring)
*   **Поиск вакансий**: Автоматический сбор вакансий с профильных ресурсов.
*   **AI Scoring**: Оценка релевантности вакансий вашему резюме по 10-балльной шкале.
*   **Фильтры**: Настройка минимального балла и требований (например, Worldwide/Remote).

#### 4. 🛠 Состояние системы
*   **Мониторинг Raspberry Pi**: температура CPU (алёрт при 65°C+), нагрузка, RAM и статус питания (Undervoltage).

### 📋 Основные команды (Админ)

| Команда | Описание |
| :--- | :--- |
| `/status` | Статус системы, БД и Raspberry Pi |
| `/listrss` | Управление источниками новостей |
| `/jobs_stats` | Статистика по найденным вакансиям |
| `/trades` | Состояние торговых позиций |

### ⚙️ Установка и запуск

1. **Настройте окружение**:
   Создайте файл `.env` на основе примера:
   ```env
   BOT_TOKEN=ваш_токен
   ADMIN_ID=ваш_id
   GEMINI_API_KEY=ключ_google_ai
   BINANCE_API_KEY=ключ_binance
   BINANCE_SECRET=секрет_binance
   ```

2. **Установите зависимости**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Запустите бота**:
   ```bash
   python3 bot.py
   ```

---
*Developed for personal efficiency and automation.*
