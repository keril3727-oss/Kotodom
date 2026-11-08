# КОТОДОМ 🐾

Платформа помощи бездомным котятам и животным: сайт объявлений и Telegram-бот на общей базе данных.

## Первый MVP

- Просмотр объявлений животных.
- Фильтр объявлений по городу.
- Форма добавления животного.
- Базовый Telegram-бот с командами /start и /help.
- API и проверка состояния сервиса: `/health`.

## Запуск

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Откройте `http://localhost:8000`.

## Переменные окружения

- `BOT_TOKEN` — токен Telegram-бота от BotFather.
- `WEBHOOK_SECRET` — необязательный секрет для проверки Telegram webhook.
- `DATABASE_PATH` — необязательный путь к SQLite-файлу.

## Деплой на Render

Создайте Web Service из этого GitHub-репозитория.

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Добавьте `BOT_TOKEN` в Environment после создания бота. Для постоянного хранения базы данных на Render потребуется подключить persistent disk и задать `DATABASE_PATH` внутри него.

## Перед публичным запуском

Нужно добавить модерацию объявлений, защиту от спама, безопасную обработку контактов и фотографий, а также понятные правила передачи животных новым владельцам.
