"""Tests for the categorization engine."""
import pytest
from pathlib import Path

from src.processing.categorizer import RuleMatcher, CategoryResult, CategorizationEngine


class TestRuleMatcher:
    """Tests for rule-based categorization."""

    def test_match_groceries(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("WHOLEFDS MKT #12345")
        assert result is not None
        assert result.category == "Groceries"
        assert result.source == "rule"

    def test_match_dining(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("UBER *EATS ORDER")
        assert result is not None
        assert result.category == "Dining"

    def test_match_subscriptions(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("NETFLIX.COM")
        assert result is not None
        assert result.category == "Subscriptions"

    def test_no_match_returns_none(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("RANDOM UNKNOWN MERCHANT")
        assert result is None

    def test_case_insensitive_matching(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("wholefds market")
        assert result is not None
        assert result.category == "Groceries"

    def test_merchant_clean_extraction(self, temp_rules_file):
        matcher = RuleMatcher(temp_rules_file)
        result = matcher.match("WHOLEFDS MKT #999")
        assert result.merchant_clean == "Whole Foods"

    def test_missing_rules_file(self):
        matcher = RuleMatcher(Path("/nonexistent/rules.yaml"))
        assert matcher.rules == []
        result = matcher.match("ANYTHING")
        assert result is None


class TestCategorizationEngine:
    """Tests for the hybrid categorization engine."""

    def test_rule_match_first(self, temp_rules_file):
        engine = CategorizationEngine()
        engine.rule_matcher = RuleMatcher(temp_rules_file)
        result = engine.categorize("WHOLEFDS MKT", use_llm=False)
        assert result.category == "Groceries"
        assert result.source == "rule"

    def test_uncategorized_without_llm(self, temp_rules_file):
        engine = CategorizationEngine()
        engine.rule_matcher = RuleMatcher(temp_rules_file)
        result = engine.categorize("UNKNOWN MERCHANT XYZ", use_llm=False)
        assert result.category == "Uncategorized"
        assert result.source == "default"

    def test_learned_rules_cached(self, temp_rules_file):
        engine = CategorizationEngine()
        engine.rule_matcher = RuleMatcher(temp_rules_file)
        # Manually add a learned rule
        engine.learned_rules["CUSTOM MERCHANT"] = CategoryResult(
            category="Shopping", merchant_clean="Custom", source="llm"
        )
        result = engine.categorize("CUSTOM MERCHANT", use_llm=False)
        assert result.category == "Shopping"
        assert result.source == "llm"

    def test_amount_passed_to_categorizer(self, temp_rules_file):
        engine = CategorizationEngine()
        engine.rule_matcher = RuleMatcher(temp_rules_file)
        # Amount shouldn't affect rule matching
        result = engine.categorize("NETFLIX", amount=-15.99, use_llm=False)
        assert result.category == "Subscriptions"


class TestCategoryResult:
    """Tests for CategoryResult dataclass."""

    def test_default_values(self):
        result = CategoryResult(category="Test")
        assert result.merchant_clean is None
        assert result.source == "rule"
        assert result.confidence == 1.0

    def test_custom_values(self):
        result = CategoryResult(
            category="Dining",
            merchant_clean="Restaurant",
            source="llm",
            confidence=0.85
        )
        assert result.category == "Dining"
        assert result.merchant_clean == "Restaurant"
        assert result.source == "llm"
        assert result.confidence == 0.85
