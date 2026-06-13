import json

from autocode.agent import Agent
from autocode.llm import LLMResponse, ToolCall
from autocode.state import checkpoint as checkpoint_module


class _TwoRoundLLM:
    def __init__(self):
        self.model = "fake-model"
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None):
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                content="先读文件",
                tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"file_path": "demo.txt"})],
                prompt_tokens=111,
                completion_tokens=22,
            )
        return LLMResponse(content="完成", prompt_tokens=222, completion_tokens=33)


def test_task_writes_llm_round_files(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    tmp_path.joinpath("demo.txt").write_text("hello", encoding="utf-8")

    agent = Agent(
        llm=_TwoRoundLLM(),
        workspace_root=str(tmp_path),
        auto_approve=True,
    )

    reply = agent.chat("读一下 demo.txt 然后结束")
    assert reply == "完成"
    assert agent.task_state is not None

    session_dir = tmp_path / agent.session_state.session_id
    md_path = session_dir / "llm_rounds.md"
    raw_path = session_dir / "llm_rounds.jsonl"

    assert md_path.exists()
    assert raw_path.exists()

    md = md_path.read_text(encoding="utf-8")
    assert "# LLM Rounds" in md
    assert "## Round 1" in md
    assert "## Round 2" in md
    assert "### system" in md
    assert "### messages" in md
    assert agent.session_state.session_id in md
    assert agent.task_state.task_id in md
    assert "demo.txt" in md
    assert '"role": "user"' in md
    assert '"role": "system"' not in md
    assert "\"prompt_tokens\": 111" in md
    assert "\"completion_tokens\": 33" in md

    rounds = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert len(rounds) == 2
    assert rounds[0]["messages"][0]["role"] == "system"
    assert rounds[0]["messages"][1]["role"] == "user"
    assert rounds[0]["response"]["tool_calls"][0]["name"] == "read_file"
    assert rounds[1]["response"]["content"] == "完成"
