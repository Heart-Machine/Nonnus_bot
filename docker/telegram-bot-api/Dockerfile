# The Telegram Bot API server (tdlib/telegram-bot-api), built from source. It
# sees the bot's token and all of its traffic, so what runs is what this file
# says, not somebody else's build.
#
# Both pins move deliberately. The base image is pinned by digest, as the
# bot's is, and Dependabot keeps it moving within Debian 13. The server is
# pinned by commit: the repository has no releases or tags, and a commit also
# fixes the TDLib submodule it builds with. To move to a newer server, take
# the latest commit of https://github.com/tdlib/telegram-bot-api - the build
# below refuses one that knows no rich messages.
FROM debian:13-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS build

ARG TELEGRAM_BOT_API_COMMIT=e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1

# The build dependencies the project's own instructions list for Debian.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cmake g++ git gperf libssl-dev make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/tdlib/telegram-bot-api.git . \
    && git checkout --detach "$TELEGRAM_BOT_API_COMMIT" \
    && git submodule update --init --recursive \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/opt/telegram-bot-api \
    && cmake --build build --target install -j "$(nproc)" \
    && strip /opt/telegram-bot-api/bin/telegram-bot-api

FROM debian:13-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

RUN apt-get update \
    && apt-get install -y --no-install-recommends libssl3t64 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/telegram-bot-api/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

# A binary that does not start, or a server too old for the bot, fails the
# build rather than the deploy. The bot sends slideshows with sendRichMessage,
# which came in Bot API 10.1. --version answers without api_id and api_hash.
RUN version="$(telegram-bot-api --version 2>&1)" \
    && echo "$version" \
    && number="${version#Bot API }" \
    && if [ "$(printf '%s\n' 10.1 "$number" | sort -V | head -n 1)" != 10.1 ] || [ "$number" = "$version" ]; then \
         echo "The bot needs Bot API 10.1 or later for rich messages, this server is: $version" >&2; exit 1; \
       fi

# Not root. On the server it runs as the deploy user, as the bot does, so the
# files it keeps in its working directory belong to that user.
USER nobody

# 8081 is the Bot API. It is not to be published to the host: the bot's token
# goes over it in plain HTTP, and the bot reaches it over Docker's network.
EXPOSE 8081
ENTRYPOINT ["telegram-bot-api"]
