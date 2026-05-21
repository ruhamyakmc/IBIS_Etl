#!/usr/bin/env python3
"""Standalone IBIS SMS runner.

Usage:
    python sms.py                  # sync queue + send today's messages
    python sms.py --sync           # sync queue only, no sending
    python sms.py --dry-run        # log what would be sent, no actual send
    python sms.py --weekly-report  # send weekly facility report email
    python sms.py --init-db        # create SMS tables (run once after setup)
"""
from __future__ import annotations

import argparse
import logging
import sys

from modules.config import ConfigLoader
from modules.db import create_db_engine, init_schemas, init_sms_tables
from modules.notifier import send_sms_weekly_report
from modules.sms_processor import SmsProcessor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
)
logger = logging.getLogger(__name__)


def init_db(engine) -> None:
    """Create SMS tables (delegates to db.init_sms_tables)."""
    init_sms_tables(engine)
    logger.info("SMS tables created (or already existed).")


def main() -> None:
    parser = argparse.ArgumentParser(description='IBIS SMS standalone runner')
    parser.add_argument('--sync',            action='store_true', help='Sync queue only, no sending')
    parser.add_argument('--dry-run',         action='store_true', help='Log what would be sent, no actual send')
    parser.add_argument('--weekly-report',   action='store_true', help='Send weekly facility report email')
    parser.add_argument('--init-db',         action='store_true', help='Create SMS tables (run once at setup)')
    parser.add_argument('--check-delivery',  action='store_true',
                        help='Poll Blasta DLR for all unconfirmed sent messages')
    parser.add_argument('-v', '--verbose',   action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = ConfigLoader('config.json')
    engine = create_db_engine(config)
    init_schemas(engine)  # ensures sms schema exists

    if args.init_db:
        init_db(engine)
        return

    if args.check_delivery:
        from modules.notifier import send_sms_flagged_alert
        processor = SmsProcessor(config=config, engine=engine)
        dlr = processor.fetch_delivery_statuses()
        logger.info(
            'DLR check complete: checked=%d updated=%d pending=%d errors=%d',
            dlr.checked, dlr.updated, dlr.pending, len(dlr.errors),
        )
        flagged = processor.get_flagged_messages()
        if flagged:
            send_sms_flagged_alert(flagged, config, engine)
            logger.info('%d message(s) flagged — alert sent to data manager.', len(flagged))
        return

    if args.weekly_report:
        send_sms_weekly_report(engine, config)
        return

    if args.dry_run:
        sms_cfg = dict(config.get('sms') or {})
        sms_cfg['dry_run'] = True
        config.config['sms'] = sms_cfg

    processor = SmsProcessor(config=config, engine=engine)

    if args.sync:
        inserted = processor.sync_queue()
        logger.info("Queue sync complete: %d new row(s) inserted.", inserted)
        return

    result = processor.run()
    logger.info(
        "SMS run complete — sent: %d  failed: %d  skipped: %d",
        result.sent, result.failed, result.skipped,
    )
    if result.failures:
        logger.warning("Failed messages:")
        for f in result.failures:
            logger.warning(
                "  subjid=%s  mobile=%s  week=%d: %s",
                f['subjid'], f['mobile_number'], f['week'], f['error'],
            )

    sys.exit(1 if result.failed > 0 else 0)


if __name__ == '__main__':
    main()
