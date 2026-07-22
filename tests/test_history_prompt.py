from quickcode.config import Environment
from quickcode.core.events import AssembledToolCall, AssistantMessage
from quickcode.core.history import History
from quickcode.prompts.system import render_system_prompt, system_reminder


def _env():
    return Environment(
        cwd="/proj", platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-07-22", is_git_repo=True, git_branch="main",
    )


def test_prompt_is_pure_and_stable():
    a = render_system_prompt(_env())
    b = render_system_prompt(_env())
    assert a == b  # byte-identical → cache-stable
    assert "<identity>" in a and "<environment>" in a
    assert "/proj" in a


def test_plan_and_headless_sections_append():
    assert "<plan_mode>" in render_system_prompt(_env(), plan=True)
    assert "<headless_mode>" in render_system_prompt(_env(), headless=True)


def test_history_cache_breakpoint_on_last_block():
    h = History("SYS")
    h.push_user("hello")
    h.push_assistant(AssistantMessage(text="hi"))
    h.push_user("again")
    msgs = h.build_messages()
    assert msgs[0].role == "system" and msgs[0].cache_control is True
    assert msgs[-1].cache_control is True
    assert all(m.cache_control is False for m in msgs[1:-1])


def test_tool_results_batch_in_call_order():
    h = History("SYS")
    calls = [
        AssembledToolCall(id="a", name="read", arguments="{}"),
        AssembledToolCall(id="b", name="grep", arguments="{}"),
    ]
    h.push_assistant(AssistantMessage(tool_calls=calls))
    h.push_tool_results([(calls[0], "ra", False), (calls[1], "rb", True)])
    tool_msgs = [m for m in h.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]
    assert "[error]" in tool_msgs[1].content


def test_system_reminder_wraps():
    assert system_reminder("x").startswith("<system-reminder>")
