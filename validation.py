import json
from typing import Any, List, Dict, Union
from models import ProtocolSchema, ComparisonOperator

class ValidationEngine:
    def __init__(self, rules: "Union[str, ProtocolSchema]"):
        # Accept either a path to a rules JSON file (read from disk) or an already-loaded
        # ProtocolSchema (used by the Master orchestrator, which holds the Redis-cached protocol).
        # In plain English: you can hand this either a filename or the protocol object itself.
        if isinstance(rules, ProtocolSchema):
            self.rules = rules
        else:
            with open(rules, 'r') as file:
                # Re-hydrates the saved file directly using your ProtocolSchema structure
                self.rules = ProtocolSchema.model_validate_json(file.read())

        # Functional dispatcher mapping every single one of your custom operators
        self.operators = {
            ComparisonOperator.GT: lambda dataVal, ruleLimit: float(dataVal) > float(ruleLimit),
            ComparisonOperator.GTE: lambda dataVal, ruleLimit: float(dataVal) >= float(ruleLimit),
            ComparisonOperator.LT: lambda dataVal, ruleLimit: float(dataVal) < float(ruleLimit),
            ComparisonOperator.LTE: lambda dataVal, ruleLimit: float(dataVal) <= float(ruleLimit),
            ComparisonOperator.EQ: lambda dataVal, ruleLimit: str(dataVal) == str(ruleLimit),
            ComparisonOperator.NOT_EQ: lambda dataVal, ruleLimit: str(dataVal) != str(ruleLimit),
            ComparisonOperator.CONTAINS: lambda dataVal, ruleLimit: str(ruleLimit) in str(dataVal),
            ComparisonOperator.NO_CONTAINS: lambda dataVal, ruleLimit: str(ruleLimit) not in str(dataVal),
        }

    def validateAction(self, newActionId: str, activeStack: List[str]) -> tuple:
        """Evaluates O(1) compound conflicts using your incompatibilities matrix mapping."""
        conflicts = self.rules.incompatibilities.get(newActionId, [])
        for item in conflicts:
            if item in activeStack:
                return False, f"Conflict detected: {newActionId} cannot be used with {item}"
        return True, "Valid"

    def checkThreshold(self, metricKey: str, value: Any) -> tuple:
        """Scans your thresholds list sequentially to verify structural state constraints."""
        for threshold in self.rules.thresholds:
            if threshold.metricKey == metricKey:
                operator = ComparisonOperator(threshold.operator)
                isBreached = self.operators[operator](value, threshold.limit)
                if isBreached:
                    return False, threshold.errorMessage
        return True, "Within limits"
    
    