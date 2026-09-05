"""
facts.py -- STAGE 6b. The anti-hallucination mechanism.

The model never decides whether it knows something. This does.

For each question the sender asked, we try to map it onto an entry in
approved_facts.yaml. If every question maps, fact_coverage = 1.0 and the
model is handed the approved wording. If even one does not map, coverage
drops and the router holds the message for a human.

Result: the model can only ever restate sanctioned language. It is
physically not given the option to invent an answer, because the answer
text is passed in with the prompt.
"""
import re, yaml
from typing import List, Dict, Tuple


class FactBook:
    def __init__(self, path="config/approved_facts.yaml"):
        data = yaml.safe_load(open(path))
        self.facts = data.get("facts", {})
        self.sendable_assets = data.get("sendable_assets", []) or []

    def match(self, question: str) -> Tuple[str, str]:
        """Return (fact_key, approved_text) or ('','')."""
        q = question.lower()
        best, best_score = None, 0
        for key, f in self.facts.items():
            score = 0
            for kw in f.get("keywords", []):
                if kw.lower() in q:
                    score += len(kw)          # longer keyword = stronger signal
            if score > best_score:
                best, best_score = key, score
        if best and best_score >= 4:
            return best, " ".join(self.facts[best]["text"].split())
        return "", ""

    def coverage(self, questions: List[str]) -> Tuple[float, List[Dict], List[str]]:
        """
        Returns (score 0-1, answered list, unanswered list).
        No questions asked => perfect coverage, nothing to get wrong.
        """
        if not questions:
            return 1.0, [], []
        answered, unanswered = [], []
        for q in questions:
            key, text = self.match(q)
            if text:
                answered.append({"q": q, "a": text, "key": key})
            else:
                unanswered.append(q)
        return round(len(answered) / len(questions), 2), answered, unanswered

    def as_block(self, answered: List[Dict]) -> str:
        return "\n".join(f"{a['key']}: {a['a']}" for a in answered) or "(none)"
