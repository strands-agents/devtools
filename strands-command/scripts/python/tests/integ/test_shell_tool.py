"""Integration tests for shell_tool with Strands Agent."""
import pytest
from strands import Agent
from ...shell_tool import shell_tool


@pytest.fixture
def agent():
    """Create an agent with shell_tool configured."""
    return Agent(tools=[shell_tool])


def test_basic_execution(agent):
    """Test basic shell command execution through agent."""
    result = agent("Run: echo 'hello'")
    result_str = str(result)
    print(f"\nBasic execution result: {result_str[:300]}")
    assert result is not None
    assert "hello" in result_str.lower()


def test_complex_multi_step_workflow(agent):
    """Test a complex multi-step workflow with state management."""
    print("\n--- Complex Multi-Step Workflow Test ---")

    # Step 1: Create temporary directory structure
    result1 = agent("Create a temp directory at /tmp/shell_test_$$ and cd into it, then confirm with pwd")
    result1_str = str(result1)
    print(f"Step 1 - Create temp dir: {result1_str[:300]}")
    assert "shell_test" in result1_str, f"Failed to create/cd to temp dir: {result1_str}"

    # Step 2: Create files with shell loops
    result2 = agent("""In the current directory, run: for i in 1 2 3; do echo "Line $i" > file$i.txt; done
Then list the files to confirm they were created.""")
    result2_str = str(result2)
    print(f"Step 2 - Create files: {result2_str[:300]}")
    assert "file1.txt" in result2_str and "file2.txt" in result2_str, f"Files not created: {result2_str}"

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
    assert "shell_test" in result5_str, f"Lost directory context: {result5_str}"

    # Cleanup
    agent("Run: cd /tmp && rm -rf shell_test_*")
    print("✓ All steps completed successfully")


def test_shell_functions_and_persistence(agent):
    """Test that shell functions persist across commands."""
    print("\n--- Shell Functions Persistence Test ---")

    # Define a shell function
    result1 = agent("""Run: greet() { echo "Hello, $1!"; }
Then test it: greet World""")
    result1_str = str(result1)
    print(f"Function definition result: {result1_str[:300]}")
    assert "Hello, World!" in result1_str, f"Function execution failed: {result1_str}"

    # Verify function persists in next command
    result2 = agent("Run: greet Testing")
    result2_str = str(result2)
    print(f"Function persistence result: {result2_str[:300]}")
    assert "Hello, Testing!" in result2_str, f"Function did not persist: {result2_str}"

    print("✓ Shell function persisted across commands")


def test_error_handling_and_recovery(agent):
    """Test that agent can recover from errors and continue."""
    print("\n--- Error Handling and Recovery Test ---")

    # Run a failing command
    result1 = agent("Run: ls /this/does/not/exist")
    result1_str = str(result1)
    print(f"Error result: {result1_str[:300]}")
    # Should contain error info - agent may summarize or show raw output
    assert ("Exit code:" in result1_str or "cannot" in result1_str.lower() or
            "no such" in result1_str.lower() or "error" in result1_str.lower() or
            "fail" in result1_str.lower()), \
        f"Expected error output: {result1_str}"

    # Verify shell still works after error - use a unique string
    result2 = agent("Run this command and show me the exact output: echo 'RECOVERY_TEST_SUCCESS_12345'")
    result2_str = str(result2)
    print(f"Recovery result: {result2_str[:300]}")
    # Check for the unique string or agent confirmation
    assert ("RECOVERY_TEST_SUCCESS_12345" in result2_str or
            "success" in result2_str.lower() or
            "working" in result2_str.lower()), \
        f"Failed to recover from error: {result2_str}"

    print("✓ Successfully recovered from error")
