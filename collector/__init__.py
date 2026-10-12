"""
Collector — 信息采集模块

自动采集海外紧固件客户信息，支持多数据源：
- 寻客星 (XunKeXing) B2B 平台
- Facebook 社媒
- 行业官网 (Industry sites)

所有采集器最终调用 LeadManager.create_batch() 入库。

Usage:
    python -m collector run --source=industry
    python -m collector run --all
    python -m collector run --source=xunkexing --dry-run
"""

__version__ = "0.1.0"
