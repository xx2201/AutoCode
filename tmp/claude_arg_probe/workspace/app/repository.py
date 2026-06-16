class IssueRepository:
    def __init__(self, issues: dict[str, dict[str, str]]):
        self.issues = issues

    def list_by_status(self, status: str) -> list[dict[str, str]]:
        return [
            {"id": issue_id, **payload}
            for issue_id, payload in self.issues.items()
            if payload["status"] == status
        ]

    def close(self, issue_id: str):
        self.issues[issue_id]["status"] = "closed"
