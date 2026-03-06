"""Integration tests for shell_tool with Strands Agent."""
import os
import pytest
import tempfile
from strands import Agent
from ...shell_tool import shell_tool


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


def assert_shell_tool_called(agent, expected_command_substring=None):
    """Helper to validate shell_tool was called in agent messages.

    Args:
        agent: The agent instance
        expected_command_substring: Optional substring to check in the command parameter

    Note: Validates across ALL shell_tool calls in the conversation, not just the last one.
    """
    # Check that there are messages (at least user message + assistant response)
    assert len(agent.messages) >= 2, f"Expected at least 2 messages, got {len(agent.messages)}"

    # Collect all shell_tool calls
    shell_tool_calls = []
    for msg in agent.messages:
        content = msg.get('content', []) if isinstance(msg, dict) else []
        for block in content:
            if isinstance(block, dict) and 'toolUse' in block:
                tool_use = block['toolUse']
                if tool_use.get('name') == 'shell_tool':
                    command = tool_use.get('input', {}).get('command', '')
                    shell_tool_calls.append(command)

    # Validate shell_tool was called at least once
    assert len(shell_tool_calls) > 0, f"shell_tool was not called. Messages: {agent.messages}"

    # If checking for specific command substring, validate at least one call contains it
    if expected_command_substring:
        matching_calls = [cmd for cmd in shell_tool_calls if expected_command_substring in cmd]
        assert len(matching_calls) > 0, \
            f"Expected '{expected_command_substring}' in at least one shell_tool call. All calls: {shell_tool_calls}"


def test_basic_execution():
    """Test basic shell command execution through agent."""
    agent = Agent(tools=[shell_tool])
    result = agent("Run: echo 'hello'")
    result_str = str(result)
    print(f"\nBasic execution result: {result_str[:300]}")
    assert result is not None

    # Validate shell_tool was called with echo command
    assert_shell_tool_called(agent, expected_command_substring="echo")

    assert "hello" in result_str.lower()


def test_complex_multi_step_workflow(temp_dir):
    """Test a complex multi-step workflow with state management."""
    agent = Agent(tools=[shell_tool])
    print(f"\n--- Complex Multi-Step Workflow Test (temp dir: {temp_dir}) ---")

    # Step 1: Change to temp directory and verify by creating a marker file
    result1 = agent(f"Run: cd {temp_dir} && touch cd_marker.txt && pwd")
    result1_str = str(result1)
    print(f"Step 1 - Change to temp dir: {result1_str[:300]}")

    # Verify cd worked by checking marker file exists in temp_dir
    marker_path = os.path.join(temp_dir, "cd_marker.txt")
    assert os.path.exists(marker_path), f"cd failed - marker file not found at {marker_path}"
    print(f"✓ Verified cd worked - marker file exists at {marker_path}")

    # Step 2: Create files with shell loops (should be in temp_dir due to persistence)
    result2 = agent("""In the current directory, run: for i in 1 2 3; do echo "Line $i" > file$i.txt; done
Then list the files to confirm they were created.""")
    result2_str = str(result2)
    print(f"Step 2 - Create files: {result2_str[:300]}")

    # Verify files actually exist on disk
    file1_path = os.path.join(temp_dir, "file1.txt")
    file2_path = os.path.join(temp_dir, "file2.txt")
    file3_path = os.path.join(temp_dir, "file3.txt")
    assert os.path.exists(file1_path), f"file1.txt not found at {file1_path}"
    assert os.path.exists(file2_path), f"file2.txt not found at {file2_path}"
    assert os.path.exists(file3_path), f"file3.txt not found at {file3_path}"
    print(f"✓ Verified all 3 files exist in {temp_dir}")

    # Verify file contents
    with open(file2_path, 'r') as f:
        content = f.read()
        assert "Line 2" in content, f"file2.txt has wrong content: {content}"
    print(f"✓ Verified file2.txt contains 'Line 2'")

    # Step 3: Use pipes and command substitution
    result3 = agent("Run: cat file*.txt | wc -l")
    result3_str = str(result3)
    print(f"Step 3 - Count lines: {result3_str[:300]}")
    assert "3" in result3_str, f"Expected 3 lines, got: {result3_str}"

    # Step 4: Use grep and conditionals
    result4 = agent("""Run this command: if grep -q "Line 2" file2.txt; then echo "FOUND"; else echo "NOT FOUND"; fi""")
    result4_str = str(result4)
    print(f"Step 4 - Grep and conditional: {result4_str[:300]}")
    assert "FOUND" in result4_str, f"Conditional failed: {result4_str}"

    # Step 5: Verify persistence - we should still be in the same directory
    result5 = agent("Run: pwd")
    result5_str = str(result5)
    print(f"Step 5 - Verify pwd persistence: {result5_str[:300]}")
    assert temp_dir in result5_str, f"Lost directory context: {result5_str}"

    # Validate shell_tool was used throughout (check last message for pwd command)
    assert_shell_tool_called(agent, expected_command_substring="pwd")

    print("✓ All steps completed successfully")


def test_shell_functions_and_persistence(temp_dir):
    """Test that shell functions persist across commands."""
    agent = Agent(tools=[shell_tool])
    print("\n--- Shell Functions Persistence Test ---")

    # Define a shell function that writes to a file
    result1 = agent(f"""Run: cd {temp_dir} && greet() {{ echo "Hello, $1!" > greeting_$1.txt; }} && greet World""")
    result1_str = str(result1)
    print(f"Function definition result: {result1_str[:300]}")

    # Verify function wrote the file
    greeting1_path = os.path.join(temp_dir, "greeting_World.txt")
    assert os.path.exists(greeting1_path), f"greeting_World.txt not found at {greeting1_path}"
    with open(greeting1_path, 'r') as f:
        content = f.read().strip()
        assert content == "Hello, World!", f"Wrong content: {content}"
    print(f"✓ Verified function wrote 'Hello, World!' to {greeting1_path}")

    # Verify function persists in next command by calling it again
    result2 = agent("Run: greet Testing")
    result2_str = str(result2)
    print(f"Function persistence result: {result2_str[:300]}")

    # Verify the persisted function wrote the new file
    greeting2_path = os.path.join(temp_dir, "greeting_Testing.txt")
    assert os.path.exists(greeting2_path), f"greeting_Testing.txt not found - function didn't persist"
    with open(greeting2_path, 'r') as f:
        content = f.read().strip()
        assert content == "Hello, Testing!", f"Wrong content: {content}"
    print(f"✓ Verified persisted function wrote 'Hello, Testing!' to {greeting2_path}")

    # Validate shell_tool was called with greet command
    assert_shell_tool_called(agent, expected_command_substring="greet")

    print("✓ Shell function persisted across commands")


def test_error_handling_and_recovery(temp_dir):
    """Test that agent can recover from errors and continue."""
    agent = Agent(tools=[shell_tool])
    print("\n--- Error Handling and Recovery Test ---")

    # Run a failing command that tries to write to a non-existent directory
    bad_path = "/this/does/not/exist/test.txt"
    result1 = agent(f"Run: echo 'test' > {bad_path}")
    result1_str = str(result1)
    print(f"Error result: {result1_str[:300]}")

    # Verify the file was NOT created (command failed)
    assert not os.path.exists(bad_path), f"File should not exist - command should have failed"
    print(f"✓ Verified command failed - no file created at {bad_path}")

    # Verify shell still works after error by creating a recovery file
    recovery_path = os.path.join(temp_dir, "recovery_success.txt")
    result2 = agent(f"Run: echo 'RECOVERED' > {recovery_path}")
    result2_str = str(result2)
    print(f"Recovery result: {result2_str[:300]}")

    # Verify recovery by checking the file exists and has correct content
    assert os.path.exists(recovery_path), f"Recovery failed - file not created at {recovery_path}"
    with open(recovery_path, 'r') as f:
        content = f.read().strip()
        assert content == "RECOVERED", f"Wrong content in recovery file: {content}"
    print(f"✓ Verified shell recovered - created file at {recovery_path} with correct content")

    # Validate shell_tool was called
    assert_shell_tool_called(agent, expected_command_substring="echo")

    print("✓ Successfully recovered from error")
