# Архитектура AniSubio

## Поток данных

```text
fansubs.ru project page / archive URL
                  |
                  v
        source adapter + allowlist
                  |
                  v
        bounded HTTP downloader
                  |
                  v
       safe ZIP/RAR extraction
                  |
                  v
      filename episode classifier
                  |
         +--------+---------+
         |                  |
         v                  v
  mapped subtitles     unresolved report
         |
         v
  DB metadata + content-addressed files
         |
         v
 /subtitles/series/kitsu:<id>:<episode>.json
         |
         v
      Stremio player
```

## Компоненты

### Source adapter

`FansubsSource` принимает конкретную карточку `fansubs.ru`. Сайт работает по
legacy HTTP и отдаёт Windows-1251. Архив загружается не прямой ссылкой, а формой:

```text
POST http://fansubs.ru/base.php
Content-Type: application/x-www-form-urlencoded

srt=<subtitle_id>
```

Адаптер находит `form input[name=srt]`, не привязываясь к нестабильным
CSS-классам. Если форм несколько, импорт требует `fansubs_subtitle_id`.

Источник ограничен allowlist доменов. Это закрывает SSRF через административный
endpoint. Перед массовым обходом сайта необходимо отдельно проверить правила
сайта, `robots.txt`, допустимую частоту запросов и разрешение на повторную
публикацию файлов. На момент исследования `robots.txt` разрешает обычные
публичные страницы для общего `User-agent: *`, запрещает `/forum/`, а отдельным
AI-ботам запрещает весь сайт. Поэтому текущий MVP не делает массовый crawler:
он загружает только явно указанную администратором карточку.

### Загрузка и распаковка

- потоковая загрузка без помещения всего архива в память;
- лимиты размера архива, числа файлов и общего распакованного размера;
- запрет абсолютных путей и `..`;
- whitelist расширений `.ass`, `.ssa`, `.srt`, `.vtt`;
- SHA-256 и content-addressed storage, поэтому одинаковые файлы не копируются.

RAR требует системный `7z`, `unrar` или `bsdtar`. Docker-образ включает `7z`.

### Сопоставление эпизодов

Поддержаны распространённые имена:

```text
Title - 01.ass
Title [02].srt
Title.E03.1080p.ass
Title - Episode 004.vtt
Title - 2x07.ass
```

Неопределённые файлы не публикуются и возвращаются в `unresolved_files`.
Параметр `filename_episode_offset` позволяет исправить наборы с нумерацией от
нуля. Следующий производственный шаг — таблица ручных overrides и preview перед
подтверждением импорта.

Kitsu обычно имеет отдельный ID для каждого сезона, поэтому ключ:

```text
(kitsu_season_id, episode_number)
```

### База и файлы

SQLite подходит для одного экземпляра и MVP. Для production с несколькими
воркерами рекомендуется:

- PostgreSQL для метаданных и заданий;
- S3/MinIO для файлов;
- Redis + Celery/Dramatiq/RQ для фонового импорта;
- CDN перед `/files`;
- Alembic для миграций.

Файлы хранятся отдельно от БД. В БД находятся Kitsu ID, эпизод, язык, checksum,
исходное имя, MIME type и source URL.

### Stremio API

```text
GET /manifest.json
GET /subtitles/series/kitsu:11:1.json
GET /subtitles/series/kitsu:11:1/<extraArgs>.json
GET /files/<sha256>/<filename>
```

Ответ:

```json
{
  "subtitles": [
    {
      "id": "anisubio-42-acde1234",
      "url": "https://addon.example/files/<sha256>/<filename>",
      "lang": "rus"
    }
  ]
}
```

### Кэширование

- manifest: 1 час;
- список субтитров: 30 минут;
- immutable-файлы с SHA-256 в URL: 1 год.

Если импорт должен немедленно становиться виден, CDN-кэш конкретного subtitle
endpoint нужно инвалидировать либо версионировать URL API.

## Production pipeline

Для production импорт следует вынести из HTTP-запроса:

1. Admin API создаёт `import_job`.
2. Worker скачивает и проверяет архив.
3. Worker строит preview маппинга.
4. Администратор подтверждает спорные эпизоды.
5. Worker атомарно публикует записи.
6. CDN получает purge для затронутых эпизодов.

Это не позволяет долгой загрузке архива занять FastAPI worker и даёт
идемпотентные retry.

## Background sync worker

`anisubio.sync_worker` работает только по allowlist Kitsu ID из конфигурации.
Он не обходит алфавитный каталог fansubs.ru.

```text
Kitsu ID
   |
   +--> Kitsu /anime/{id}/mappings --> MAL ID
   |
   +--> Shikimori /api/animes/{mal_id} --> русское имя + aliases
   |
   +--> POST fansubs.ru/search.php (CP1251)
   |
   +--> exact normalized title match
   |
   +--> GET card --> POST base.php?srt=<id> --> archives
   |
   +--> safe extraction --> episode mapping --> SQLite
```

Единый `aiohttp.ClientSession` имеет `limit_per_host=1`, а каждый следующий
запрос ждёт случайную паузу 3–5 секунд. User-Agent содержит браузерно-совместимые
поля, но явно идентифицирует AniSubio. Неоднозначное название не выбирается по
fuzzy score: задача завершается ошибкой и записывает её в `sync_records`.

## Telegram object storage

```text
FastAPI / worker
      |
      | SOCKS5 127.0.0.1:10990
      v
Telegram Bot API
      |
      +-- AniSubio Storage
      `-- AniSubio Database Backups
```

Codex MCP не является runtime-зависимостью. Он создаёт каналы и выполняет
административные операции. Production-код использует отдельного бота.

В `storage_objects` сохраняются:

- backend и универсальный object ID;
- `chat_id` и `message_id`;
- Bot API `file_id` и `file_unique_id`;
- SHA-256, размер, имя и MIME type.

`/files/<asset_id>` никогда не перенаправляет клиента на Telegram URL, потому
что такой URL содержит bot token. FastAPI проксирует файл и поддерживает
локальный горячий cache.

## Durable lazy-load

`sync_jobs.active_key = kitsu:<id>` обеспечивает single-flight для одного
сезона. Cache miss только добавляет job и сразу возвращает:

```json
{"subtitles": []}
```

Один worker последовательно забирает задачи. SQLite работает в WAL mode с
`busy_timeout=5000`. Для нескольких worker механизм claim потребуется заменить
на PostgreSQL `FOR UPDATE SKIP LOCKED`.

## Catalog vacuum

Полный каталог сканируется отдельным процессом, чтобы длинный алфавитный проход
не блокировал пользовательские job:

```text
vacuum-worker                 sync-worker
      |                            |
55 index pages --5 sec--> catalog |
      |                            |
Shikimori exact match              |
      |                            |
MAL → Kitsu mapping                |
      |                            |
low priority SyncJob ------------> season import
                                   |
lazy-load high priority ---------->|
```

Metadata API на RU VPS могут использовать германский SOCKS5 через
`ANISUBIO_METADATA_PROXY_URL`. Fansubs.ru остаётся на прямом соединении с RU
VPS. Повторные попытки также соблюдают пятисекундную паузу.
