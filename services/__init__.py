"""
QuantEmbrace - A Hedge Level Algo Trading System

Services package for algorithmic trading across NSE India (Zerodha Kite Connect)
and US Equities (Alpaca). All services are designed to run on AWS ECS Fargate.

Architecture:
    Data Ingestion -> Strategy Engine -> Risk Engine -> Execution Engine
                                          ^
                                          |
                                      AI Engine (optional signal enrichment)

Critical invariants:
    - Strategy logic, execution logic, and risk logic are NEVER mixed
    - Risk engine sits between strategy and execution
    - ALL trades must pass risk validation before execution
    - System is restart-safe and idempotent
"""

__version__ = "0.1.0"
__app_name__ = "QuantEmbrace"
