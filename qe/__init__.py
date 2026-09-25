"""QuantEmbrace v2 core — one deterministic engine, three clocks (ADR-037).

Design: architecture/re-architecture-2026-07.md. This package never touches
live/paper trading state; the v1 stack remains authoritative until M5 cutover.
"""

__version__ = "2.0.0a1"
