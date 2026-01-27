"""Evaluator to validate Python code syntax in release notes."""

import ast
import re
from strands_evals.evaluators import Evaluator
from strands_evals.types import EvaluationData, EvaluationOutput


class CodeSyntaxValidityEvaluator(Evaluator[str, str]):
    """
    Evaluates whether code examples in release notes are syntactically valid Python.
    
    Checks:
    - All code blocks parse with ast.parse()
    - Imports reference real modules (strands.*, common stdlib)
    - Code blocks are not empty or trivial
    """
    
    # Known valid import prefixes for strands SDK
    VALID_STRANDS_IMPORTS = [
        "strands",
        "strands.agent",
        "strands.hooks",
        "strands.models",
        "strands.tools",
        "strands.types",
        "strands.multiagent",
        "strands.experimental",
        "strands.session",
    ]
    
    # Common standard library modules
    STDLIB_MODULES = {
        "os", "sys", "json", "typing", "datetime", "functools",
        "collections", "itertools", "re", "pathlib", "asyncio",
        "unittest", "pytest", "mock", "dataclasses", "enum",
    }
    
    # Third-party modules commonly used with strands
    THIRD_PARTY_MODULES = {
        "httpx", "openai", "google", "google.genai",
        "fastapi", "starlette", "pydantic", "boto3", "botocore",
    }
    
    def __init__(self, threshold: float = 0.8):
        """
        Initialize the evaluator.
        
        Args:
            threshold: Score threshold for passing (default 0.8)
        """
        super().__init__()
        self.threshold = threshold
    
    def _extract_code_blocks(self, text: str) -> list[str]:
        """Extract Python code blocks from markdown text."""
        # Match ```python ... ``` or ``` ... ``` blocks
        pattern = r'```(?:python)?\s*\n(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        return [m.strip() for m in matches if m.strip()]
    
    def _validate_syntax(self, code: str) -> tuple[bool, str]:
        """
        Validate Python syntax using ast.parse().
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            ast.parse(code)
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError at line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Parse error: {str(e)}"
    
    def _extract_imports(self, code: str) -> list[str]:
        """Extract import module names from code."""
        imports = []
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module.split('.')[0])
        except:
            pass
        return imports
    
    def _validate_imports(self, imports: list[str]) -> tuple[bool, list[str]]:
        """
        Check if imports reference known valid modules.
        
        Returns:
            Tuple of (all_valid, list_of_unknown_imports)
        """
        unknown = []
        for imp in imports:
            is_known = (
                imp in self.STDLIB_MODULES or
                imp in self.THIRD_PARTY_MODULES or
                any(imp.startswith(prefix.split('.')[0]) for prefix in self.VALID_STRANDS_IMPORTS)
            )
            if not is_known:
                unknown.append(imp)
        return len(unknown) == 0, unknown
    
    def evaluate(
        self, evaluation_case: EvaluationData[str, str]
    ) -> list[EvaluationOutput]:
        """
        Evaluate the syntax validity of code blocks in the output.
        
        Args:
            evaluation_case: The evaluation data containing actual_output
            
        Returns:
            List containing single EvaluationOutput
        """
        output = evaluation_case.actual_output or ""
        
        if not output:
            return [EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason="No output to evaluate"
            )]
        
        # Extract code blocks
        code_blocks = self._extract_code_blocks(output)
        
        if not code_blocks:
            # No code blocks - could be intentional for bug fixes section
            # Give neutral score if expected_output mentions no code needed
            expected = evaluation_case.expected_output or ""
            if "no code" in expected.lower() or "bug fix" in expected.lower():
                return [EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="No code blocks found, but none expected for this content type"
                )]
            return [EvaluationOutput(
                score=0.5,
                test_pass=False,
                reason="No Python code blocks found in release notes"
            )]
        
        # Validate each code block
        results = []
        total_score = 0.0
        
        for i, code in enumerate(code_blocks):
            # Check syntax
            syntax_valid, syntax_error = self._validate_syntax(code)
            
            if not syntax_valid:
                results.append(f"Block {i+1}: INVALID - {syntax_error}")
                continue
            
            # Check imports
            imports = self._extract_imports(code)
            imports_valid, unknown = self._validate_imports(imports)
            
            if not imports_valid:
                results.append(f"Block {i+1}: Unknown imports: {unknown}")
                total_score += 0.7  # Partial credit for valid syntax
            else:
                results.append(f"Block {i+1}: VALID")
                total_score += 1.0
        
        # Calculate final score
        final_score = total_score / len(code_blocks) if code_blocks else 0.0
        
        reason = f"Checked {len(code_blocks)} code blocks. " + "; ".join(results)
        
        return [EvaluationOutput(
            score=final_score,
            test_pass=final_score >= self.threshold,
            reason=reason
        )]
