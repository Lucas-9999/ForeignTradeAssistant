"""CLI audit logging must persist INFO-level authorization decisions."""

import logging


def test_cli_logging_persists_info_records_to_rotating_log(tmp_path):
    from utils.cli_logging import configure_cli_logging

    handler = configure_cli_logging(log_dir=tmp_path)
    logger = logging.getLogger("tests.cli_logging")
    try:
        logger.info("delivery_authorization_decision %s", '{"decision":"allow"}')
        handler.flush()
        text = (tmp_path / "fta.log").read_text(encoding="utf-8")
        assert "delivery_authorization_decision" in text
        assert '"decision":"allow"' in text
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
