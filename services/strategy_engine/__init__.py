"""
Strategy Engine for QuantEmbrace.

Runs trading strategies on incoming market data and generates signals.
Signals are forwarded to the Risk Engine for validation — strategy logic
NEVER directly triggers order execution.
"""
