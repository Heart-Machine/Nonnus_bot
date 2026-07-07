# Instagram Reels Telegram Bot

Telegram-бот на Python: получает ссылку на Instagram Reel и отправляет видео обратно файлом.

Используйте только для видео, которые у вас есть право скачивать и пересылать. Бот не обходит приватность, платный доступ или DRM.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Настройка

1. Создайте бота через [@BotFather](https://t.me/BotFather).
2. Скопируйте `.env.example` в `.env`.
3. Вставьте токен:

```env
BOT_TOKEN=123456789:your_real_token
```

Если Instagram не отдает некоторые ролики без авторизации, можно экспортировать cookies из своего браузера в формате Netscape и указать путь:

```env
COOKIES_FILE=C:\path\to\instagram_cookies.txt
```

## Запуск

```powershell
python bot.py
```

После запуска отправьте боту ссылку вида:

```text
https://www.instagram.com/reel/XXXXXXXXXXX/
```

В группе или общем чате добавьте бота и тегните его вместе со ссылкой:

```text
@your_bot_username https://www.instagram.com/reel/XXXXXXXXXXX/
```

В группах бот отвечает только на сообщения, где есть его `@username`. Если у бота включен BotFather Privacy Mode, это нормальный режим: Telegram все равно доставляет боту сообщения с упоминанием.

## Как это работает

- `python-telegram-bot` принимает сообщения и отправляет файл в Telegram.
- `yt-dlp` скачивает видео по ссылке.
- Для каждой загрузки создается временная папка, которая удаляется после отправки.
- `MAX_FILE_SIZE_MB` ограничивает размер файла перед отправкой.

## Частые проблемы

**Не скачивается публичный Reel**

Обновите `yt-dlp`:

```powershell
pip install -U yt-dlp
```

**Instagram просит вход**

Укажите `COOKIES_FILE` с cookies вашей Instagram-сессии.

**Файл слишком большой**

Увеличьте `MAX_FILE_SIZE_MB`, если ваш Telegram Bot API принимает файлы такого размера, или скачивайте более низкое качество, изменив параметр `format` в `bot.py`.
