# Гарик — Отчёты (Telegram-бот)

Показывает владельцу короткую сводку по филиалам (Мурино / Бугры) прямо из рабочих Google-таблиц: клиенты, выручка, товары, отзывы, касса — за сегодня / вчера / текущую неделю.

## 1. Локальный тест (без хостинга, до деплоя)

```bash
cd ~/mens-cave-report-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="ваш_токен_от_BotFather"
export ALLOWED_USER_IDS="ваш_telegram_id"
export GOOGLE_CREDENTIALS_FILE="/путь/к/service-account.json"
export SHEET_ID_MURINO="1ZB4ixsT84iKHbwtL82iVm5Ev0q-BQ_3XMmYP32ULkLI"
export SHEET_ID_BUGRY="ID_таблицы_Бугры"
export USE_POLLING=1

python3 bot.py
```

Дальше открывайте бота в Telegram и нажимайте `/start`.

## 2. Деплой на Render.com (бесплатно, работает круглосуточно)

1. Залейте эту папку в приватный репозиторий на GitHub.
2. На [render.com](https://render.com) → **New → Web Service** → подключите репозиторий.
3. Настройки:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Plan**: Free
4. В разделе **Environment** добавьте переменные:
   - `BOT_TOKEN` — токен от BotFather
   - `ALLOWED_USER_IDS` — ваш Telegram ID (через запятую, если несколько)
   - `GOOGLE_CREDENTIALS_JSON` — **всё содержимое** JSON-файла сервисного аккаунта одной строкой (не путь к файлу — на Render файла нет, только переменные)
   - `SHEET_ID_MURINO` — ID таблицы Мурино
   - `SHEET_ID_BUGRY` — ID таблицы Бугры
5. Задеплойте. Render сам даст публичный URL вида `https://garik-bot.onrender.com` и пробросит его в `RENDER_EXTERNAL_URL` — бот сам поставит вебхук при старте.

## 3. Чтобы бот не "засыпал" (бесплатный тариф Render)

Зарегистрируйтесь на [cron-job.org](https://cron-job.org) (бесплатно) и создайте задачу:
- URL: `https://ваш-адрес.onrender.com/health`
- Интервал: каждые 10 минут

Это держит бота живым — точно так же, как у вас настроен бот ДДС.

## Структура

- `bot.py` — сам бот (webhook-режим для Render, polling-режим для локального теста)
- `sheets.py` — чтение данных из Google Sheets
- `requirements.txt`, `Procfile` — для деплоя
