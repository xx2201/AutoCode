from corecoder.checkpoint import list_checkpoints, load_checkpoint, save_checkpoint
from corecoder import checkpoint as checkpoint_module
from corecoder.state import TaskState


def test_checkpoint_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)

    state = TaskState(task_id="task_demo", status="running", step_index=3)
    messages = [{"role": "user", "content": "hello"}]
    save_checkpoint(state, messages, "demo-model")

    loaded = load_checkpoint("task_demo")
    assert loaded is not None
    loaded_state, loaded_messages, loaded_model = loaded
    assert loaded_state.task_id == "task_demo"
    assert loaded_state.step_index == 3
    assert loaded_messages == messages
    assert loaded_model == "demo-model"


def test_list_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)
    save_checkpoint(TaskState(task_id="task_one", status="waiting_approval", step_index=1), [], "m1")
    entries = list_checkpoints()
    assert len(entries) == 1
    assert entries[0]["task_id"] == "task_one"
    assert entries[0]["status"] == "waiting_approval"


def test_checkpoint_is_written_as_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)
    save_checkpoint(TaskState(task_id="utf8_task", title="帮我执行代码"), [], "m1")
    raw = tmp_path.joinpath("utf8_task/checkpoint.json").read_bytes()
    assert b"\xe5\xb8\xae\xe6\x88\x91" in raw
