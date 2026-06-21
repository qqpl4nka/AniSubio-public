from pydantic import BaseModel, Field, HttpUrl, model_validator


class ImportRequest(BaseModel):
    kitsu_id: int = Field(gt=0)
    archive_url: HttpUrl | None = None
    source_page_url: HttpUrl | None = None
    fansubs_subtitle_id: int | None = Field(default=None, gt=0)
    language: str = "rus"
    filename_episode_offset: int = 0

    @model_validator(mode="after")
    def exactly_one_source(self) -> "ImportRequest":
        supplied = [self.archive_url, self.source_page_url]
        if sum(item is not None for item in supplied) != 1:
            raise ValueError("Укажите ровно один из archive_url или source_page_url")
        if self.archive_url and self.fansubs_subtitle_id:
            raise ValueError(
                "fansubs_subtitle_id используется только вместе с source_page_url"
            )
        return self


class ImportResult(BaseModel):
    archive_url: str
    imported: int
    duplicates: int
    unresolved_files: list[str]
    imported_episodes: list[int]


class SubtitleItem(BaseModel):
    id: str
    url: str
    lang: str
    name: str | None = None


class SubtitleResponse(BaseModel):
    subtitles: list[SubtitleItem]
