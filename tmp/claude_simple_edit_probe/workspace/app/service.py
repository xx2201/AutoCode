from app.cache import SimpleCache
from app.config import DEFAULT_LIMIT, DEFAULT_LIMIT_ENV
from app.env_loader import get_env
from app.normalizer import normalize_status
from app.repository import IssueRepository


class IssueService:
    def __init__(self, repo: IssueRepository, cache: SimpleCache):
        self.repo = repo
        self.cache = cache

    def _cache_key(self, status: str, limit: int) -> str:
        return f"issues:{status}:{limit}"

    def _default_limit(self) -> int:
        return int(get_env(DEFAULT_LIMIT_ENV, str(DEFAULT_LIMIT)))

    def list_titles(self, status: str, limit: int | None = None) -> list[str]:
        normalized = normalize_status(status)
        actual_limit = self._default_limit() if limit is None else limit
        key = self._cache_key(normalized, actual_limit)
        cached = self.cache.get(key)
        if cached is not None:
            return cached.split("|") if cached else []
        rows = self.repo.list_by_status(normalized)[:actual_limit]
        titles = [row["title"] for row in rows]
        self.cache.set(key, "|".join(titles))
        return titles

    def summary(self, status: str) -> str:
        titles = self.list_titles(status)
        return ",".join(titles)

    def close_issue(self, issue_id: str):
        self.repo.close(issue_id)
        self.cache.clear_prefix("issue:")
