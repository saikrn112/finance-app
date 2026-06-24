"""Categorization engine: Rules + Gemini LLM fallback."""
import re
import yaml
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

@dataclass
class CategoryResult:
    category: str
    merchant_clean: Optional[str] = None
    source: str = "rule"  # rule, llm, user
    confidence: float = 1.0


class RuleMatcher:
    """Fast regex-based categorization."""

    def __init__(self, rules_path: Path = Path("rules/categories.yaml")):
        self.rules = []
        # Load from plugins/rules/categories.yaml first if it exists (user-specific),
        # otherwise fall back to the default rules/categories.yaml
        import os
        env_plugins_dir = os.environ.get("FINANCE_PLUGINS_DIR")
        plugin_rules_path = Path(env_plugins_dir) / "rules" / "categories.yaml" if env_plugins_dir else Path("plugins/rules/categories.yaml")
        effective_path = plugin_rules_path if plugin_rules_path.exists() else rules_path
        if effective_path.exists():
            with open(effective_path) as f:
                data = yaml.safe_load(f)
                for rule in data.get("rules", []):
                    self.rules.append({
                        "pattern": re.compile(rule["pattern"], re.IGNORECASE),
                        "category": rule["category"],
                        "merchant_clean": rule.get("merchant_clean"),
                    })
    
    def match(self, merchant_raw: str) -> Optional[CategoryResult]:
        """Try to match merchant against rules."""
        for rule in self.rules:
            if rule["pattern"].search(merchant_raw):
                return CategoryResult(
                    category=rule["category"],
                    merchant_clean=rule.get("merchant_clean"),
                    source="rule",
                )
        return None


class GeminiCategorizer:
    """LLM-based categorization for unknown merchants."""
    
    CATEGORIES = [
        "Housing", "Transportation", "Groceries", "Dining", "Shopping",
        "Entertainment", "Health", "Travel", "Subscriptions", "Utilities",
        "Insurance", "Personal Care", "Education", "Gifts & Donations",
        "Fees & Interest", "Salary/Paycheck", "Refunds", "Interest",
        "Other Income", "Investment", "Savings",
        "Internal Transfer", "Remittance", "Uncategorized"
    ]
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._model = None
    
    @property
    def model(self):
        if self._model is None and self.api_key:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            self._model = genai.GenerativeModel("gemini-1.5-flash")
        return self._model
    
    def categorize(self, merchant_raw: str, amount: float) -> Optional[CategoryResult]:
        """Use Gemini to categorize unknown merchant."""
        if not self.model:
            return None
        
        prompt = f"""Categorize this transaction. Return ONLY a JSON object.

Merchant: {merchant_raw}
Amount: ${abs(amount):.2f}

Categories: {', '.join(self.CATEGORIES)}

Return format:
{{"category": "CategoryName", "merchant_clean": "Clean Merchant Name"}}"""

        try:
            response = self.model.generate_content(prompt)
            text = response.text.strip()
            # Extract JSON from response
            import json
            if "{" in text:
                json_str = text[text.index("{"):text.rindex("}")+1]
                data = json.loads(json_str)
                return CategoryResult(
                    category=data.get("category", "Uncategorized"),
                    merchant_clean=data.get("merchant_clean"),
                    source="llm",
                    confidence=0.8,
                )
        except Exception:
            pass
        return None


class CategorizationEngine:
    """Hybrid categorizer: rules first, LLM fallback."""
    
    def __init__(self, gemini_api_key: str = ""):
        self.rule_matcher = RuleMatcher()
        self.llm = GeminiCategorizer(gemini_api_key)
        self.learned_rules: dict[str, CategoryResult] = {}
    
    def categorize(self, merchant_raw: str, amount: float = 0, use_llm: bool = True) -> CategoryResult:
        # Check learned rules first
        if merchant_raw in self.learned_rules:
            return self.learned_rules[merchant_raw]
        
        # Try rule matcher
        result = self.rule_matcher.match(merchant_raw)
        if result:
            return result
        
        # Try LLM
        if use_llm:
            result = self.llm.categorize(merchant_raw, amount)
            if result:
                # Learn for future
                self.learned_rules[merchant_raw] = result
                return result
        
        return CategoryResult(category="Uncategorized", source="default")
