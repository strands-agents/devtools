"""Evaluator to detect AI-typical writing patterns in release notes."""

from strands_evals.evaluators import Evaluator
from strands_evals.types import EvaluationData, EvaluationOutput


class NaturalWritingEvaluator(Evaluator[str, str]):
    """
    Evaluates whether release notes read naturally and don't exhibit
    obvious AI-generated patterns.
    
    Checks for:
    - Excessive hedging language ("I think", "It seems", "perhaps")
    - Overly formal/verbose phrasing
    - Repetitive sentence structures
    - Unnecessary meta-commentary about the content
    """
    
    # Common AI hedging patterns
    HEDGING_PATTERNS = [
        "i think",
        "it seems",
        "perhaps",
        "it appears",
        "it's worth noting",
        "it should be noted",
        "importantly",
        "notably",
        "interestingly",
        "essentially",
        "basically",
        "in essence",
        "as mentioned",
        "as previously mentioned",
        "to summarize",
        "in summary",
        "in conclusion",
        "overall",
        "generally speaking",
    ]
    
    # AI meta-commentary patterns
    META_COMMENTARY_PATTERNS = [
        "here is",
        "here are",
        "below is",
        "below are",
        "the following",
        "as requested",
        "as you asked",
        "i've",
        "i have",
        "i will",
        "let me",
        "i'd be happy to",
        "i'm happy to",
        "certainly",
        "absolutely",
        "of course",
    ]
    
    # Overly formal/verbose phrases
    VERBOSE_PATTERNS = [
        "in order to",
        "for the purpose of",
        "with respect to",
        "in regard to",
        "in regards to",
        "pertaining to",
        "utilize",  # instead of "use"
        "leverage",  # overused
        "facilitate",
        "endeavor",
        "aforementioned",
    ]
    
    def __init__(
        self,
        hedging_weight: float = 0.4,
        meta_weight: float = 0.3,
        verbose_weight: float = 0.3,
        threshold: float = 0.7,
    ):
        """
        Initialize the evaluator.
        
        Args:
            hedging_weight: Weight for hedging pattern detection
            meta_weight: Weight for meta-commentary pattern detection
            verbose_weight: Weight for verbose pattern detection
            threshold: Score threshold for passing (default 0.7)
        """
        super().__init__()
        self.hedging_weight = hedging_weight
        self.meta_weight = meta_weight
        self.verbose_weight = verbose_weight
        self.threshold = threshold
    
    def _count_pattern_matches(self, text: str, patterns: list[str]) -> int:
        """Count how many patterns appear in the text."""
        text_lower = text.lower()
        return sum(1 for pattern in patterns if pattern in text_lower)
    
    def _calculate_pattern_score(
        self, 
        match_count: int, 
        max_acceptable: int = 2
    ) -> float:
        """
        Calculate score based on pattern matches.
        
        Returns 1.0 if no matches, decreasing to 0.0 as matches increase.
        """
        if match_count == 0:
            return 1.0
        elif match_count <= max_acceptable:
            return 1.0 - (match_count / (max_acceptable * 2))
        else:
            return max(0.0, 0.5 - (match_count - max_acceptable) * 0.1)
    
    def evaluate(
        self, evaluation_case: EvaluationData[str, str]
    ) -> list[EvaluationOutput]:
        """
        Evaluate the naturalness of the output text.
        
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
        
        # Count pattern matches
        hedging_count = self._count_pattern_matches(output, self.HEDGING_PATTERNS)
        meta_count = self._count_pattern_matches(output, self.META_COMMENTARY_PATTERNS)
        verbose_count = self._count_pattern_matches(output, self.VERBOSE_PATTERNS)
        
        # Calculate individual scores
        hedging_score = self._calculate_pattern_score(hedging_count, max_acceptable=1)
        meta_score = self._calculate_pattern_score(meta_count, max_acceptable=1)
        verbose_score = self._calculate_pattern_score(verbose_count, max_acceptable=2)
        
        # Weighted average
        final_score = (
            hedging_score * self.hedging_weight +
            meta_score * self.meta_weight +
            verbose_score * self.verbose_weight
        )
        
        # Build reason string
        issues = []
        if hedging_count > 0:
            issues.append(f"{hedging_count} hedging patterns")
        if meta_count > 0:
            issues.append(f"{meta_count} meta-commentary patterns")
        if verbose_count > 0:
            issues.append(f"{verbose_count} verbose patterns")
        
        if issues:
            reason = f"Found: {', '.join(issues)}. Score breakdown: hedging={hedging_score:.2f}, meta={meta_score:.2f}, verbose={verbose_score:.2f}"
        else:
            reason = "No AI-typical patterns detected. Text reads naturally."
        
        return [EvaluationOutput(
            score=final_score,
            test_pass=final_score >= self.threshold,
            reason=reason
        )]
