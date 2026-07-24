import base64

import pytest

from autocode.web.files import WebFileStore, WebSendTool


def test_web_send_tool_uses_current_channel_sender():
    calls = []
    tool = WebSendTool(lambda path: calls.append(path) or "attached")

    assert tool.execute("reports/result.pdf") == "attached"
    assert calls == ["reports/result.pdf"]


def test_file_store_offers_and_reads_workspace_file(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    report = workspace / "简历.pdf"
    report.write_bytes(b"%PDF-test")
    store = WebFileStore()

    offered = store.offer("workspace-a", workspace, str(report))
    downloaded = store.read("workspace-a", offered["file_id"])

    assert offered["name"] == "简历.pdf"
    assert offered["media_type"] == "application/pdf"
    assert offered["can_preview"] is True
    assert base64.b64decode(downloaded["data_base64"]) == b"%PDF-test"


def test_file_store_rejects_outside_and_protected_files(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    env_file = workspace / ".env"
    env_file.write_text("TOKEN=secret", encoding="utf-8")
    autocode_dir = workspace / ".autocode"
    autocode_dir.mkdir()
    transcript = autocode_dir / "transcript.jsonl"
    transcript.write_text("private history", encoding="utf-8")
    store = WebFileStore()

    with pytest.raises(ValueError, match="inside the current workspace"):
        store.offer("workspace-a", workspace, str(outside))
    with pytest.raises(ValueError, match="Protected workspace files"):
        store.offer("workspace-a", workspace, str(env_file))
    with pytest.raises(ValueError, match="Protected workspace files"):
        store.offer("workspace-a", workspace, str(transcript))


def test_file_offer_is_bound_to_workspace_and_expires(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("done", encoding="utf-8")
    store = WebFileStore(ttl_seconds=0)
    offered = store.offer("workspace-a", workspace, str(artifact))

    with pytest.raises(ValueError, match="invalid or has expired"):
        store.read("workspace-b", offered["file_id"])
    with pytest.raises(ValueError, match="invalid or has expired"):
        store.read("workspace-a", offered["file_id"])
