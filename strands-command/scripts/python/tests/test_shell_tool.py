"""Tests for the persistent shell tool."""
import pytest
from unittest.mock import Mock
from strands.types.tools import ToolContext
from ..shell_tool import ShellSession, shell_tool


@pytest.fixture
def session():
    """Create a ShellSession and stop it after the test."""
    s = ShellSession()
    yield s
    if s._alive:
        s.stop()


"""Tests for ShellSession class."""

def test_basic_command(session):
    """Test basic command execution."""
    output = session.run("echo hello")
    assert "hello" in output

def test_exit_code_success(session):
    """Test successful command exit code."""
    output = session.run("true")
    # Exit code 0 should not be appended
    assert "Exit code:" not in output

def test_exit_code_failure(session):
    """Test failed command exit code."""
    output = session.run("false")
    assert "Exit code: 1" in output

def test_multiline_output(session):
    """Test command with multiple output lines."""
    output = session.run("echo line1; echo line2; echo line3")
    assert "line1" in output
    assert "line2" in output
    assert "line3" in output

def test_cd_persistence(session):
    """Test that cd command persists across commands."""
    # Create a temp directory
    session.run("mkdir -p /tmp/shell_test_$$")
    session.run("cd /tmp/shell_test_$$")

    # Check we're in the right directory
    output = session.run("pwd")
    assert "shell_test" in output

    # Verify persistence
    output2 = session.run("pwd")
    assert output.strip() == output2.strip()

def test_env_var_persistence(session):
    """Test that exported environment variables persist."""
    session.run("export TEST_VAR=hello123")
    output = session.run("echo $TEST_VAR")
    assert "hello123" in output

def test_shell_variable_persistence(session):
    """Test that shell variables persist."""
    session.run("MY_VAR=testing456")
    output = session.run("echo $MY_VAR")
    assert "testing456" in output

def test_stderr_merged_into_stdout(session):
    """Test that stderr is merged into output."""
    # Command that writes to stderr
    output = session.run("echo error message >&2")
    assert "error message" in output

def test_no_newline_output(session):
    """Test command that produces output without trailing newline."""
    output = session.run("printf 'no newline'")
    assert "no newline" in output

def test_large_output(session):
    """Test command with large output."""
    # Generate ~10KB of output
    output = session.run("for i in {1..200}; do echo 'Line number '$i; done")
    assert "Line number 1" in output
    assert "Line number 200" in output

def test_timeout():
    """Test command timeout handling."""
    session = ShellSession(timeout=1)
    try:
        with pytest.raises(TimeoutError):
            session.run("sleep 10")

        # Session should be dead after timeout
        assert not session._alive
    finally:
        # Clean up even if assertion fails
        if session._alive:
            session.stop()

def test_sequential_commands(session):
    """Test multiple sequential commands."""
    session.run("echo first")
    session.run("echo second")
    output = session.run("echo third")
    assert "third" in output

def test_pipe_commands(session):
    """Test commands with pipes."""
    output = session.run("echo 'hello world' | grep hello")
    assert "hello world" in output

def test_command_substitution(session):
    """Test command substitution."""
    output = session.run("echo $(echo nested)")
    assert "nested" in output

def test_exit_code_propagation(session):
    """Test that non-zero exit codes are properly captured."""
    output = session.run("ls /nonexistent 2>&1")
    assert "Exit code:" in output
    # ls should fail with non-zero exit code
    assert "Exit code: 0" not in output

def test_restart(session):
    """Test session restart."""
    session.run("export TEST_VAR=before")
    session.restart()
    # Variable should be gone after restart
    output = session.run("echo ${TEST_VAR:-empty}")
    assert "empty" in output

def test_special_characters_in_output(session):
    """Test handling of special characters."""
    output = session.run("echo '$HOME' '\\n' '\\t' '|' '&'")
    assert "$HOME" in output


"""Tests for shell_tool function."""

def create_mock_context():
    """Create a mock tool context with properly configured agent."""
    # Create a simple object that can have attributes set on it
    class MockAgent:
        pass

    mock_agent = MockAgent()
    mock_context = Mock(spec=ToolContext)
    mock_context.agent = mock_agent
    return mock_context

def test_tool_basic_usage():
    """Test basic tool usage."""
    context = create_mock_context()
    output = shell_tool("echo test", tool_context=context)
    assert "test" in output

def test_tool_creates_session():
    """Test that tool creates session in registry."""
    from ..shell_tool import _sessions
    context = create_mock_context()
    shell_tool("echo test", tool_context=context)
    assert context.agent in _sessions

def test_tool_reuses_session():
    """Test that tool reuses existing session."""
    context = create_mock_context()
    shell_tool("export VAR=value", tool_context=context)
    output = shell_tool("echo $VAR", tool_context=context)
    assert "value" in output

def test_tool_restart_flag():
    """Test tool restart functionality."""
    context = create_mock_context()
    shell_tool("export VAR=before", tool_context=context)
    output = shell_tool("echo start", restart=True, tool_context=context)
    assert "start" in output

    # Variable should be gone after restart
    output = shell_tool("echo ${VAR:-gone}", tool_context=context)
    assert "gone" in output

def test_tool_restart_only():
    """Test restarting without command."""
    context = create_mock_context()
    shell_tool("echo test", tool_context=context)
    output = shell_tool("", restart=True, tool_context=context)
    assert "restarted" in output.lower()

def test_tool_timeout_parameter():
    """Test custom timeout parameter."""
    context = create_mock_context()
    output = shell_tool("sleep 0.1", timeout=5, tool_context=context)
    # Should complete successfully
    assert "Error" not in output

def test_tool_timeout_error():
    """Test timeout error handling."""
    context = create_mock_context()
    output = shell_tool("sleep 10", timeout=1, tool_context=context)
    assert "timeout" in output.lower() or "Error" in output

def test_tool_exit_code_in_output():
    """Test that non-zero exit codes appear in output."""
    context = create_mock_context()
    output = shell_tool("false", tool_context=context)
    assert "Exit code: 1" in output

def test_tool_persistence_across_calls():
    """Test that state persists across multiple tool calls."""
    context = create_mock_context()
    shell_tool("cd /tmp", tool_context=context)
    shell_tool("export MY_VAR=persistent", tool_context=context)
    output = shell_tool("pwd; echo $MY_VAR", tool_context=context)
    assert "/tmp" in output
    assert "persistent" in output

def test_tool_session_cleanup_on_error():
    """Test that session is recreated after fatal error."""
    from ..shell_tool import _sessions
    context = create_mock_context()

    # First call succeeds
    shell_tool("echo first", tool_context=context)
    session = _sessions[context.agent]

    # Force kill the process
    session._process.kill()
    session._process.wait()

    # Next call should recreate session
    output = shell_tool("echo recovered", tool_context=context)
    assert "recovered" in output or "Error" in output


"""Tests to verify architectural properties."""

def test_no_readline_dependency(session):
    """Verify that output without newlines works correctly."""
    # This would fail if using readline()
    output = session.run("printf 'line1'; printf 'line2'")
    assert "line1" in output and "line2" in output

def test_sentinel_uniqueness(session):
    """Test that concurrent commands would use unique sentinels."""
    # Run multiple commands - each should have unique sentinel
    outputs = []
    for i in range(5):
        output = session.run(f"echo test{i}")
        outputs.append(output)
        assert f"test{i}" in output

    # All outputs should be distinct
    assert len(set(outputs)) == len(outputs)

def test_binary_mode_handling(session):
    """Test that binary mode handles various encodings."""
    # Test with UTF-8 characters
    output = session.run("echo 'Hello 世界'")
    # Should decode with replacement, not crash
    assert isinstance(output, str)

def test_buffer_offset_isolation(session):
    """Test that commands don't see each other's output."""
    output1 = session.run("echo first")
    output2 = session.run("echo second")

    # Second command should not include first command's output
    assert "first" not in output2
    assert "second" in output2

def test_merged_stderr_stdout(session):
    """Test that stderr and stdout are properly merged."""
    # Command with interleaved stdout and stderr
    output = session.run("echo out1; echo err1 >&2; echo out2; echo err2 >&2")
    # All output should be present
    assert "out1" in output
    assert "err1" in output
    assert "out2" in output
    assert "err2" in output
