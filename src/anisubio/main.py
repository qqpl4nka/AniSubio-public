import hmac
import html
import re
import asyncio
import aiohttp
import pysubs2
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from anisubio import __version__
from anisubio.config import Settings, get_settings
from anisubio.db import SessionLocal, create_schema, get_db
from anisubio.models import (
    ReviewItem,
    ReviewAnalysis,
    ExternalIdMapping,
    FansubsCatalogItem,
    StorageObject,
    SubtitleAsset,
    UnresolvedSubtitle,
)
from anisubio.schemas import ImportRequest, ImportResult, SubtitleItem, SubtitleResponse
from anisubio.services.importer import import_subtitles
from anisubio.services.external_ids import resolve_imdb_series
from anisubio.services.jobs import enqueue_sync_job
from anisubio.storage import LocalCache
from anisubio.storage.factory import create_telegram_transport


KITSU_VIDEO_ID = re.compile(r"^kitsu:(?P<kitsu_id>\d+):(?P<episode>\d+)$")
IMDB_VIDEO_ID = re.compile(
    r"^(?P<imdb_id>tt\d+):(?P<season>\d+):(?P<episode>\d+)$"
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().ensure_directories()
    create_schema()
    yield


app = FastAPI(
    title="AniSubio",
    version=__version__,
    docs_url="/admin/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS", "POST"],
    allow_headers=["*"],
)


def cache_headers(seconds: int) -> dict[str, str]:
    return {"Cache-Control": f"public, max-age={seconds}"}


def load_subtitles_with_encoding_fallback(path: Path) -> pysubs2.SSAFile:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1251", "utf-16", "koi8-r"):
        try:
            return pysubs2.load(str(path), encoding=encoding)
        except Exception as exc:
            last_error = exc
    if last_error is None:
        raise ValueError("No subtitle encodings configured")
    raise last_error


def addon_manifest_url(public_base_url: str) -> str:
    return f"{public_base_url}/v2/manifest.json"


def stremio_install_url(public_base_url: str) -> str:
    manifest_url = addon_manifest_url(public_base_url)
    transport_url = manifest_url.removeprefix("https://").removeprefix(
        "http://"
    )
    return f"stremio://{transport_url}"


def stremio_web_install_url(public_base_url: str) -> str:
    manifest_url = addon_manifest_url(public_base_url)
    return (
        "https://web.stremio.com/#/addons?addon="
        + quote(manifest_url, safe="")
    )


@app.get("/", response_class=HTMLResponse)
def addon_home(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    subtitle_count = db.scalar(
        select(func.count()).select_from(SubtitleAsset)
    ) or 0
    anime_count = db.scalar(
        select(func.count(distinct(SubtitleAsset.kitsu_id)))
    ) or 0
    manifest_url = addon_manifest_url(settings.public_base_url)
    install_url = stremio_install_url(settings.public_base_url)
    web_install_url = stremio_web_install_url(settings.public_base_url)
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AniSubio — русские аниме-субтитры для Stremio</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center;
      background:radial-gradient(circle at top,#3b2768,#11101a 55%); color:#fff; }}
    main {{ width:min(720px,calc(100% - 32px)); padding:40px; box-sizing:border-box;
      border:1px solid #ffffff24; border-radius:28px; background:#171523e8;
      box-shadow:0 24px 80px #0008; }}
    h1 {{ margin:0 0 12px; font-size:clamp(36px,8vw,64px); }}
    p {{ color:#d6d0e4; line-height:1.6; }}
    .stats {{ display:flex; gap:12px; flex-wrap:wrap; margin:24px 0; }}
    .stat {{ padding:14px 18px; border-radius:16px; background:#ffffff0d; }}
    .stat strong {{ display:block; font-size:24px; color:#d8b7ff; }}
    .button {{ display:inline-block; margin:12px 8px 0 0; padding:15px 24px;
      border-radius:999px; background:#9c6cff; color:#fff; text-decoration:none;
      font-weight:750; }}
    .secondary {{ background:#ffffff14; border:1px solid #ffffff30; }}
    code {{ overflow-wrap:anywhere; color:#cbb7ef; }}
  </style>
</head>
<body><main>
  <h1>AniSubio</h1>
  <p>Живой Stremio-аддон русских аниме-субтитров. База продолжает
  пополняться фоновым парсером без переустановки аддона.</p>
  <div class="stats">
    <div class="stat"><strong>{subtitle_count:,}</strong>дорожек</div>
    <div class="stat"><strong>{anime_count:,}</strong>аниме</div>
  </div>
  <a class="button" href="{html.escape(web_install_url)}">Открыть установку</a>
  <a class="button secondary" href="{html.escape(install_url)}">Desktop-ссылка</a>
  <p><strong>Важно:</strong> на открывшейся карточке AniSubio нажмите
  <em>Install</em>. Простого открытия карточки недостаточно.</p>
  <p>Ручная установка: Add-ons → Add addon → вставьте:</p>
  <code id="manifest">{html.escape(manifest_url)}</code>
  <p><button class="button secondary" type="button"
    onclick="navigator.clipboard.writeText(document.getElementById('manifest').textContent)">
    Копировать manifest URL</button></p>
</main></body></html>"""
    return HTMLResponse(
        page,
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/manifest.json")
def manifest(settings: Settings = Depends(get_settings)) -> JSONResponse:
    body = {
        "id": "ru.anisubio.subtitles",
        "version": __version__,
        "name": "AniSubio",
        "description": "Русские субтитры для аниме",
        "behaviorHints": {"configurable": False},
        "resources": [
            {
                "name": "subtitles",
                "types": ["series"],
                "idPrefixes": ["kitsu:", "tt"],
            }
        ],
        "types": ["series"],
        "catalogs": [],
    }
    return JSONResponse(
        body, headers=cache_headers(settings.manifest_cache_seconds)
    )


@app.get("/v2/manifest.json")
def manifest_v2() -> JSONResponse:
    body = {
        "id": "ru.anisubio.subtitles.v2",
        "version": "1.0.0",
        "name": "AniSubio",
        "description": "Русские субтитры для аниме",
        "behaviorHints": {"configurable": False},
        "resources": [
            {
                "name": "subtitles",
                "types": ["series"],
                "idPrefixes": ["kitsu:", "tt"],
            }
        ],
        "types": ["series"],
        "catalogs": [],
    }
    return JSONResponse(
        body,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def _subtitle_response(
    content_type: str,
    video_id: str,
    db: Session,
    settings: Settings,
    *,
    route_prefix: str = "",
) -> JSONResponse:
    if content_type != "series":
        return JSONResponse(
            SubtitleResponse(subtitles=[]).model_dump(),
            headers=cache_headers(settings.subtitle_list_cache_seconds),
        )
    kitsu_match = KITSU_VIDEO_ID.fullmatch(video_id)
    imdb_match = IMDB_VIDEO_ID.fullmatch(video_id)
    if kitsu_match:
        kitsu_id = int(kitsu_match.group("kitsu_id"))
        episode = int(kitsu_match.group("episode"))
    elif imdb_match:
        episode = int(imdb_match.group("episode"))
        try:
            kitsu_id = await resolve_imdb_series(
                db,
                imdb_match.group("imdb_id"),
                int(imdb_match.group("season")),
            )
        except (aiohttp.ClientError, TimeoutError):
            kitsu_id = None
        if kitsu_id is None:
            return JSONResponse(
                SubtitleResponse(subtitles=[]).model_dump(),
                headers=cache_headers(60),
            )
    else:
        return JSONResponse(
            SubtitleResponse(subtitles=[]).model_dump(),
            headers=cache_headers(settings.subtitle_list_cache_seconds),
        )
    assets = db.scalars(
        select(SubtitleAsset)
        .where(
            SubtitleAsset.kitsu_id == kitsu_id,
            SubtitleAsset.episode == episode,
        )
        .order_by(SubtitleAsset.id)
    ).all()
    assets = [
        asset
        for asset in assets
        if asset.fansubs_id is None
        or (
            (catalog_item := db.get(FansubsCatalogItem, asset.fansubs_id))
            is not None
            and catalog_item.resolution_status == "resolved"
            and catalog_item.kitsu_id == asset.kitsu_id
        )
    ]
    if not assets:
        enqueue_sync_job(db, kitsu_id, episode, reason="lazy_load")
        return JSONResponse(
            SubtitleResponse(subtitles=[]).model_dump(),
            headers={
                **cache_headers(5),
                "X-AniSubio-Job": "queued",
                "Retry-After": "10",
            },
        )
    # Stremio exposes every returned object as a separate variant. Keep the
    # list useful instead of flooding the player with every archived release.
    assets = sorted(
        assets,
        key=lambda asset: (
            "rus-old" in asset.original_filename.casefold(),
            "sign" in asset.original_filename.casefold(),
            asset.id,
        ),
    )[:3]
    response = SubtitleResponse(
        subtitles=[
            SubtitleItem(
                id=f"anisubio-{asset.id}-{asset.checksum[:12]}",
                url=(
                    f"{settings.public_base_url}{route_prefix}"
                    f"/files/{asset.id}.srt"
                ),
                lang=asset.language,
                name=f"AniSubio • {asset.display_name}",
            )
            for asset in assets
        ]
    )
    return JSONResponse(
        response.model_dump(),
        headers=cache_headers(settings.subtitle_list_cache_seconds),
    )


@app.get("/subtitles/{content_type}/{video_id}.json")
async def subtitles(
    content_type: str,
    video_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    return await _subtitle_response(content_type, video_id, db, settings)


@app.get("/v2/subtitles/{content_type}/{video_id}.json")
async def subtitles_v2(
    content_type: str,
    video_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    return await _subtitle_response(
        content_type,
        video_id,
        db,
        settings,
        route_prefix="/v2",
    )


@app.get("/subtitles/{content_type}/{video_id}/{extra_args}.json")
async def subtitles_with_extra(
    content_type: str,
    video_id: str,
    extra_args: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    del extra_args
    return await _subtitle_response(content_type, video_id, db, settings)


@app.get("/v2/subtitles/{content_type}/{video_id}/{extra_args}.json")
async def subtitles_v2_with_extra(
    content_type: str,
    video_id: str,
    extra_args: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    del extra_args
    return await _subtitle_response(
        content_type,
        video_id,
        db,
        settings,
        route_prefix="/v2",
    )


@app.get("/files/{asset_id}")
@app.get("/files/{asset_id}.{extension}")
@app.get("/v2/files/{asset_id}")
@app.get("/v2/files/{asset_id}.{extension}")
async def subtitle_file(
    asset_id: int,
    extension: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    asset = db.get(SubtitleAsset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Subtitle not found")
    if extension is not None and extension.casefold() != "srt":
        raise HTTPException(status_code=404, detail="Subtitle format mismatch")
    if asset.storage_object_id is None:
        if not asset.stored_filename:
            raise HTTPException(status_code=404, detail="Subtitle object is missing")
        legacy_path = settings.storage_dir / asset.stored_filename
        if not legacy_path.is_file():
            raise HTTPException(status_code=404, detail="Subtitle file is missing")
        return FileResponse(
            legacy_path,
            media_type=asset.media_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{asset.checksum}"',
                "Access-Control-Allow-Origin": "*",
            },
        )

    storage_object = db.get(StorageObject, asset.storage_object_id)
    if not storage_object or storage_object.backend != "telegram":
        raise HTTPException(status_code=404, detail="Storage object is missing")

    cache = LocalCache(settings.local_cache_dir, settings.local_cache_max_bytes)
    cached = cache.get(storage_object.checksum)
    if cached is None:
        lock = await cache.lock_for(storage_object.checksum)
        async with lock:
            cached = cache.get(storage_object.checksum)
            if cached is None:
                suffix = Path(storage_object.original_filename).suffix.lower()
                transport = create_telegram_transport(settings)
                with NamedTemporaryFile(
                    dir=settings.temp_dir,
                    suffix=suffix,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                try:
                    await transport.download_to(
                        storage_object.object_id,
                        temporary_path,
                    )
                    cached = cache.put(
                        temporary_path,
                        storage_object.checksum,
                        suffix,
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="Telegram storage is temporarily unavailable",
                    ) from exc
                finally:
                    temporary_path.unlink(missing_ok=True)

    rendered_dir = settings.local_cache_dir / "rendered-srt"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered = rendered_dir / f"{storage_object.checksum}.srt"
    if not rendered.is_file():
        render_lock = await cache.lock_for(f"srt:{storage_object.checksum}")
        async with render_lock:
            if not rendered.is_file():
                temporary_rendered = rendered.with_suffix(".srt.part")
                try:
                    subtitles = await asyncio.to_thread(
                        load_subtitles_with_encoding_fallback,
                        cached,
                    )
                    await asyncio.to_thread(
                        subtitles.save,
                        str(temporary_rendered),
                        format_="srt",
                        encoding="utf-8",
                    )
                    temporary_rendered.replace(rendered)
                except Exception as exc:
                    temporary_rendered.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=422,
                        detail="Subtitle conversion failed",
                    ) from exc

    return FileResponse(
        rendered,
        media_type="application/x-subrip",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{storage_object.checksum}-srt"',
            "Access-Control-Allow-Origin": "*",
            "Content-Disposition": (
                f'inline; filename="anisubio-{asset.id}.srt"'
            ),
        },
    )


def require_admin(
    x_admin_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


@app.post(
    "/admin/import",
    response_model=ImportResult,
    dependencies=[Depends(require_admin)],
)
async def admin_import(
    request: ImportRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ImportResult:
    try:
        return await import_subtitles(request, db, settings)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/admin/unresolved",
    dependencies=[Depends(require_admin)],
)
def unresolved_subtitles(
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = db.scalars(
        select(UnresolvedSubtitle)
        .where(UnresolvedSubtitle.status == "pending_review")
        .order_by(UnresolvedSubtitle.created_at)
    ).all()
    return [
        {
            "id": row.id,
            "kitsu_id": row.kitsu_id,
            "fansubs_title_id": row.fansubs_title_id,
            "fansubs_archive_id": row.fansubs_archive_id,
            "original_filename": row.original_filename,
            "checksum": row.checksum,
            "reason": row.reason,
        }
        for row in rows
    ]


@app.get(
    "/admin/review",
    dependencies=[Depends(require_admin)],
)
def review_queue(
    status: str = "pending_review",
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = db.scalars(
        select(ReviewItem)
        .where(ReviewItem.status == status)
        .order_by(ReviewItem.created_at)
    ).all()
    result = []
    for row in rows:
        analysis = db.scalar(
            select(ReviewAnalysis).where(
                ReviewAnalysis.review_item_id == row.id
            )
        )
        result.append({
            "id": row.id,
            "item_type": row.item_type,
            "category": row.category,
            "kitsu_id": row.kitsu_id,
            "sync_job_id": row.sync_job_id,
            "unresolved_subtitle_id": row.unresolved_subtitle_id,
            "fansubs_id": row.fansubs_id,
            "source_url": row.source_url,
            "summary": row.summary,
            "attempts": row.attempts,
            "analysis": (
                {
                    "recommendation": analysis.recommendation,
                    "confidence_percent": analysis.confidence_percent,
                    "candidate_episode": analysis.candidate_episode,
                    "retryable": bool(analysis.retryable),
                }
                if analysis
                else None
            ),
        })
    return result


@app.post(
    "/admin/unresolved/{issue_id}/resolve",
    dependencies=[Depends(require_admin)],
)
def resolve_unresolved_subtitle(
    issue_id: int,
    episode: int,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if episode <= 0:
        raise HTTPException(status_code=422, detail="Episode must be positive")
    issue = db.get(UnresolvedSubtitle, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    storage_object = db.get(StorageObject, issue.storage_object_id)
    if storage_object is None:
        raise HTTPException(status_code=409, detail="Storage object is missing")
    asset = db.scalar(
        select(SubtitleAsset).where(
            SubtitleAsset.kitsu_id == issue.kitsu_id,
            SubtitleAsset.episode == episode,
            SubtitleAsset.storage_object_id == issue.storage_object_id,
        )
    )
    if asset is None:
        asset = SubtitleAsset(
            kitsu_id=issue.kitsu_id,
            fansubs_id=issue.fansubs_title_id,
            episode=episode,
            language="rus",
            display_name=Path(issue.original_filename).stem,
            original_filename=issue.original_filename,
            media_type=storage_object.media_type,
            checksum=issue.checksum,
            storage_object_id=issue.storage_object_id,
            stored_filename=None,
            source_url=(
                f"http://fansubs.ru/base.php?id={issue.fansubs_title_id}"
                f"#srt={issue.fansubs_archive_id}"
                if issue.fansubs_title_id
                else None
            ),
        )
        db.add(asset)
    issue.status = "resolved"
    issue.resolved_episode = episode
    review_item = db.scalar(
        select(ReviewItem).where(
            ReviewItem.unresolved_subtitle_id == issue.id
        )
    )
    if review_item is not None:
        review_item.status = "resolved"
        db.add(review_item)
    db.add(issue)
    db.commit()
    db.refresh(asset)
    return {
        "status": "resolved",
        "issue_id": issue.id,
        "asset_id": asset.id,
        "episode": episode,
    }
