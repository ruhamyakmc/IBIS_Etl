#!/usr/bin/env python3
"""Preview the weekly SMS report in the terminal without sending email.

Run from project root:
    python scripts/preview_weekly_report.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.config import ConfigLoader
from modules.db import create_db_engine
from modules.notifier import (
    _build_weekly_sms_report,
    _build_followup_table,
    _query_followup_due,
)
from modules.sms_processor import SmsProcessor


def main() -> None:
    config = ConfigLoader('config.json')
    engine = create_db_engine(config)

    today = date.today()
    days_since_tuesday = (today.weekday() - 1) % 7
    this_tuesday = today - timedelta(days=days_since_tuesday)
    week_start = this_tuesday - timedelta(days=6)
    week_end = this_tuesday + timedelta(days=1)
    week_ending_str = this_tuesday.strftime('%d %b %Y')

    processor = SmsProcessor(config=config, engine=engine)
    weekly_rows = processor.get_weekly_report_data(week_start=week_start, week_end=week_end)
    cumulative_rows = processor.get_cumulative_report_data()
    followup_rows = _query_followup_due(engine)

    if not weekly_rows and not cumulative_rows and not followup_rows:
        print("No SMS activity or follow-up data.")
        return

    plain = _build_weekly_sms_report(weekly_rows, cumulative_rows, week_ending_str)
    plain = f'{plain}\n\n{_build_followup_table(followup_rows)}'
    print(plain)


if __name__ == '__main__':
    main()
