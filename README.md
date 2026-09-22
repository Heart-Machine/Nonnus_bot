# Instagram Telegram Bot

Telegram-бот на Python: получает ссылку на публикацию Instagram и отправляет ее содержимое обратно файлами.

Поддерживаются:

- Reels и обычные видео - отправляются видеофайлом;
- посты с одним фото - отправляются фотографией;
- карусели из фото и видео - отправляются альбомом в том же порядке, что и в публикации.

Карусель длиннее 10 файлов разбивается на несколько альбомов: это предел Telegram на один альбом.

Используйте только для материалов, которые у вас есть право скачивать и пересылать. Бот не обходит приватность, платный доступ или DRM.

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

## Разработка

Инструменты для разработки ставятся отдельно от рантайма:

```powershell
pip install -r requirements-dev.txt
```

Тесты и линтер:

```powershell
pytest
ruff check .
```

Тесты не ходят в сеть и не обращаются к Telegram: то, что туда ходит, подменяется заглушками, а проверяется логика вокруг - какие элементы публикации содержат видео, в каком порядке получаются файлы, что уходит в Telegram и что попадает в кэш `file_id`.

Настройки линтера лежат в `ruff.toml`. Включены правила, ловящие ошибки, а не оформление: `pycodestyle`, `pyflakes` и `bugbear`. Правила сортировки импортов и модернизации аннотаций выключены намеренно - в `bot.py` своя последовательная конвенция, и включение этих правил означало бы переоформление всего файла без пользы для поведения.

### Зависимости и локфайл

Рантайм-зависимости описаны в двух файлах:

- `requirements.in` - намерение: что боту нужно и в каких границах. Правите вы его.
- `requirements.txt` - сгенерированный локфайл: точные версии, включая транзитивные. Правится только через `pip-compile`.

Образ ставит зависимости из локфайла, поэтому сборка одного и того же коммита всегда даёт один и тот же набор версий. Без этого каждая пересборка тянула бы то, что оказалось свежим на PyPI в тот момент, и бот мог сломаться от правки README.

Добавить или изменить зависимость:

```powershell
# отредактируйте requirements.in, затем
pip-compile requirements.in
```

Поднять версии осознанно:

```powershell
pip-compile --upgrade requirements.in
```

Без `--upgrade` команда сохраняет уже зафиксированные версии, поэтому регенерация ничего не двигает сама по себе. CI проверяет, что локфайл не разошёлся с `requirements.in`: иначе файл тихо протухает и перестаёт что-либо гарантировать.

Коммитить нужно оба файла.

`requirements-dev.txt` намеренно не заблокирован: он никуда не едет, а `pip-compile` резолвит под ту платформу, на которой запущен, - локфайл, собранный на Windows, притащил бы `colorama` (его тянет `pytest` только там) и расходился бы с Linux при каждой проверке. Рантайм-версии приходят в него через `-r requirements.txt`, так что тесты идут против тех же версий, с которыми собирается образ.

Базовый образ в `Dockerfile` прибит по digest, а не по тегу: `3.13-slim` подвижен. Как обновить - написано комментарием в самом `Dockerfile`.

## Inline Mode

Inline Mode позволяет вызвать бота прямо в любом чате Telegram:

```text
@your_bot_username https://www.instagram.com/reel/XXXXXXXXXXX/
```

Чтобы включить:

1. Откройте [@BotFather](https://t.me/BotFather).
2. Выполните `/setinline`.
3. Выберите своего бота.
4. Укажите placeholder, например `Instagram link`.

Для inline-режима боту нужен storage-чат. Telegram inline-результаты не принимают локальный файл напрямую, поэтому бот один раз загружает файлы в storage-чат, получает их `file_id`, сохраняет их в кэше и затем отдает inline-результаты.

У inline-сообщения нет аналога альбома: оно несет ровно один файл. Поэтому карусель бот отдает списком - по одному inline-результату на каждый файл публикации, с подписью вида `Файл 2 из 7`. Чтобы отправить следующий файл, повторите inline-запрос и выберите его в списке.

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

Первый inline-запрос для новой публикации может занять больше времени: бот скачивает файлы и загружает их в storage-чат. Если Telegram покажет результат `Готовлю видео...`, выберите его - сообщение обновится само, как только публикация будет готова. Повторные запросы по той же ссылке работают из кэша.

## Docker

Локально можно собрать и запустить контейнер через Docker Compose:

```powershell
docker compose up -d --build
```

Перед запуском рядом должен быть `.env` с `BOT_TOKEN` и остальными настройками.

## Video Compression

Если скачанное видео больше `MAX_FILE_SIZE_MB`, бот может автоматически сжать его через `ffmpeg`, чтобы Telegram принял файл. В карусели каждое видео обрабатывается отдельно.

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

## Photos

У Telegram отдельный, гораздо более низкий лимит на фотографии, а Instagram отдает оригиналы, поэтому фото настраиваются своими параметрами:

```env
PHOTO_MAX_FILE_SIZE_MB=10
PHOTO_MAX_DIMENSION=2560
PHOTO_DOWNLOAD_TIMEOUT_SECONDS=60
```

Фото больше `PHOTO_MAX_FILE_SIZE_MB`, а также форматы, которые Telegram не принимает как фотографию (например WebP), пережимаются в JPEG со стороной не больше `PHOTO_MAX_DIMENSION`. Для этого нужен `ffmpeg` - тот же, что и для сжатия видео.

## CI

Workflow `.github/workflows/ci.yml` запускается на каждый pull request:

1. Линтер и тесты на Python 3.13 - той же версии, что в `Dockerfile`.
2. Сборка Docker-образа без публикации, чтобы поломка `Dockerfile` находилась до мержа, а не при деплое.

Отдельным шагом `tools/check_action_inputs.py` сверяет каждый `with:` в обоих workflow с `action.yml` того действия, которому параметр передаётся, на его прибитом SHA. Это нужно потому, что GitHub неизвестный параметр не отвергает, а лишь предупреждает в Annotations и идёт дальше: так `script_stop: true` оставался в деплое и ничего не делал после того, как `appleboy/ssh-action` его убрал. Действия из `deploy.yml` на pull request не выполняются, и для них это единственная проверка до мержа. Секретов она не требует, поэтому безопасна и для PR из форка.

Тот же workflow вызывается из деплоя через `workflow_call`, поэтому push напрямую в `main` не доедет до сервера, не пройдя те же проверки: job сборки образа ждет `CI`. Сборка образа на самом деплое не дублируется - этот job идет только на pull request.

## GitHub Actions Deploy

В репозитории есть workflow `.github/workflows/deploy.yml`. Он запускается при push в `main` или вручную через `workflow_dispatch`.

Что делает pipeline:

1. Прогоняет CI: линтер, тесты.
2. Собирает Docker-образ.
3. Публикует образ в GitHub Container Registry: `ghcr.io`.
4. Подключается к серверу по SSH.
5. Создает/обновляет `.env` и `docker-compose.yml` на сервере.
6. Выполняет `docker compose pull` и `docker compose up -d`.

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
PHOTO_MAX_FILE_SIZE_MB
PHOTO_MAX_DIMENSION
PHOTO_DOWNLOAD_TIMEOUT_SECONDS
UPLOAD_TIMEOUT_SECONDS
ENABLE_VIDEO_COMPRESSION
VIDEO_COMPRESSION_TARGET_MB
VIDEO_COMPRESSION_HEIGHTS
VIDEO_COMPRESSION_AUDIO_KBPS
VIDEO_COMPRESSION_PRESET
VIDEO_COMPRESSION_MIN_VIDEO_KBPS
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

## Безопасность

**Actions прибиты по SHA коммита, а не по тегу.** Тег - подвижная ссылка: если аккаунт мейнтейнера действия угонят, в деплой-job приедет чужой код, а там в окружении `SSH_PRIVATE_KEY`, `BOT_TOKEN` и cookies Instagram. Рядом с каждым SHA стоит комментарий с версией - его поддерживает Dependabot, он же двигает сами пины.

**Cookies Instagram на сервере лежат с правами `600`.** Это живая сессия аккаунта, фактически вход без пароля, поэтому к ним то же отношение, что и к `.env`. Контейнер запускается от того же пользователя, который их создаёт, так что доступ на чтение сохраняется.

**Dependabot следит за тремя экосистемами** - `github-actions`, `pip` и `docker` - плюс включены alerts и автоматические security-обновления. Настройки в `.github/dependabot.yml`. Обновления сгруппированы: иначе каждая зависимость приезжает отдельным pull request.

**Secret scanning и push protection включены** на уровне репозитория: попытка запушить токен будет заблокирована.

**`main` защищён** - прямые коммиты запрещены, force-push и удаление ветки тоже, изменения приходят только через pull request.

Чего в репозитории нет и быть не должно: `.env`, файла cookies, ключей. Всё это в `.gitignore` и в `.dockerignore`, в образ не попадает, на сервер приезжает из секретов GitHub Actions.

## Как это работает

- `python-telegram-bot` принимает сообщения и отправляет файлы в Telegram.
- `yt-dlp` разбирает публикацию: сначала без скачивания, чтобы увидеть ее состав, затем скачивает те элементы карусели, в которых действительно есть видео.
- Фотографии бот забирает сам, по прямой ссылке с CDN: для фото Instagram `yt-dlp` не формирует форматы, их URL встречается только среди превью, из-за чего любая публикация без видео раньше падала с ошибкой `No video formats found`.
- Для каждой загрузки создается временная папка, которая удаляется после отправки.
- `MAX_FILE_SIZE_MB` и `PHOTO_MAX_FILE_SIZE_MB` ограничивают размер файлов перед отправкой.

## Частые проблемы

**Не скачивается публичная публикация**

Обновите `yt-dlp`:

```powershell
pip install -U yt-dlp
```

**Instagram просит вход**

Укажите `COOKIES_FILE` с cookies вашей Instagram-сессии.

**Файл слишком большой**

Увеличьте `MAX_FILE_SIZE_MB`, если ваш Telegram Bot API принимает файлы такого размера, или скачивайте более низкое качество, изменив параметр `format` в `bot.py`.
