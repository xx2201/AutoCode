import json

import pytest

from autocode.workspaces import WorkspaceRegistry


def test_cli_registry_registers_existing_projects_and_updates_recency(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()

    first = registry.register(first_path)
    second = registry.register(second_path)
    first_again = registry.register(first_path)

    workspaces = registry.list_workspaces()
    assert [item["workspace_id"] for item in workspaces] == [
        first.workspace_id,
        second.workspace_id,
    ]
    assert first_again.workspace_id == first.workspace_id
    assert registry.resolve(first.workspace_id) == first_path.resolve()


def test_registry_does_not_discover_unregistered_directories(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    registered_path = tmp_path / "registered"
    unregistered_path = tmp_path / "unregistered"
    registered_path.mkdir()
    unregistered_path.mkdir()
    registered = registry.register(registered_path)

    assert [item["workspace_id"] for item in registry.list_workspaces()] == [
        registered.workspace_id
    ]
    with pytest.raises(ValueError, match="not registered by the local CLI"):
        registry.resolve("00000000000000000000")


def test_registry_hides_registered_project_after_directory_is_removed(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    project = tmp_path / "project"
    project.mkdir()
    workspace = registry.register(project)
    project.rmdir()

    assert registry.list_workspaces() == []
    with pytest.raises(ValueError, match="not registered by the local CLI"):
        registry.resolve(workspace.workspace_id)


def test_registry_rejects_nonexistent_project(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")

    with pytest.raises(ValueError, match="does not exist"):
        registry.register(tmp_path / "missing")


def test_registry_fails_loudly_for_invalid_json(tmp_path):
    registry_file = tmp_path / "workspaces.json"
    registry_file.write_text("{broken", encoding="utf-8")
    registry = WorkspaceRegistry(registry_file)

    with pytest.raises(RuntimeError, match="Cannot read workspace registry"):
        registry.list_workspaces()


def test_registry_file_has_explicit_version(tmp_path):
    registry_file = tmp_path / "workspaces.json"
    project = tmp_path / "project"
    project.mkdir()
    WorkspaceRegistry(registry_file).register(project)

    payload = json.loads(registry_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["workspaces"]) == 1
