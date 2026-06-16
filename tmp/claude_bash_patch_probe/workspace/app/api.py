from app.cache import SimpleCache
from app.data import ISSUES
from app.repository import IssueRepository
from app.service import IssueService

_service = IssueService(
    IssueRepository({issue_id: payload.copy() for issue_id, payload in ISSUES.items()}),
    SimpleCache(),
)


def build_report(status: str) -> str:
    titles = _service.list_titles(status)
    return f"count={len(titles)} first={titles[0] if titles else '-'}"


def close_issue(issue_id: str):
    _service.close_issue(issue_id)
