import os
import subprocess
import time
import threading
import weakref
from strands import tool
from strands.types.tools import ToolContext


# Module-level session registry with automatic cleanup when agents are GC'd
_sessions = weakref.WeakKeyDictionary()


class ShellSession:
    """Manages a persistent shell process using plain pipes.

    Architecture:
    - One long-lived shell process per session
    - stderr merged into stdout for simplified stream handling
    - Single long-lived reader thread (not per-command threads)
    - Binary mode with manual decode to avoid text buffering issues
    - Buffer offset tracking for clean per-command output extraction
    - Single-flight execution with lock to prevent command interleaving
    """

    def __init__(self, timeout: int = 30):
        self._timeout = timeout
        self._process = None
        self._alive = False

        # Single-flight execution lock
        self._run_lock = threading.Lock()

        # Shared output buffer with synchronization
        self._output_buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._buffer_condition = threading.Condition(self._buffer_lock)

        # Reader thread
        self._reader_thread = None
        self._stop_reader = False

        self._start_process()
    
    def __del__(self):
        """Ensure OS processes and threads are cleaned up if the object is garbage collected."""
        try:
            self.stop()
        except Exception:
            pass

    def _start_process(self):
        """Start the shell process with clean configuration."""
        # default to bash
        shell = os.environ.get("SHELL", "/bin/bash")

        # Configure shell for clean startup (no rc files)
        if shell.endswith("bash"):
            argv = [shell, "--noprofile", "--norc"]
        elif shell.endswith("zsh"):
            argv = [shell, "-f"]
        else:
            argv = [shell]

        # Start process with merged stderr->stdout, binary mode
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            env={**os.environ, "PS1": "", "PS2": "", "PROMPT": ""},
        )

        self._alive = True
        self._stop_reader = False

        # Start long-lived reader thread
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self):
        """Long-lived reader thread that continuously reads from stdout.

        This runs for the entire lifetime of the shell process, not per-command.
        Reads fixed-size chunks (not readline!) to avoid blocking on newlines.

        Note: os.read() will block until data is available, which is fine for a
        daemon thread. This approach is simpler and cross-platform (Windows compatible).
        We avoid select() which doesn't work with file descriptors on Windows.
        """
        READ_CHUNK_SIZE = 4096
        try:
            fd = self._process.stdout.fileno()
            while not self._stop_reader and self._process and self._process.poll() is None:
                # Block until data is available (or EOF)
                # This is safe in a daemon thread and works on all platforms
                chunk = os.read(fd, READ_CHUNK_SIZE)
                if not chunk:
                    # EOF - process died
                    break

                # Append to shared buffer and notify waiters
                with self._buffer_condition:
                    self._output_buffer.extend(chunk)
                    self._buffer_condition.notify_all()
        except Exception:
            # Process died or other error
            pass
        finally:
            with self._buffer_condition:
                self._alive = False
                self._buffer_condition.notify_all()

    def run(self, command: str, timeout: int | None = None) -> str:
        """Execute a command in the persistent session.

        Args:
            command: The command to execute
            timeout: Optional timeout in seconds

        Returns:
            Command output with exit code appended if non-zero
        """
        # Single-flight execution - only one command at a time
        with self._run_lock:
            if not self._alive or not self._process or self._process.poll() is not None:
                raise Exception("Shell session is not running")

            effective_timeout = timeout if timeout is not None else self._timeout

            # Generate unique sentinel hash
            hash = f"{time.time_ns()}_{os.urandom(4).hex()}"
            sentinel = f"__CMD_DONE__:{hash}:"

            # Record buffer position before command
            with self._buffer_lock:
                start_offset = len(self._output_buffer)

            # Write command with sentinel
            try:
                wrapped_command = f"{command}\n__EXIT_CODE=$?\nprintf '\\n{sentinel}%s\\n' \"$__EXIT_CODE\"\n"
                self._process.stdin.write(wrapped_command.encode('utf-8'))
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._alive = False
                raise Exception(f"Failed to write to shell: {e}")

            # Wait for sentinel with timeout
            deadline = time.time() + effective_timeout
            sentinel_bytes = sentinel.encode('utf-8')

            while True:
                with self._buffer_condition:
                    # Check if sentinel appeared after start_offset
                    buffer_view = bytes(self._output_buffer[start_offset:])
                    if sentinel_bytes in buffer_view:
                        # Found sentinel. Extract output
                        output = buffer_view.decode('utf-8', errors='replace')
                        break

                    # Check timeout
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        # Timeout - kill session (not trustworthy after timeout)
                        self.stop()
                        raise TimeoutError(f"Command timed out after {effective_timeout} seconds")

                    # Check if session died
                    if not self._alive:
                        raise Exception("Shell process died unexpectedly")

                    # Wait for more output
                    self._buffer_condition.wait(timeout=min(remaining, 0.1))

            # Prune the buffer to prevent memory leaks
            # This is critical for long-lived sessions with many commands
            with self._buffer_lock:
                # Find the end of the sentinel line to safely truncate
                sentinel_idx = self._output_buffer.find(sentinel_bytes, start_offset)
                if sentinel_idx != -1:
                    # Find the newline after the sentinel
                    nl_idx = self._output_buffer.find(b'\n', sentinel_idx)
                    if nl_idx != -1:
                        # Delete everything up to and including the sentinel line
                        del self._output_buffer[:nl_idx + 1]
                    else:
                        # No newline found, just delete up to end of sentinel
                        del self._output_buffer[:sentinel_idx + len(sentinel_bytes)]

            # Parse output and extract exit code
            exit_code = -1
            lines = output.split('\n')
            filtered_lines = []

            for line in lines:
                if sentinel in line:
                    # Extract exit code from sentinel line
                    parts = line.split(':')
                    if len(parts) >= 3:
                        try:
                            exit_code = int(parts[2])
                        except ValueError:
                            pass
                    # Don't include sentinel line in output
                    continue
                filtered_lines.append(line)

            output = '\n'.join(filtered_lines).strip()

            # Append exit code if non-zero
            if exit_code != 0:
                output += f"\n\nExit code: {exit_code}"

            return output

    def stop(self):
        """Stop the shell process and reader thread."""
        self._stop_reader = True
        self._alive = False

        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1)

    def restart(self):
        """Restart the shell session."""
        self.stop()
        self._output_buffer.clear()
        self._start_process()


@tool(context=True)
def shell_tool(
    command: str,
    timeout: int | None = None,
    restart: bool = False,
    tool_context: ToolContext = None
) -> str:
    """
    Execute a shell command in a persistent shell session.

    The shell session preserves state across commands:
    - Working directory (cd persists)
    - Exported environment variables
    - Shell variables
    - Sourced shell state

    Uses the system default shell ($SHELL, defaulting to /bin/bash) with clean
    startup configuration (--noprofile --norc for bash, -f for zsh).

    **Supported commands:**
    - Standard shell commands
    - Build/test commands
    - Shell pipelines and normal non-interactive commands

    **Unsupported/unreliable:**
    - Interactive programs: vim, less, top, nano
    - REPLs: python, node, irb
    - Password prompts or TTY-required programs
    - Full-screen TUIs
    - Background jobs that continue writing after command returns

    Args:
        command: The shell command to execute
        timeout: Optional timeout in seconds (default: 30)
        restart: If True, restart the shell session before running the command

    Returns:
        The command output, with exit code appended if non-zero
    """
    agent = tool_context.agent

    # Handle restart without command - just recreate session and return
    if restart and (not command or command.strip() == ""):
        if agent in _sessions:
            _sessions[agent].stop()
        _sessions[agent] = ShellSession()
        return "Shell session restarted"

    # Handle restart with command - stop old session and create fresh one
    if restart:
        if agent in _sessions:
            _sessions[agent].stop()
        _sessions[agent] = ShellSession()

    # Get or create session (normal case)
    if agent not in _sessions:
        _sessions[agent] = ShellSession()

    session = _sessions[agent]

    try:
        return session.run(command, timeout=timeout)
    except TimeoutError as e:
        # Session is dead after timeout, recreate on next call
        return f"Error: {str(e)}"
    except Exception as e:
        # Only restart if process actually died
        if session._process is None or session._process.poll() is not None:
            session.stop()
            _sessions[agent] = ShellSession()
        return f"Error: {str(e)}"
