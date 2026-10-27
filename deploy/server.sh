# The deploy's half that runs on the server. .github/workflows/deploy.yml
# sends it over ssh to `bash -s` on standard input, after the settings as
# `export NAME=value` lines - so none of them is on a command line there,
# where everyone on the server could read them in the process list while the
# deploy runs.

# Without this bash goes on past a failed command and the deploy reports the
# exit code of the last one, docker image prune: a failed pull would leave a
# green tick on a deploy that never happened.
set -e

deploy() {
  # Checked before anything on the server changes, so a switch to the
  # bot's own Bot API server with a secret missing stops here and
  # leaves the running bot as it is.
  if [ "$LOCAL_BOT_API" = "true" ]; then
    if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ] || [ -z "$BOT_API_IMAGE" ]; then
      echo "LOCAL_BOT_API is on, but TELEGRAM_API_ID, TELEGRAM_API_HASH or the server image is missing" >&2
      exit 1
    fi
  fi

  APP_DIR="${DEPLOY_PATH:-$HOME/instagram-reels-bot}"
  mkdir -p "$APP_DIR/data" "$APP_DIR/cookies"
  cd "$APP_DIR"
  APP_UID="$(id -u)"
  APP_GID="$(id -g)"

  if [ -n "$GHCR_TOKEN" ]; then
    # Keep the registry credentials out of the user's permanent
    # docker config. Docker stores them base64-encoded rather than
    # encrypted and says so in a warning; here they live in a
    # directory this script creates and removes on the way out, so
    # nothing is left behind even when the deploy fails partway.
    DOCKER_CONFIG="$(mktemp -d)"
    export DOCKER_CONFIG
    trap 'rm -rf "$DOCKER_CONFIG"' EXIT

    # DOCKER_CONFIG also moves where docker looks for CLI plugins, so
    # a compose installed per-user rather than system-wide has to be
    # carried across or `docker compose` below stops resolving.
    if [ -d "$HOME/.docker/cli-plugins" ]; then
      ln -s "$HOME/.docker/cli-plugins" "$DOCKER_CONFIG/cli-plugins"
    fi

    echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
  fi

  COOKIES_FILE_VALUE=""
  if [ -n "$INSTAGRAM_COOKIES_B64" ]; then
    echo "$INSTAGRAM_COOKIES_B64" | base64 -d > cookies/instagram_cookies.txt
    # These are a live Instagram session - effectively a login
    # without a password - so they get the same treatment as .env
    # rather than being left readable by every user on the box. The
    # container runs as this same user, so it can still read them.
    chmod 600 cookies/instagram_cookies.txt
    COOKIES_FILE_VALUE="/app/cookies/instagram_cookies.txt"
  else
    # The secret was removed, so the bot runs without cookies - and
    # the session from an earlier deploy has no reason to stay on
    # disk. It is still a live login until Instagram closes it.
    rm -f cookies/instagram_cookies.txt
  fi

  TELEGRAM_API_URL_VALUE=""
  if [ "$LOCAL_BOT_API" = "true" ]; then
    # The server's working directory - the bot's session with
    # Telegram - and the temporary files of the uploads it takes.
    mkdir -p data/telegram-bot-api/temp
    # api_id and api_hash, which the server reads from these very
    # variables. A file of their own, as private as .env: the bot
    # has no use for them.
    {
      printf 'TELEGRAM_API_ID=%s\n' "$TELEGRAM_API_ID"
      printf 'TELEGRAM_API_HASH=%s\n' "$TELEGRAM_API_HASH"
    } > bot-api.env
    chmod 600 bot-api.env
    TELEGRAM_API_URL_VALUE="http://telegram-bot-api:8081"
  else
    rm -f bot-api.env
  fi

  {
    printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'COOKIES_FILE=%s\n' "$COOKIES_FILE_VALUE"
    printf 'MAX_FILE_SIZE_MB=%s\n' "$MAX_FILE_SIZE_MB"
    printf 'PHOTO_MAX_FILE_SIZE_MB=%s\n' "$PHOTO_MAX_FILE_SIZE_MB"
    printf 'PHOTO_MAX_DIMENSION=%s\n' "$PHOTO_MAX_DIMENSION"
    printf 'PHOTO_DOWNLOAD_TIMEOUT_SECONDS=%s\n' "$PHOTO_DOWNLOAD_TIMEOUT_SECONDS"
    printf 'UPLOAD_TIMEOUT_SECONDS=%s\n' "$UPLOAD_TIMEOUT_SECONDS"
    printf 'ENABLE_VIDEO_COMPRESSION=%s\n' "$ENABLE_VIDEO_COMPRESSION"
    printf 'VIDEO_COMPRESSION_TARGET_MB=%s\n' "$VIDEO_COMPRESSION_TARGET_MB"
    printf 'VIDEO_COMPRESSION_HEIGHTS=%s\n' "$VIDEO_COMPRESSION_HEIGHTS"
    printf 'VIDEO_COMPRESSION_AUDIO_KBPS=%s\n' "$VIDEO_COMPRESSION_AUDIO_KBPS"
    printf 'VIDEO_COMPRESSION_PRESET=%s\n' "$VIDEO_COMPRESSION_PRESET"
    printf 'VIDEO_COMPRESSION_MIN_VIDEO_KBPS=%s\n' "$VIDEO_COMPRESSION_MIN_VIDEO_KBPS"
    printf 'MAX_PARALLEL_DOWNLOADS=%s\n' "$MAX_PARALLEL_DOWNLOADS"
    printf 'CANARY_POST_URL=%s\n' "$CANARY_POST_URL"
    printf 'ADMIN_USER_IDS=%s\n' "$ADMIN_USER_IDS"
    printf 'DAILY_DOWNLOAD_LIMIT=%s\n' "$DAILY_DOWNLOAD_LIMIT"
    printf 'DAILY_LIMIT_TIMEZONE=%s\n' "$DAILY_LIMIT_TIMEZONE"
    printf 'STORAGE_CHAT_ID=%s\n' "$STORAGE_CHAT_ID"
    printf 'INLINE_CACHE_DB=%s\n' "/app/data/inline_cache.sqlite3"
    # Kept apart from the cache: that one can be deleted and refills
    # itself, the roles in this one cannot.
    printf 'USERS_DB=%s\n' "/app/data/users.sqlite3"
    # The JSON cache from before INLINE_CACHE_DB: its posts are
    # carried into the database once, when the database is created.
    printf 'INLINE_CACHE_FILE=%s\n' "/app/data/inline_cache.json"
    printf 'TELEGRAM_API_URL=%s\n' "$TELEGRAM_API_URL_VALUE"
  } > .env
  chmod 600 .env

  {
    printf 'services:\n'
    printf '  instagram-reels-bot:\n'
    printf '    image: %s\n' "$IMAGE"
    printf '    container_name: instagram-reels-bot\n'
    printf '    restart: unless-stopped\n'
    printf '    user: "%s:%s"\n' "$APP_UID" "$APP_GID"
    printf '    env_file:\n'
    printf '      - .env\n'
    printf '    volumes:\n'
    printf '      - ./data:/app/data\n'
    printf '      - ./cookies:/app/cookies:ro\n'
    if [ -n "$TELEGRAM_API_URL_VALUE" ]; then
      printf '    depends_on:\n'
      printf '      - telegram-bot-api\n'
      # No ports: the bot reaches the server over the network
      # compose gives the two of them, by the service's name, and
      # nothing else is to reach it - the bot's token goes over it in
      # plain HTTP, and --local lets it read files from its disk.
      printf '  telegram-bot-api:\n'
      printf '    image: %s\n' "$BOT_API_IMAGE"
      printf '    container_name: instagram-reels-bot-api\n'
      printf '    restart: unless-stopped\n'
      printf '    user: "%s:%s"\n' "$APP_UID" "$APP_GID"
      printf '    env_file:\n'
      printf '      - bot-api.env\n'
      printf '    command:\n'
      printf '      - --local\n'
      printf '      - --dir=/var/lib/telegram-bot-api\n'
      printf '      - --temp-dir=/var/lib/telegram-bot-api/temp\n'
      printf '    volumes:\n'
      printf '      - ./data/telegram-bot-api:/var/lib/telegram-bot-api\n'
    fi
  } > docker-compose.yml

  docker compose pull
  # --remove-orphans takes the Bot API server down when LOCAL_BOT_API
  # is turned off and the service leaves the file.
  docker compose up -d --remove-orphans
  docker image prune -f
}

# All of the work is in a function, so that bash has read the whole script
# before any of it runs. It reads the script from standard input as it goes,
# and a command in it that read standard input too would swallow the rest of
# the script as its own input; called this way, the commands get an empty
# one.
deploy </dev/null
