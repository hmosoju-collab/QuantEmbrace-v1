"""
Risk Engine for QuantEmbrace.

Sits between the Strategy Engine and Execution Engine. ALL signals must pass
through risk validation before becoming orders. This is a non-negotiable
architectural boundary — no signal bypasses risk checks.
"""
