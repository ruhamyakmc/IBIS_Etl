#!/usr/bin/env python3
"""Export Uganda participants in the Incentive arm.

Produces: Output/ug_incentive_arm.xlsx  (one sheet per Uganda site)

Columns: facility, subjid, dob, participants_name, mobile_number, arm_text

Run from project root:
    python scripts/export_ug_incentive_arm.py
"""
from __future__ import annotations

import io
import os
import smtplib
import ssl
import sys
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import msoffcrypto
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.config import ConfigLoader
from modules.db import create_db_engine
from modules.notifier import _load_smtp_password

UG_FACILITY_LABELS: dict[int, str] = {
    11: 'Bushenyi HCIV',
    12: 'Ishaka Adventist Hospital (Bushenyi)',
    13: 'Ishongororo HCIV (Ibanda)',
    14: 'Ruhoko HCIV (Ibanda)',
    99: 'Other',
}

QUERY = """
    SELECT
        health_facility_ug,
        subjid,
        dob,
        participants_name,
        mobile_number,
        arm_text
    FROM ibis.baseline
    WHERE countrycode::integer = 1
      AND consent::integer = 1
      AND subjid IS NOT NULL
      AND arm_text = 'Incentive'
    ORDER BY health_facility_ug, subjid
"""

OUTPUT = Path('Output/ug_incentive_arm.xlsx')


def write_excel_encrypted(df: pd.DataFrame, output_path: Path, password: str) -> bytes:
    """Write password-protected Excel, save to disk, and return the encrypted bytes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to an in-memory buffer first, then encrypt
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        for facility_code, label in UG_FACILITY_LABELS.items():
            site_df = df[df['health_facility_ug'].astype(str) == str(facility_code)].copy()
            if site_df.empty:
                continue
            site_df.to_excel(writer, sheet_name=label[:31], index=False)
            print(f"    {label[:31]}: {len(site_df)} row(s)")

        known_codes = {str(k) for k in UG_FACILITY_LABELS.keys()}
        other_df = df[~df['health_facility_ug'].astype(str).isin(known_codes)].copy()
        if not other_df.empty:
            other_df.to_excel(writer, sheet_name='Unknown Site', index=False)
            print(f"    Unknown Site: {len(other_df)} row(s)")

    buf.seek(0)
    encrypted = io.BytesIO()
    office_file = msoffcrypto.OfficeFile(buf)
    office_file.encrypt(password, encrypted)

    encrypted_bytes = encrypted.getvalue()
    output_path.write_bytes(encrypted_bytes)
    return encrypted_bytes


def send_report(excel_bytes: bytes, filename: str, config) -> None:
    email_cfg = config.get('email')
    recipients = config.get('exports', {}).get('incentive_report_recipients', [])
    if not recipients:
        print("No incentive_report_recipients configured — skipping email.")
        return

    today = date.today().strftime('%Y-%m-%d')
    today_long = date.today().strftime('%B %-d, %Y')
    password = _load_smtp_password(email_cfg['keyfiles']['smtp_ini'], email_cfg['keyfiles']['smtp_key'])

    msg = MIMEMultipart('mixed')
    msg['Subject'] = f'IBIS Uganda — Incentive Arm Participants ({today})'
    msg['From'] = email_cfg['sender']
    msg['To'] = ', '.join(recipients)

    body = (
        f'Please find attached the current list of Uganda Incentive Arm participants as of {today_long}.\n\n'
        f'Please note that the file is password-protected to ensure participant confidentiality. '
        f'If you require access, kindly contact the Data Team to obtain the password.\n\n'
        f'Best regards,\n'
        f'Emmanuel\n'
    )
    msg.attach(MIMEText(body, 'plain'))

    part = MIMEBase('application', 'octet-stream')
    part.set_payload(excel_bytes)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
    msg.attach(part)

    with smtplib.SMTP(email_cfg['smtp_host'], email_cfg['smtp_port']) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(email_cfg['smtp_username'], password)
        smtp.sendmail(email_cfg['sender'], recipients, msg.as_string())

    print(f"Report emailed to: {', '.join(recipients)}")


def main() -> None:
    config = ConfigLoader('config.json')
    db_cfg = config.get('db')
    if os.environ.get('DB_HOST'):
        db_cfg['host'] = os.environ['DB_HOST']
    if os.environ.get('DB_PORT'):
        db_cfg['port'] = int(os.environ['DB_PORT'])
    if os.environ.get('DB_PASSWORD_FILE'):
        db_cfg['password_secret_file'] = os.environ['DB_PASSWORD_FILE']
    engine = create_db_engine(config)

    password = config.get('exports', {}).get('excel_password')
    if not password:
        raise RuntimeError("exports.excel_password not set in config.json")

    print("Querying ibis.baseline for Uganda Incentive arm participants ...")
    with engine.connect() as conn:
        df = pd.read_sql(text(QUERY), conn)

    if df.empty:
        print("No records found.")
        return

    today = date.today().strftime('%Y-%m-%d')
    filename = f'ug_incentive_arm_{today}.xlsx'
    print(f"Found {len(df)} participant(s). Writing to {OUTPUT} ...")
    excel_bytes = write_excel_encrypted(df, OUTPUT, password)
    print(f"Done → {OUTPUT.resolve()}")

    send_report(excel_bytes, filename, config)


if __name__ == '__main__':
    main()
