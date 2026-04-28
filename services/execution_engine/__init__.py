"""
Execution Engine for QuantEmbrace.

Translates risk-approved signals into broker orders and manages the full
order lifecycle. Routes orders to the correct broker: Zerodha for NSE
instruments, Alpaca for US equities.

CRITICAL: This service ONLY receives signals that have been approved by
the Risk Engine. Direct strategy-to-execution communication is prohibited.
"""
