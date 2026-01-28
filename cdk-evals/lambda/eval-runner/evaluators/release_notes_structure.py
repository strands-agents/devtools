"""Evaluator to validate release notes structure and formatting."""

import re
from strands_evals.evaluators import Evaluator
from strands_evals.types import EvaluationData, EvaluationOutput


class ReleaseNotesStructureEvaluator(Evaluator[str, str]):
    """
    Evaluates whether release notes follow the expected structure and format.
    
    Checks:
    - Has "Major Features" section
    - Has PR links in correct format
    - Has proper markdown formatting
    - Code blocks are properly fenced
    """
    
    def __init__(self, threshold: float = 0.7):
        """
        Initialize the evaluator.
        
        Args:
            threshold: Score threshold for passing (default 0.7)
        """
        super().__init__()
        self.threshold = threshold
    
    def _check_major_features_section(self, text: str) -> tuple[bool, str]:
        """Check for Major Features section."""
        patterns = [
            r'##\s*Major Features',
            r'###\s*Major Features',
            r'\*\*Major Features\*\*',
        ]
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True, "Major Features section found"
        return False, "Missing Major Features section"
    
    def _check_pr_links(self, text: str) -> tuple[float, str]:
        """Check for proper PR link formatting."""
        # Pattern for PR links like [PR#1234](url) or PR#1234
        pr_patterns = [
            r'\[PR#\d+\]\(https?://[^\)]+\)',  # Full markdown link
            r'\*\*PR#\d+\*\*',  # Bold PR number
            r'PR#\d+',  # Just PR number
            r'\[#\d+\]\(https?://[^\)]+\)',  # [#123](url) format
        ]
        
        pr_count = 0
        linked_count = 0
        
        for pattern in pr_patterns:
            matches = re.findall(pattern, text)
            pr_count += len(matches)
            if 'http' in pattern:
                linked_count += len(matches)
        
        if pr_count == 0:
            return 0.0, "No PR references found"
        
        link_ratio = linked_count / pr_count if pr_count > 0 else 0
        
        if link_ratio >= 0.5:
            return 1.0, f"Found {pr_count} PR references, {linked_count} with links"
        elif pr_count > 0:
            return 0.7, f"Found {pr_count} PR references but only {linked_count} have links"
        return 0.0, "No PR references found"
    
    def _check_code_fencing(self, text: str) -> tuple[float, str]:
        """Check for proper code block formatting."""
        # Count properly fenced code blocks
        fenced_blocks = len(re.findall(r'```(?:python)?\s*\n.*?```', text, re.DOTALL))
        
        # Count inline code that might be unfenced examples
        potential_code = len(re.findall(r'\n\s{4,}(?:from|import|def|class|@)', text))
        
        if fenced_blocks > 0:
            if potential_code == 0:
                return 1.0, f"{fenced_blocks} properly fenced code blocks"
            else:
                return 0.8, f"{fenced_blocks} fenced blocks, but {potential_code} potential unfenced code"
        elif potential_code > 0:
            return 0.3, f"No fenced code blocks, but found {potential_code} potential code snippets"
        return 0.5, "No code blocks found (may be expected for bug-fix-only notes)"
    
    def _check_markdown_headers(self, text: str) -> tuple[float, str]:
        """Check for proper markdown header structure."""
        h1_count = len(re.findall(r'^#\s+', text, re.MULTILINE))
        h2_count = len(re.findall(r'^##\s+', text, re.MULTILINE))
        h3_count = len(re.findall(r'^###\s+', text, re.MULTILINE))
        
        total_headers = h1_count + h2_count + h3_count
        
        if total_headers >= 3:
            return 1.0, f"Good header structure: {h1_count} H1, {h2_count} H2, {h3_count} H3"
        elif total_headers >= 1:
            return 0.7, f"Minimal headers: {total_headers} total"
        return 0.3, "No markdown headers found"
    
    def _check_bug_fixes_section(self, text: str) -> tuple[bool, str]:
        """Check for Bug Fixes section (optional but good to have)."""
        patterns = [
            r'##\s*Major Bug Fixes',
            r'###\s*Major Bug Fixes',
            r'##\s*Bug Fixes',
            r'\*\*Major Bug Fixes\*\*',
        ]
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True, "Bug Fixes section found"
        return False, "No Bug Fixes section (may be expected)"
    
    def evaluate(
        self, evaluation_case: EvaluationData[str, str]
    ) -> list[EvaluationOutput]:
        """
        Evaluate the structure and formatting of release notes.
        
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
        
        # Run all checks
        features_ok, features_msg = self._check_major_features_section(output)
        pr_score, pr_msg = self._check_pr_links(output)
        code_score, code_msg = self._check_code_fencing(output)
        header_score, header_msg = self._check_markdown_headers(output)
        bugfix_ok, bugfix_msg = self._check_bug_fixes_section(output)
        
        # Calculate weighted score
        # Major Features section is critical (30%)
        # PR links important (25%)
        # Code formatting important (20%)
        # Headers matter (15%)
        # Bug fixes section optional (10%)
        
        final_score = (
            (1.0 if features_ok else 0.0) * 0.30 +
            pr_score * 0.25 +
            code_score * 0.20 +
            header_score * 0.15 +
            (1.0 if bugfix_ok else 0.5) * 0.10  # Partial credit if missing
        )
        
        # Build reason
        checks = [
            f"Features: {features_msg}",
            f"PRs: {pr_msg}",
            f"Code: {code_msg}",
            f"Headers: {header_msg}",
            f"BugFixes: {bugfix_msg}",
        ]
        reason = " | ".join(checks)
        
        return [EvaluationOutput(
            score=final_score,
            test_pass=final_score >= self.threshold,
            reason=reason
        )]
