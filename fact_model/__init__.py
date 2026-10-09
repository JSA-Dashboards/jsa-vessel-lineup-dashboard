"""Region-grouped line-up / executed fact model for the vessel dashboard.

dimensions  region/port/elevator mapping, commodity group, marketing year (config/*.csv)
lineup      SNAPSHOT math  - tonnage queued as of each report_date
executed    CUMULATIVE math - YTD running totals differenced into MTD / MYTD
store       SQLite persistence (facts.db)
"""
