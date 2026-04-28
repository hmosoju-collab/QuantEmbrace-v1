"""
Data Ingestion Service for QuantEmbrace.

Connects to Zerodha Kite (NSE India) and Alpaca (US Equities) market data
feeds via WebSocket, normalizes tick data, and stores it in S3 (historical)
and DynamoDB (latest prices).
"""
