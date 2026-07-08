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

## Inline Mode

Inline Mode позволяет вызвать бота прямо в любом чате Telegram:

```text
@your_bot_username https://www.instagram.com/reel/XXXXXXXXXXX/
```

Чтобы включить:

1. Откройте [@BotFather](https://t.me/BotFather).
2. Выполните `/setinline`.
3. Выберите своего бота.
4. Укажите placeholder, например `Instagram Reel link`.

Для inline-режима боту нужен storage-чат. Telegram inline-результаты не принимают локальный файл напрямую, поэтому бот один раз загружает видео в storage-чат, получает `file_id`, сохраняет его в кэше и затем отдает inline-результат.

Настройка storage-чата:

1. Создайте приватную группу.
2. Добавьте туда бота.
3. Отправьте в этом чате команду:

```text
/chatid
```

4. Скопируйте полученный ID в `.env`:

```env
STORAGE_CHAT_ID=-1001234567890
```

Первый inline-запрос для нового Reel может занять больше времени: бот скачивает видео и загружает его в storage-чат. Если Telegram покажет результат `Готовлю видео...`, повторите inline-запрос через несколько секунд. Повторные запросы по тому же Reel будут работать из кэша.

## Docker

Локально можно собрать и запустить контейнер через Docker Compose:

```powershell
docker compose up -d --build
```

Перед запуском рядом должен быть `.env` с `BOT_TOKEN` и остальными настройками.

## Video Compression

Если скачанный Reel больше `MAX_FILE_SIZE_MB`, бот может автоматически сжать его через `ffmpeg`, чтобы Telegram принял файл.

Включить или отключить:

```env
ENABLE_VIDEO_COMPRESSION=true
```

Полностью отключить сжатие:

```env
ENABLE_VIDEO_COMPRESSION=false
```

Основные настройки:

```env
MAX_FILE_SIZE_MB=50
VIDEO_COMPRESSION_TARGET_MB=49
VIDEO_COMPRESSION_HEIGHTS=1280,854,640
VIDEO_COMPRESSION_AUDIO_KBPS=96
VIDEO_COMPRESSION_PRESET=veryfast
VIDEO_COMPRESSION_MIN_VIDEO_KBPS=250
```

`VIDEO_COMPRESSION_HEIGHTS` задает последовательность попыток. Для вертикальных Reels `1280` обычно означает итог около `720x1280`, `854` - около `480x854`.

## GitHub Actions Deploy

В репозитории есть workflow `.github/workflows/deploy.yml`. Он запускается при push в `main` или вручную через `workflow_dispatch`.

Что делает pipeline:

1. Собирает Docker-образ.
2. Публикует образ в GitHub Container Registry: `ghcr.io`.
3. Подключается к серверу по SSH.
4. Создает/обновляет `.env` и `docker-compose.yml` на сервере.
5. Выполняет `docker compose pull` и `docker compose up -d`.

Добавьте в GitHub repository secrets:

```text
BOT_TOKEN
STORAGE_CHAT_ID
SSH_HOST
SSH_USER
SSH_PRIVATE_KEY
```

Опциональные secrets:

```text
SSH_PORT
GHCR_USERNAME
GHCR_TOKEN
INSTAGRAM_COOKIES_B64
```

`GHCR_USERNAME` и `GHCR_TOKEN` нужны, если GHCR package приватный. Для `GHCR_TOKEN` используйте GitHub Personal Access Token с правом `read:packages`.

Если нужны Instagram cookies на сервере, закодируйте cookies-файл в base64 и сохраните результат в `INSTAGRAM_COOKIES_B64`:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\instagram_cookies.txt"))
```

Pipeline создаст `cookies/instagram_cookies.txt` на сервере с правами на чтение для контейнера. Сам файл не хранится в Git и не попадает в Docker-образ.

Опциональные repository variables:

```text
DEPLOY_PATH
MAX_FILE_SIZE_MB
UPLOAD_TIMEOUT_SECONDS
ENABLE_VIDEO_COMPRESSION
VIDEO_COMPRESSION_TARGET_MB
VIDEO_COMPRESSION_HEIGHTS
VIDEO_COMPRESSION_AUDIO_KBPS
VIDEO_COMPRESSION_PRESET
VIDEO_COMPRESSION_MIN_VIDEO_KBPS
INLINE_PREPARE_WAIT_SECONDS
```

Если `DEPLOY_PATH` не задан, деплой идет в:

```text
$HOME/instagram-reels-bot
```

На сервере должны быть установлены Docker и Docker Compose plugin.

Если контейнер не может записать inline-кэш в `/app/data/inline_cache.json`, проверьте владельца data-директории. При deploy через GitHub Actions контейнер запускается с UID/GID SSH-пользователя. Для `DEPLOY_PATH=/opt/nonnus_bot` можно исправить так:

```bash
sudo chown -R YOUR_SSH_USER:YOUR_SSH_USER /opt/nonnus_bot/data
```

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
