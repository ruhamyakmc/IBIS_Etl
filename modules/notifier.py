from __future__ import annotations

import html as _html
import io
import logging
import smtplib
import ssl
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from cryptography.fernet import Fernet
import pandas as pd
from stages.base import StageResult

logger = logging.getLogger(__name__)

_UG_SITE_NAMES: dict[str, str] = {
    '11': 'Bushenyi HCIV',
    '12': 'Ishaka Adv. Hosp',
    '13': 'Ishongororo HCIV',
    '14': 'Ruhoko HCIV',
    '99': 'Other',
}


def _load_smtp_password(ini_path: str, key_path: str) -> str:
    """
    Read the Fernet-encrypted Password from ini_path using the key in key_path.
    Raises KeyError if 'Password' is absent from the ini file.
    """
    with open(key_path, 'r') as f:
        key = f.read().strip().encode()
    cipher = Fernet(key)

    cfg: dict[str, str] = {}
    with open(ini_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            cfg[k.strip()] = v.strip()

    if 'Password' not in cfg:
        raise KeyError("'Password' key not found in SMTP credential file.")

    return cipher.decrypt(cfg['Password'].encode()).decode()


def _query_validation_report(engine) -> pd.DataFrame | None:
    """
    Query gold_ibis.ds_validation_report.
    Returns None if the table does not exist or any error occurs.
    """
    try:
        return pd.read_sql('SELECT * FROM gold_ibis.ds_validation_report', engine)
    except Exception as exc:
        logger.warning("Could not query ds_validation_report: %s", exc)
        return None


def _build_stage_summary(
    results: dict[str, StageResult],
    stages: list[str],
) -> str:
    sep = '─' * 47
    lines = ['Stage Results', sep]
    for name in stages:
        if name not in results:
            lines.append(f'  —  {name:<28}  skipped')
        elif results[name].success:
            rw = results[name].rows_written
            row_str = f'{rw:,} rows' if rw else ''
            lines.append(f'  ✓  {name:<28}  {row_str}')
        else:
            lines.append(f'  ✗  {name:<28}  FAILED')
    lines.append(sep)
    return '\n'.join(lines)


def _build_validation_summary(report_df: pd.DataFrame | None) -> str:
    """Concise summary for the email body — full detail is in the CSV attachment."""
    if report_df is None:
        return 'Validation report unavailable — measures_ibis did not run.\n'

    sep = '─' * 47
    lines = ['Validation Issues (see attachment for full detail)', sep]

    for severity in ['ERROR', 'WARNING']:
        subset = report_df[report_df['severity'] == severity]
        if subset.empty:
            continue
        lines.append(f'\n  {severity}S ({len(subset)} record(s)):')
        for (country, site), group in subset.groupby(['country', 'site'], sort=True, dropna=False):
            header = f'{country} / {site}' if site else str(country)
            lines.append(f'    {header}')
            for check, cnt in group.groupby('check').size().items():
                lines.append(f'      • {check}  ({cnt})')

    lines.append(sep)
    return '\n'.join(lines)


def _attach_csv(msg: MIMEMultipart, df: pd.DataFrame, filename: str) -> None:
    """Sanitise and attach a DataFrame as a UTF-8 CSV to a MIME message."""
    csv_buffer = io.StringIO()
    safe_df = df.copy()
    for col in safe_df.select_dtypes(include='object').columns:
        safe_df[col] = safe_df[col].map(
            lambda v: ("'" + v) if isinstance(v, str) and v and v[0] in ('=', '+', '-', '@', '\t', '\r') else v
        )
    safe_df.to_csv(csv_buffer, index=False)
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(csv_buffer.getvalue().encode('utf-8-sig'))
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
    msg.attach(part)


def _send(
    email_cfg: dict,
    recipients: list[str],
    subject: str,
    plain: str,
    html: str,
    attachment_df: pd.DataFrame | None = None,
    attachment_filename: str | None = None,
    extra_attachments: list[tuple[pd.DataFrame, str]] | None = None,
) -> None:
    """Assemble a multipart email and send it via SMTP with STARTTLS."""
    ini_path = email_cfg['keyfiles']['smtp_ini']
    key_path = email_cfg['keyfiles']['smtp_key']
    username = email_cfg['smtp_username']
    password = _load_smtp_password(ini_path, key_path)

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From'] = email_cfg['sender']
    msg['To'] = ', '.join(recipients)

    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(plain, 'plain'))
    alt.attach(MIMEText(html, 'html'))
    msg.attach(alt)

    if attachment_df is not None:
        filename = attachment_filename or f'ibis_validation_{date.today().strftime("%Y-%m-%d")}.csv'
        _attach_csv(msg, attachment_df, filename)

    for df, fname in (extra_attachments or []):
        _attach_csv(msg, df, fname)

    with smtplib.SMTP(email_cfg['smtp_host'], email_cfg['smtp_port']) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.sendmail(email_cfg['sender'], recipients, msg.as_string())


def send_pipeline_report(
    results: dict[str, StageResult],
    stages: list[str],
    engine,
    config,
) -> None:
    """
    Send two targeted emails after a pipeline run:

    - pipeline_recipients: always notified (success or failure) with stage summary.
    - field_recipients: notified only when validation issues (ERRORs or WARNINGs)
      exist, with validation summary and CSV attachment.

    Silently returns if no email config is present.
    SMTP errors are caught and logged — never raised to the pipeline.
    """
    email_cfg = config.get('email')
    if not email_cfg:
        return

    # Query once — used for both field email trigger and body
    report_df = _query_validation_report(engine)

    # Merge any stage-level warnings (e.g. corrupt archives) into the report.
    stage_warnings = [w for r in results.values() for w in r.warnings]
    if stage_warnings:
        warnings_df = pd.DataFrame(stage_warnings)
        report_df = pd.concat([warnings_df, report_df], ignore_index=True) if report_df is not None else warnings_df

    # Filter validation report to configured countries (if specified)
    notify_countries = email_cfg.get('notify_countries')
    if notify_countries and report_df is not None:
        report_df = report_df[report_df['country'].str.lower().isin(
            [c.lower() for c in notify_countries]
        )]

    has_failures = any(not r.success for r in results.values())
    has_issues = (
        report_df is not None
        and not report_df.empty
        and report_df['severity'].isin(['ERROR', 'WARNING']).any()
    )

    today = date.today().strftime('%d %b %Y')
    stage_section = _build_stage_summary(results, stages)

    # --- Pipeline recipients: always send ---
    pipeline_recipients = email_cfg.get('pipeline_recipients', [])
    if pipeline_recipients:
        if has_failures:
            pipeline_subject = f'IBIS Pipeline \u2014 FAILED ({today})'
        else:
            pipeline_subject = f'IBIS Pipeline \u2014 Run complete ({today})'

        pipeline_plain = stage_section
        sms_summary = _build_sms_summary(results)
        if sms_summary:
            pipeline_plain = f"{pipeline_plain}\n\n{sms_summary}"
        pipeline_html = f'<pre style="font-family:monospace;font-size:13px">{_html.escape(pipeline_plain)}</pre>'
        try:
            _send(email_cfg, pipeline_recipients, pipeline_subject, pipeline_plain, pipeline_html)
            logger.info(f'Pipeline status email sent to {pipeline_recipients}.')
        except Exception as exc:
            logger.error(f'Notifier failed (pipeline recipients) \u2014 email not sent: {exc}')

    # --- Field recipients: per-country, only when that country has issues ---
    field_recipients_cfg = email_cfg.get('field_recipients', {})
    if isinstance(field_recipients_cfg, dict) and report_df is not None and not report_df.empty:
        for country, recipients in field_recipients_cfg.items():
            if not recipients:
                continue
            country_df = report_df[report_df['country'].str.lower() == country.lower()]
            if country_df.empty or not country_df['severity'].isin(['ERROR', 'WARNING']).any():
                continue
            field_subject = f'IBIS Data Quality \u2014 {country.title()} issues found ({today})'
            validation_section = _build_validation_summary(country_df)
            field_plain = f'{stage_section}\n\n{validation_section}'
            field_html = f'<pre style="font-family:monospace;font-size:13px">{_html.escape(field_plain)}</pre>'
            try:
                _send(email_cfg, recipients, field_subject, field_plain, field_html, attachment_df=country_df)
                logger.info(f'Field quality report ({country}) sent to {recipients}.')
            except Exception as exc:
                logger.error(f'Notifier failed (field recipients {country}) \u2014 email not sent: {exc}')


def _build_sms_summary(results: dict[str, 'StageResult']) -> str | None:
    """Build SMS summary section for the pipeline email. Returns None if send_sms didn't run."""
    sms_result = results.get('send_sms')
    if sms_result is None:
        return None
    meta = getattr(sms_result, 'metadata', {})
    if not meta:
        return None

    sent     = meta.get('sent', 0)
    failed   = meta.get('failed', 0)
    skipped  = meta.get('skipped', 0)
    failures = meta.get('failures', [])

    sep   = '─' * 47
    lines = ['SMS Summary', sep]
    lines.append(f'  Sent:     {sent}')
    lines.append(f'  Failed:   {failed}')
    lines.append(f'  Skipped:  {skipped}')

    if failures:
        lines.append('')
        lines.append('  Failed messages:')
        for failure in failures:
            lines.append(
                f"    subjid={failure['subjid']}  mobile={failure['mobile_number']}"
                f"  week={failure['week']}  error: {failure['error']}"
            )
    lines.append(sep)
    return '\n'.join(lines)


def _build_weekly_sms_table(rows: list[dict], title: str) -> str:
    """
    Build a transposed SMS stats table: sites as columns, weeks+metrics as rows.
    rows: list of dicts with keys health_facility_ug, week, due, submitted, delivered,
          undelivered, pending.
    """
    if not rows:
        return f'{title}\n  No activity.\n'

    # Determine which site codes appear in data, in canonical order
    all_sites = [
        code for code in _UG_SITE_NAMES
        if any(str(r['health_facility_ug']) == code for r in rows)
    ]
    all_weeks = sorted({r['week'] for r in rows})

    # Build lookup: (site_code, week) -> row dict
    lookup: dict[tuple, dict] = {}
    for r in rows:
        lookup[(str(r['health_facility_ug']), r['week'])] = r

    col_w = 17    # width of each site data column (longest name is 16 chars)
    week_w = 9    # "Week 11 " padded
    metric_w = 13 # "Delivered    " padded — longest metric label is 9 chars
    label_w = week_w + metric_w  # 22

    sep = '─' * (label_w + col_w * len(all_sites) + col_w)

    # Header row
    header = f"{'':>{label_w}}"
    for code in all_sites:
        name = _UG_SITE_NAMES.get(code, code)
        header += f'{name:>{col_w}}'
    header += f'{"Total":>{col_w}}'

    lines = [title, sep, header, sep]

    metrics = [
        ('due',         'Due'),
        ('submitted',   'Sent'),
        ('delivered',   'Delivered'),
        ('undelivered', 'Failed'),
        ('pending',     'Pending'),
    ]

    for week in all_weeks:
        first = True
        for key, label in metrics:
            site_vals = [lookup.get((code, week), {}).get(key, 0) for code in all_sites]
            total = sum(site_vals)
            week_label = f'Week {week}' if first else ''
            first = False
            row_label = f'{week_label:<{week_w}}{label:<{metric_w}}'
            data_cols = ''.join(f'{v:>{col_w}}' for v in site_vals)
            lines.append(f'{row_label}{data_cols}{total:>{col_w}}')
        lines.append(sep)

    return '\n'.join(lines)


def _build_weekly_sms_df(rows: list[dict], period_label: str) -> pd.DataFrame:
    """
    Build a formatted DataFrame matching the weekly SMS report layout.

    Columns: (blank label), site names, Total, %
    Rows per week:
      - Due for Xwk SMS (n)
      - Xwk SMS Outcome           (section header)
      - • Sent (n, %)             — % of Due
      - • Delivered (n, %)        — % of Sent
      - • Failed (N, %)           — % of Sent (confirmed failures only)
      - • Pending (n, %)          — % of Sent
    """
    if not rows:
        return pd.DataFrame()

    all_sites = [
        code for code in _UG_SITE_NAMES
        if any(str(r['health_facility_ug']) == code for r in rows)
    ]
    site_names = [_UG_SITE_NAMES.get(c, c) for c in all_sites]
    all_weeks = sorted({r['week'] for r in rows})
    lookup = {(str(r['health_facility_ug']), r['week']): r for r in rows}

    cols = [''] + site_names + ['Total', '%']

    def blank() -> dict:
        return {c: '' for c in cols}

    def fmt_cell(count: int, denom: int) -> str:
        if denom == 0:
            return ''
        return f'{count} ({count / denom * 100:.1f}%)'

    def pct_str(num: int, denom: int) -> str:
        if denom == 0:
            return ''
        return f'{num / denom * 100:.1f}%'

    records = []
    records.append(blank() | {'': period_label})

    for i, week in enumerate(all_weeks):
        if i > 0:
            records.append(blank())

        due_vals    = [lookup.get((c, week), {}).get('due', 0)         for c in all_sites]
        submitted   = [lookup.get((c, week), {}).get('submitted', 0)   for c in all_sites]
        delivered   = [lookup.get((c, week), {}).get('delivered', 0)   for c in all_sites]
        undelivered = [lookup.get((c, week), {}).get('undelivered', 0) for c in all_sites]
        pending     = [lookup.get((c, week), {}).get('pending', 0)     for c in all_sites]

        tot_due  = sum(due_vals)
        tot_sent = sum(submitted)
        tot_del  = sum(delivered)
        tot_fail = sum(undelivered)
        tot_pend = sum(pending)

        # Due row
        due_row = blank() | {'': f'Due for {week}wk SMS (n)', 'Total': tot_due}
        for name, val in zip(site_names, due_vals):
            due_row[name] = val
        records.append(due_row)

        # Outcome section header
        records.append(blank() | {'': f'{week}wk SMS Outcome'})

        # Sent row — % of Due
        sent_row = blank() | {
            '': '  • Sent (n, %)',
            'Total': tot_sent,
            '%': pct_str(tot_sent, tot_due),
        }
        for name, val, d in zip(site_names, submitted, due_vals):
            sent_row[name] = fmt_cell(val, d)
        records.append(sent_row)

        # Delivered row — % of Sent
        del_row = blank() | {
            '': '  • Delivered (n, %)',
            'Total': tot_del,
            '%': pct_str(tot_del, tot_sent),
        }
        for name, val, sub in zip(site_names, delivered, submitted):
            del_row[name] = fmt_cell(val, sub)
        records.append(del_row)

        # Failed row (confirmed failures only) — % of Sent
        fail_row = blank() | {
            '': '  • Failed (N, %)',
            'Total': tot_fail,
            '%': pct_str(tot_fail, tot_sent),
        }
        for name, val, sub in zip(site_names, undelivered, submitted):
            fail_row[name] = fmt_cell(val, sub)
        records.append(fail_row)

        # Pending row — % of Sent
        pend_row = blank() | {
            '': '  • Pending (n, %)',
            'Total': tot_pend,
            '%': pct_str(tot_pend, tot_sent),
        }
        for name, val, sub in zip(site_names, pending, submitted):
            pend_row[name] = fmt_cell(val, sub)
        records.append(pend_row)

    return pd.DataFrame(records, columns=cols)


def _build_weekly_sms_report(weekly_rows: list[dict], cumulative_rows: list[dict], week_ending: str) -> str:
    """Build full weekly SMS report: this-week table + cumulative table."""
    parts = [
        f'IBIS SMS Weekly Report — week ending {week_ending}',
        '',
        _build_weekly_sms_table(weekly_rows, 'This week'),
        '',
        _build_weekly_sms_table(cumulative_rows, 'Cumulative (all time)'),
    ]
    return '\n'.join(parts)


def _query_followup_due(engine) -> list[dict]:
    """
    Query gold_ibis.ds_followup_due for Uganda, aggregated by facility.
    Returns a list of dicts with keys: health_facility_ug, entered_window,
    primary_endpoint_done, done_not_due, due_pending, overdue.
    Returns [] if the table does not exist yet.
    """
    sql = """
        SELECT
            health_facility_ug,
            COUNT(*) FILTER (
                WHERE window_status IN ('due', 'attended', 'overdue')
            )                                                       AS entered_window,
            COUNT(*) FILTER (WHERE window_status = 'attended')      AS primary_endpoint_done,
            COUNT(*) FILTER (
                WHERE has_followup AND followup_out_of_window
            )                                                       AS done_not_due,
            COUNT(*) FILTER (
                WHERE window_status IN ('due')
            )                                                       AS due_pending,
            COUNT(*) FILTER (WHERE window_status = 'overdue')       AS overdue
        FROM gold_ibis.ds_followup_due
        WHERE countrycode::integer = 1
        GROUP BY health_facility_ug
        ORDER BY health_facility_ug
    """
    try:
        df = pd.read_sql(sql, engine)
        return df.to_dict('records')
    except Exception as exc:
        logger.warning("Could not query ds_followup_due: %s", exc)
        return []


def _build_followup_df(rows: list[dict]) -> pd.DataFrame:
    """
    Build follow-up tracking DataFrame: parameters as rows, sites as columns.
    Mirrors the layout of _build_weekly_sms_df.
    """
    all_sites = [
        code for code in _UG_SITE_NAMES
        if any(str(r.get('health_facility_ug', '')) == code for r in rows)
    ]
    site_names = [_UG_SITE_NAMES.get(c, c) for c in all_sites]
    lookup = {str(r['health_facility_ug']): r for r in rows}
    cols = [''] + site_names + ['Total', '%']

    def blank() -> dict:
        return {c: '' for c in cols}

    def fmt_pct(n: int, denom: int) -> str:
        if denom == 0:
            return '—'
        return f'{n} ({n / denom * 100:.1f}%)'

    def pct_str(n: int, denom: int) -> str:
        if denom == 0:
            return '—'
        return f'{n / denom * 100:.1f}%'

    def site_vals(key: str) -> list[int]:
        return [int(lookup.get(c, {}).get(key, 0)) for c in all_sites]

    entered   = site_vals('entered_window')
    done      = site_vals('primary_endpoint_done')
    not_due   = site_vals('done_not_due')
    pending   = site_vals('due_pending')
    overdue   = site_vals('overdue')

    tot_entered = sum(entered)
    tot_done    = sum(done)
    tot_not_due = sum(not_due)
    tot_pending = sum(pending)
    tot_overdue = sum(overdue)

    records = []

    # Entered row
    r = blank() | {'': 'Entered follow-up period (≥3mo from BL) (n)', 'Total': tot_entered}
    for name, val in zip(site_names, entered):
        r[name] = val
    records.append(r)

    # Completed visit = done + not_due
    completed     = [d + nd for d, nd in zip(done, not_due)]
    tot_completed = tot_done + tot_not_due
    r = blank() | {'': 'Completed follow-up visit (n)', 'Total': tot_completed}
    for name, val in zip(site_names, completed):
        r[name] = val
    records.append(r)

    # Primary endpoint done
    r = blank() | {
        '': '  \u2022 Primary Endpoint Done (n,%)',
        'Total': tot_done,
        '%': pct_str(tot_done, tot_entered),
    }
    for name, val, ent in zip(site_names, done, entered):
        r[name] = fmt_pct(val, ent)
    records.append(r)

    # Done not due
    r = blank() | {'': '  \u2022 Visit Done - Not Due (n)', 'Total': tot_not_due}
    for name, val in zip(site_names, not_due):
        r[name] = val
    records.append(r)

    # Due pending
    r = blank() | {
        '': 'Due Pending (n,%)',
        'Total': tot_pending,
        '%': pct_str(tot_pending, tot_entered),
    }
    for name, val, ent in zip(site_names, pending, entered):
        r[name] = fmt_pct(val, ent)
    records.append(r)

    # Overdue
    r = blank() | {
        '': 'Overdue (n,%)',
        'Total': tot_overdue,
        '%': pct_str(tot_overdue, tot_entered),
    }
    for name, val, ent in zip(site_names, overdue, entered):
        r[name] = fmt_pct(val, ent)
    records.append(r)

    return pd.DataFrame(records, columns=cols)


def _build_followup_table(rows: list[dict]) -> str:
    """Plain-text follow-up tracking table for email body."""
    if not rows:
        return 'Follow-up Tracking\n  No data yet (ds_followup_due not populated).\n'

    all_sites = [
        code for code in _UG_SITE_NAMES
        if any(str(r.get('health_facility_ug', '')) == code for r in rows)
    ]
    lookup = {str(r['health_facility_ug']): r for r in rows}

    col_w   = 17
    label_w = 38
    sep = '─' * (label_w + col_w * len(all_sites) + col_w)

    header = f"{'':>{label_w}}"
    for code in all_sites:
        header += f'{_UG_SITE_NAMES.get(code, code):>{col_w}}'
    header += f'{"Total":>{col_w}}'

    def site_vals(key: str) -> list[int]:
        return [int(lookup.get(c, {}).get(key, 0)) for c in all_sites]

    def fmt_pct(n: int, denom: int) -> str:
        return f'{n} ({n/denom*100:.1f}%)' if denom else '—'

    entered   = site_vals('entered_window')
    done      = site_vals('primary_endpoint_done')
    not_due   = site_vals('done_not_due')
    pending   = site_vals('due_pending')
    overdue_v = site_vals('overdue')

    tot_e, tot_d, tot_nd, tot_p, tot_o = (
        sum(entered), sum(done), sum(not_due), sum(pending), sum(overdue_v)
    )
    completed   = [d + nd for d, nd in zip(done, not_due)]
    tot_c = tot_d + tot_nd

    def row(label: str, vals: list, total, pct_denom: list | None = None) -> str:
        cells = ''
        for v, e in zip(vals, pct_denom or [None]*len(vals)):
            cells += f'{(fmt_pct(v, e) if e is not None else v):>{col_w}}'
        cells += f'{(fmt_pct(total, sum(pct_denom)) if pct_denom else total):>{col_w}}'
        return f'{label:<{label_w}}{cells}'

    lines = ['Follow-up Tracking — Uganda', sep, header, sep]
    lines.append(row('Entered follow-up period (≥3mo) (n)', entered, tot_e))
    lines.append(row('Completed follow-up visit (n)', completed, tot_c))
    lines.append(row('  • Primary Endpoint Done (n,%)', done, tot_d, entered))
    lines.append(row('  • Visit Done - Not Due (n)', not_due, tot_nd))
    lines.append(row('Due Pending (n,%)', pending, tot_p, entered))
    lines.append(row('Overdue (n,%)', overdue_v, tot_o, entered))
    lines.append(sep)
    return '\n'.join(lines)


def send_sms_weekly_report(engine, config) -> None:
    """Send weekly SMS activity report to Uganda field recipients."""
    from datetime import date, timedelta
    email_cfg = config.get('email')
    if not email_cfg:
        return

    uganda_recipients = email_cfg.get('sms_dm_recipients', [])
    if not uganda_recipients:
        logger.warning("No sms_dm_recipients configured — weekly SMS report not sent.")
        return

    # Wednesday-to-Tuesday window: last Wednesday 00:00 to end of this Tuesday.
    # Report is sent Wednesday morning covering the previous Wed–Tue period.
    today = date.today()
    days_since_tuesday = (today.weekday() - 1) % 7
    this_tuesday = today - timedelta(days=days_since_tuesday)
    week_start = this_tuesday - timedelta(days=6)   # previous Wednesday
    week_end = this_tuesday + timedelta(days=1)     # exclusive upper bound — includes all of Tuesday

    from modules.sms_processor import SmsProcessor
    processor = SmsProcessor(config=config, engine=engine)
    weekly_rows = processor.get_weekly_report_data(week_start=week_start, week_end=week_end)
    cumulative_rows = processor.get_cumulative_report_data()

    followup_rows = _query_followup_due(engine)

    if not weekly_rows and not cumulative_rows and not followup_rows:
        logger.info("No SMS activity or follow-up data — weekly report not sent.")
        return

    week_ending_str = this_tuesday.strftime('%d %b %Y')
    subject = f'IBIS SMS Weekly Report \u2014 week ending {week_ending_str}'
    plain = _build_weekly_sms_report(weekly_rows, cumulative_rows, week_ending_str)
    plain = f'{plain}\n\n{_build_followup_table(followup_rows)}'
    html = f'<pre style="font-family:monospace;font-size:13px">{_html.escape(plain)}</pre>'

    # Build CSV attachment in the formatted report layout,
    # with a blank spacer row separating the sections.
    frames = []
    if weekly_rows:
        frames.append(_build_weekly_sms_df(weekly_rows, f'This week (ending {week_ending_str})'))
    if weekly_rows and cumulative_rows:
        frames.append(pd.DataFrame([{}]))
    if cumulative_rows:
        frames.append(_build_weekly_sms_df(cumulative_rows, 'Cumulative (all time)'))
    if followup_rows:
        if frames:
            frames.append(pd.DataFrame([{}]))
        frames.append(_build_followup_df(followup_rows))
    attachment_df = pd.concat(frames, ignore_index=True) if frames else None

    csv_filename = f'ibis_sms_report_{this_tuesday.strftime("%Y-%m-%d")}.csv'

    # Build delivery linelist — all messages sent to date, one row per message.
    linelist_rows = processor.get_delivery_linelist()
    linelist_df = pd.DataFrame(linelist_rows) if linelist_rows else None
    if linelist_df is not None:
        linelist_df.columns = ['Subject ID', 'Site', 'Week', 'Arm', 'Language',
                               'Mobile', 'Scheduled Date', 'Sent At (EAT)', 'Delivery Status']
    linelist_filename = f'ibis_sms_linelist_{this_tuesday.strftime("%Y-%m-%d")}.csv'

    try:
        extra = [(linelist_df, linelist_filename)] if linelist_df is not None else []
        _send(email_cfg, uganda_recipients, subject, plain, html,
              attachment_df=attachment_df, attachment_filename=csv_filename,
              extra_attachments=extra)
        logger.info('Weekly SMS report sent to %s.', uganda_recipients)
    except Exception as exc:
        logger.error('Weekly SMS report email failed: %s', exc)


def send_sms_flagged_alert(flagged: list[dict], config, engine) -> None:
    """
    Send daily alert to data manager listing messages that never reached Blasta.
    Only called when flagged is non-empty.
    """
    from datetime import date
    email_cfg = config.get('email')
    if not email_cfg:
        return

    dm_recipients = email_cfg.get('sms_dm_recipients', [])
    if not dm_recipients:
        logger.warning("No sms_dm_recipients configured — flagged alert not sent.")
        return

    today_str = date.today().strftime('%d %b %Y')
    subject = f'IBIS SMS \u2014 Action Required: {today_str}'

    sep = '─' * 85
    lines = [
        f'IBIS SMS — Action Required: {today_str}',
        sep,
        'The following messages failed to reach Blasta and require manual resending.',
        '',
        f'{"Participant":<20} {"Site":<35} {"Week":>4}  Last Error',
        sep,
    ]

    for msg in flagged:
        site_name = _UG_SITE_NAMES.get(
            str(msg.get('health_facility_ug', '')),
            str(msg.get('health_facility_ug', 'Unknown')),
        )
        error = (msg.get('last_error') or 'unknown')[:40]
        lines.append(
            f"{msg['subjid']:<20} {site_name:<35} {msg['week']:>4}  {error}"
        )

    lines.append(sep)

    # Build resend SQL grouped by week
    by_week: dict[int, list[str]] = {}
    for msg in flagged:
        by_week.setdefault(msg['week'], []).append(msg['subjid'])

    lines.append('To resend, run:')
    for week, subjids in sorted(by_week.items()):
        id_list = ', '.join(f"'{s}'" for s in subjids)
        lines.append(f"  UPDATE sms.queue SET status = 'pending'")
        lines.append(f"  WHERE subjid IN ({id_list}) AND week = {week};")

    lines.append(sep)

    plain = '\n'.join(lines)
    html = f'<pre style="font-family:monospace;font-size:13px">{_html.escape(plain)}</pre>'

    try:
        _send(email_cfg, dm_recipients, subject, plain, html)
        logger.info('Flagged SMS alert sent to %s (%d messages).', dm_recipients, len(flagged))
    except Exception as exc:
        logger.error('Flagged SMS alert email failed: %s', exc)
