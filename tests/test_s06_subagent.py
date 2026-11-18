import builtins
import copy
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
LESSON = ROOT / "s06_subagent" / "code.py"


def load_lesson(temp_cwd: Path):
    fake_anthropic = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = types.SimpleNamespace(create=None)
            self.options = []

        def with_options(self, **kwargs):
            self.options.append(kwargs)
            return self

    fake_dotenv = types.ModuleType("dotenv")
    fake_anthropic.Anthropic = FakeAnthropic
    fake_dotenv.load_dotenv = lambda override=True: None

    previous_modules = {
        "anthropic": sys.modules.get("anthropic"),
        "dotenv": sys.modules.get("dotenv"),
    }
    previous_cwd = Path.cwd()
    previous_model_id = os.environ.get("MODEL_ID")
    spec = importlib.util.spec_from_file_location("s06_subagent_test", LESSON)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {LESSON}")
    module = importlib.util.module_from_spec(spec)

    sys.modules["anthropic"] = fake_anthropic
    sys.modules["dotenv"] = fake_dotenv
    try:
        os.chdir(temp_cwd)
        os.environ["MODEL_ID"] = "test-model"
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)
        if previous_model_id is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = previous_model_id
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def tool_block(name: str, tool_id: str, **tool_input):
    return types.SimpleNamespace(
        type="tool_use",
        id=tool_id,
        name=name,
        input=tool_input,
    )


def test_s06_is_kernel_plus_task():
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))

    base_names = {tool["name"] for tool in lesson.BASE_TOOLS}
    parent_names = {tool["name"] for tool in lesson.TOOLS}
    child_names = {tool["name"] for tool in lesson.SUB_TOOLS}

    assert base_names == {"bash", "read_file", "write_file", "edit_file", "glob"}
    assert parent_names == base_names | {"task"}
    assert child_names == base_names
    assert "todo_write" not in parent_names
    assert "task" not in child_names
    assert lesson.TASK_TOOL["input_schema"]["required"] == ["prompt"]
    assert lesson.large_output_hook in lesson.HOOKS["PostToolUse"]


def test_subagent_starts_with_fresh_messages_and_returns_final_text():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "note.txt").write_text("child input")
        lesson = load_lesson(root)
        calls = []
        responses = [
            types.SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_block("read_file", "read_1", path="note.txt")],
            ),
            types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="The note says child input.")],
            ),
        ]

        def create(**kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            return responses.pop(0)

        lesson.client.messages.create = create
        result = lesson.run_subagent("Read note.txt and report its contents.")

    assert calls[0]["messages"] == [
        {"role": "user", "content": "Read note.txt and report its contents."}
    ]
    assert {tool["name"] for tool in calls[0]["tools"]} == {
        "bash", "read_file", "write_file", "edit_file", "glob",
    }
    assert result == "The note says child input."
    assert len(calls) == 2
    assert lesson.client.options == []


def test_subagent_file_tools_keep_the_kernel_permission_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lesson = load_lesson(root)
        outside = root.parent / "s06-outside.txt"
        block = tool_block(
            "write_file",
            "write_1",
            path=str(outside),
            content="not allowed",
        )

        with patch.object(builtins, "input", return_value="n"):
            result = lesson.execute_tool(block, lesson.SUB_HANDLERS)

        assert result == "Permission denied by user"
        assert not outside.exists()


def test_budget_exhaustion_preserves_evidence_and_summarizes_without_tools():
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))
    calls = []
    executed = []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) <= 30:
            turn = len(calls)
            return types.SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    tool_block("read_file", f"read_{turn}", path="note.txt"),
                    tool_block("glob", f"glob_{turn}", pattern="*.py"),
                ],
            )
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[types.SimpleNamespace(
                type="text",
                text="Confirmed note.txt has evidence; tests remain unchecked.",
            )],
        )

    def execute(block, handlers):
        executed.append(block.id)
        return f"Evidence from {block.id}"

    lesson.client.messages.create = create
    lesson.execute_tool = execute
    # The exhausted path must not run a Stop hook that could request more work.
    lesson.HOOKS["Stop"] = [lambda _: pytest.fail("Stop hook reopened the loop")]
    result = lesson.run_subagent("Read note.txt and check tests.")

    assert result.startswith("[Subagent status: budget_exhausted]")
    assert "note.txt has evidence" in result
    assert "tests remain unchecked" in result
    assert len(calls) == 31
    # The budget counts model turns, not individual tool invocations.
    assert len(executed) == 60
    assert lesson.client.options == [{"timeout": 30.0, "max_retries": 0}]
    final_call = calls[-1]
    assert final_call["model"] == calls[0]["model"]
    assert final_call["tool_choice"] == {"type": "none"}
    assert final_call["messages"][0] == {
        "role": "user", "content": "Read note.txt and check tests."
    }
    assert "budget is exhausted" in final_call["messages"][-1]["content"]
    assert "Evidence from read_30" in str(final_call["messages"][-2])
    # Every tool call, including the final batch, has its matching result.
    transcript = final_call["messages"]
    for index, message in enumerate(transcript):
        if message["role"] != "assistant":
            continue
        ids = [block.id for block in message["content"] if block.type == "tool_use"]
        results = transcript[index + 1]["content"]
        assert [block["tool_use_id"] for block in results] == ids


@pytest.mark.parametrize("failure", [
    "timeout", "exception", "empty", "whitespace", "tool", "text_and_tool", "truncated",
])
def test_budget_summary_failure_returns_once_without_executing_tools(failure):
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))
    calls = []
    executed = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 30:
            return types.SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_block("read_file", f"read_{len(calls)}", path="note.txt")],
            )
        if failure == "timeout":
            raise TimeoutError("summary timed out")
        if failure == "exception":
            raise RuntimeError("provider unavailable")
        text = types.SimpleNamespace(type="text", text="partial answer")
        tool = tool_block("write_file", "unexpected", path="should-not-exist.txt", content="x")
        content = {
            "empty": [],
            "whitespace": [types.SimpleNamespace(type="text", text="  \n")],
            "tool": [tool],
            "text_and_tool": [text, tool],
            "truncated": [text],
        }[failure]
        return types.SimpleNamespace(
            stop_reason="max_tokens" if failure == "truncated" else "end_turn",
            content=content,
        )

    lesson.client.messages.create = create
    lesson.execute_tool = lambda block, _: executed.append(block.id) or "read evidence"
    result = lesson.run_subagent("Inspect the repository.")

    assert result.startswith("[Subagent status: budget_exhausted]")
    assert "Progress summary unavailable" in result
    assert "task may be incomplete" in result
    assert len(calls) == 31
    assert len(executed) == 30
    assert "unexpected" not in executed
    assert lesson.client.options == [{"timeout": 30.0, "max_retries": 0}]


def test_stop_hook_cannot_extend_the_budget_or_reopen_the_summary():
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))
    calls = []
    hook_calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[types.SimpleNamespace(type="text", text="current findings")],
        )

    def stop(messages):
        hook_calls.append(len(messages))
        return "Please continue checking."

    lesson.client.messages.create = create
    lesson.HOOKS["Stop"] = [stop]
    result = lesson.run_subagent("Inspect the repository.")

    assert result.startswith("[Subagent status: budget_exhausted]")
    assert len(calls) == 31
    assert len(hook_calls) == 30
    assert calls[-1]["tool_choice"] == {"type": "none"}


def test_normal_completion_on_the_last_turn_does_not_request_a_summary():
    with tempfile.TemporaryDirectory() as tmp:
        lesson = load_lesson(Path(tmp))
    calls = []
    executed = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) < 30:
            return types.SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_block("read_file", f"read_{len(calls)}", path="note.txt")],
            )
        return types.SimpleNamespace(
            stop_reason="end_turn",
            content=[types.SimpleNamespace(type="text", text="finished on turn 30")],
        )

    lesson.client.messages.create = create
    lesson.execute_tool = lambda block, _: executed.append(block.id) or "read evidence"
    result = lesson.run_subagent("Inspect the repository.")

    assert result == "finished on turn 30"
    assert len(calls) == 30
    assert len(executed) == 29
    assert lesson.client.options == []
