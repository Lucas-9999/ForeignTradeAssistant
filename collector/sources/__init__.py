"""
Collector Sources — individual data source implementations.

Each source is a module with a Collector class that inherits from BaseCollector.
Sources are auto-registered via the SOURCE_REGISTRY in collector.cli.

Built-in sources:
- industry: Industry website / directory scrapers
- xunkexing: 寻客星 B2B platform
- facebook: Facebook pages & groups (Phase 2)
"""
