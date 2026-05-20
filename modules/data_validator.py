from __future__ import annotations

"""
data_validator.py
-----------------
Validates a combined baseline DataFrame.

Validation checks performed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1.  Missing required values on core identifier fields.
2.  Age bounds: respondants_age must be 10-110 or -7.
3.  Cross-country field contamination (e.g. Kenya records with Uganda facility).
4.  Duplicate uniqueid values (same survey session recorded twice).
5.  Duplicate screening_id values (same participant screened more than once).
6.  Consented participants (consent == 1) that lack a subjid.
7.  Missing interviewer_id values.
8.  Records whose countrycode does not match the country folder they came from.
9.  Duplicate subjid values among consented participants.
10. Duplicate phone numbers (after normalising formatting and country codes).
11. Phone numbers differing by exactly one digit (likely transposition/typo).
12. Duplicate participant names (case-insensitive).
13. Highly similar names that are not identical (possible data-entry error).
14. Interview duration anomalies (impossible, too short, or too long).
15. Date of birth / age consistency (future dob, eligibility, mismatch).
16. Visit date validity (future date, stale date, mismatch with starttime).
17. Appointment date logic (appointment before visit, unexpected interval).
18. Consent flow integrity (invalid code, non-consented record with subjid).
19. Client sex coding (invalid code, missing for consented participants).
20. Interviewer productivity (excessive daily interviews, unusual hours).
21. Screening ID format and country-prefix correctness.
22. Health facility code validity (codes must match the country's valid set).
23. Tablet record counts (tablets with suspiciously few records).
24. Overall record completeness (columns with high null rates).

Usage
~~~~~
    from modules.data_validator import DataValidator
    validator = DataValidator()
    report_df = validator.validate(df, country_code=2)   # 2 = Kenya
    report_df.to_csv("Output/quality_report_kenya.csv", index=False)
"""

import logging
import os
import re
from typing import Optional

import pandas as pd

# rapidfuzz / numpy are optional-at-import-time: spawned subprocesses created
# by access_reader re-import this module but may not have these packages on
# their sys.path.  We degrade gracefully to difflib in that case.
try:
    import numpy as np
    from rapidfuzz import fuzz, process as fuzz_process
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    import difflib as _difflib
    _RAPIDFUZZ_AVAILABLE = False

logger = logging.getLogger(__name__)

# -9 is inserted by the tablet software when a question is skipped via skip logic.
_SYSTEM_SKIP = -9
# -7 is the "don't know" response code.
_DONT_KNOW = -7


class DataValidator:
    """Validates a baseline DataFrame."""

    # Core fields that must have a non-null value in every record.
    _REQUIRED_FIELDS = [
        'starttime', 'stoptime', 'countrycode', 'tabletnum',
        'client_sex', 'health_facility', 'screening_id', 'uniqueid',
    ]

    # Exact column names as they appear in the baseline table.
    _PHONE_FIELD = 'mobile_number'
    _NAME_FIELD = 'participants_name'

    # Minimum similarity ratio (0–1) for two names to be flagged as related.
    _NAME_SIMILARITY_THRESHOLD = 0.85

    # Maps lowercase country name to the expected screening_id prefix.
    _COUNTRY_ID_PREFIXES: dict[str, str] = {
        'kenya': 'SCR',
        'uganda': 'SCR',
    }

    # Valid format for a screening ID: alphanumeric, hyphens, underscores only.
    _SCREENING_ID_RE = re.compile(r'^[A-Za-z0-9_\-]+$')

    # Health facility code → label mappings (from the data dictionary).
    _FACILITY_CODES_KE: dict[int, str] = {
        21: 'Homa Bay Teaching and Referral Hospital',
        22: 'Rachuonyo District Hospital',
        23: 'Suba District Hospital',
        24: 'Ndhiwa District Hospital',
        99: 'Other',
    }
    _FACILITY_CODES_UG: dict[int, str] = {
        11: 'Bushenyi HCIV',
        12: 'Ishaka Adventist Hospital (Bushenyi)',
        13: 'Ishongororo HCIV (Ibanda)',
        14: 'Ruhoko HCIV (Ibanda)',
        99: 'Other',
    }

    # Columns excluded from sparse_column check — known to be intentionally empty
    # (e.g. vdate mirrors starttime and is never independently populated).
    _COMPLETENESS_EXCLUDE: set[str] = {'vdate', 'age'}

    def __init__(self, system_skip_code: int = _SYSTEM_SKIP):
        self.skip_code = system_skip_code

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        df: pd.DataFrame,
        country_code: Optional[int] = None,
        country_name: str = '',
        site_name: str = '',
        skip_identity: bool = False,
    ) -> pd.DataFrame:
        """
        Run all 24 validation checks and return a quality-report DataFrame.

        Each row in the report represents one issue found.  Columns:
            check            - name of the validation check
            severity         - 'ERROR' or 'WARNING'
            country          - country the records belong to
            site             - community/site the records belong to
            field            - the column that triggered the issue (if applicable)
            record_count     - number of affected records
            detail           - human-readable description
            affected_subjids - semicolon-separated subjids (or screening_ids) of
                               the specific records involved, where applicable
            affected_tablets - comma-separated tablet numbers of the affected rows
        """
        issues: list[dict] = []

        issues += self._check_required_fields(df)
        issues += self._check_age(df)
        issues += self._check_cross_country_fields(df)
        issues += self._check_health_facility_codes(df)
        issues += self._check_duplicate_uniqueid(df)
        issues += self._check_duplicate_screening_id(df)
        issues += self._check_consent_without_subjid(df)
        issues += self._check_missing_interviewer_id(df)
        if country_code is not None:
            issues += self._check_countrycode_mismatch(df, country_code, country_name)

        # Duplicate / related participant identity checks
        # (skipped at per-facility level when a country-level pass handles them)
        if not skip_identity:
            issues += self._check_duplicate_subjid(df)
            issues += self._check_duplicate_phone(df)
            issues += self._check_similar_phones(df)
            issues += self._check_duplicate_name(df)
            issues += self._check_similar_names(df)

        # Temporal and logical checks
        issues += self._check_interview_duration(df)
        issues += self._check_dob_age_consistency(df)
        issues += self._check_visit_date(df)
        issues += self._check_appointment_dates(df)

        # Coding / consent integrity checks
        issues += self._check_consent_flow(df)
        issues += self._check_client_sex(df)

        # Operational signal checks
        issues += self._check_interviewer_productivity(df)
        issues += self._check_screening_id_format(df, country_name)
        issues += self._check_tablet_record_counts(df)
        issues += self._check_record_completeness(df)

        for issue in issues:
            issue.setdefault('country', country_name)
            issue.setdefault('site', site_name)
            issue.setdefault('affected_tablets', '')

        report = pd.DataFrame(issues, columns=[
            'check', 'severity', 'country', 'site', 'field', 'record_count',
            'detail', 'affected_subjids', 'affected_tablets',
        ])
        report['affected_subjids'] = report['affected_subjids'].fillna('')
        report['affected_tablets'] = report['affected_tablets'].fillna('')
        total_errors = (report['severity'] == 'ERROR').sum()
        total_warnings = (report['severity'] == 'WARNING').sum()
        logger.info(
            f"Validation complete [{country_name or 'combined'}]: "
            f"{total_errors} error(s), {total_warnings} warning(s)."
        )
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_float_suffix(value) -> str:
        """
        Convert a value to a clean string, removing the spurious '.0' that
        pandas adds when an integer column is read from Access as float64.
        e.g. 712345678.0 -> '712345678', 'S001' -> 'S001'.
        """
        s = str(value).strip()
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
        except (ValueError, OverflowError):
            pass
        return s

    def _normalize_phone(self, phone) -> str:
        """
        Strip formatting and leading country codes so numbers from different
        tablets can be compared fairly.
        Handles: +254…, 254…, +256…, 256… (Kenya/Uganda), leading 0, and
        float representation from Access/pandas (e.g. 712345678.0).
        Returns the bare 9-digit local number, or the raw digit string if the
        pattern is unrecognised.
        """
        digits = re.sub(r'\D', '', self._strip_float_suffix(phone))
        for prefix in ('254', '256'):
            if digits.startswith(prefix) and len(digits) == 12:
                return digits[3:]
        if digits.startswith('0') and len(digits) == 10:
            return digits[1:]
        return digits

    def _subjids_for_mask(self, df: pd.DataFrame, mask) -> str:
        """
        Return a semicolon-separated string of subjids for the rows identified
        by *mask* (boolean Series or Index).  Falls back to screening_id when
        subjid is absent or only contains skip-code / null values.
        """
        try:
            subset = df.loc[mask]
        except Exception:  # noqa: BLE001
            return ''

        skip_str = str(self.skip_code)

        if 'subjid' in subset.columns:
            subjids = subset['subjid'].map(self._strip_float_suffix)
            valid = subjids[
                subjids.notna()
                & (subjids != '')
                & (subjids != skip_str)
                & (subjids.str.lower() != 'nan')
            ]
            if not valid.empty:
                return '; '.join(sorted(valid.unique().tolist()))

        if 'screening_id' in subset.columns:
            sids = subset['screening_id'].dropna().astype(str).str.strip()
            sids = sids[sids != '']
            if not sids.empty:
                return '; '.join(sorted(sids.unique().tolist()))

        return ''

    @staticmethod
    def _parse_dob(series: pd.Series) -> pd.Series:
        """
        Parse a DOB column and correct 2-digit year ambiguity.

        Access/mdbtools exports dates as MM/DD/YY.  Python's dateutil maps
        years 00-49 to 2000-2049, so participants born in e.g. 1940 appear as
        2040.  Any parsed date that is in the future but becomes a plausible
        DOB (age 10-110) when shifted back 100 years is corrected automatically.
        """
        dob = pd.to_datetime(series, errors='coerce', format='%d/%m/%Y %H:%M:%S')
        today = pd.Timestamp.now().normalize()
        future_mask = dob.notna() & (dob > today)
        if not future_mask.any():
            return dob

        dob = dob.copy()
        future_idx = dob[future_mask].index
        corrected = dob.loc[future_idx].map(
            lambda d: d.replace(year=d.year - 100) if not pd.isna(d) else d
        )
        min_dob = today - pd.DateOffset(years=110)
        max_dob = today - pd.DateOffset(years=10)
        valid = (corrected >= min_dob) & (corrected <= max_dob)
        dob.loc[corrected[valid].index] = corrected[valid]
        return dob

    def _tablets_for_mask(self, df: pd.DataFrame, mask) -> str:
        """Return a comma-separated sorted string of unique tablet numbers
        for the rows identified by *mask* (boolean Series or Index)."""
        try:
            subset = df.loc[mask]
        except Exception:  # noqa: BLE001
            return ''
        if 'tabletnum' not in subset.columns:
            return ''
        tablets = subset['tabletnum'].dropna().map(self._strip_float_suffix)
        tablets = tablets[tablets != '']
        if tablets.empty:
            return ''
        return ', '.join(sorted(tablets.unique().tolist()))

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_required_fields(self, df: pd.DataFrame) -> list[dict]:
        issues = []
        for fname in self._REQUIRED_FIELDS:
            if fname not in df.columns:
                issues.append(dict(
                    check='missing_column',
                    severity='ERROR',
                    field=fname,
                    record_count=len(df),
                    detail=f"Column '{fname}' is absent from the dataset.",
                    affected_subjids='',
                    affected_tablets='',
                ))
                continue
            null_mask = df[fname].isna()
            empty_mask = (df[fname].astype(str).str.strip() == '') & df[fname].notna()
            missing_mask = null_mask | empty_mask
            total_missing = int(missing_mask.sum())
            if total_missing:
                issues.append(dict(
                    check='missing_required_value',
                    severity='ERROR',
                    field=fname,
                    record_count=total_missing,
                    detail=f"{total_missing} record(s) have no value for required field '{fname}'.",
                    affected_subjids=self._subjids_for_mask(df, missing_mask),
                    affected_tablets=self._tablets_for_mask(df, missing_mask),
                ))
        return issues

    def _check_age(self, df: pd.DataFrame) -> list[dict]:
        issues = []
        if 'respondants_age' not in df.columns:
            return issues
        age = pd.to_numeric(df['respondants_age'], errors='coerce')
        bad = age.dropna()
        bad = bad[~(((bad >= 10) & (bad <= 110)) | (bad == _DONT_KNOW) | (bad == self.skip_code))]
        if not bad.empty:
            bad_vals = [int(v) for v in sorted(bad.unique())]
            issues.append(dict(
                check='invalid_age',
                severity='ERROR',
                field='respondants_age',
                record_count=int(len(bad)),
                detail=f"{len(bad)} record(s) have age outside 10-110 (or -7): {bad_vals}.",
                affected_subjids=self._subjids_for_mask(df, bad.index),
                affected_tablets=self._tablets_for_mask(df, bad.index),
            ))
        return issues

    @staticmethod
    def _decode_facility_codes(series: pd.Series, code_map: dict) -> list[str]:
        """Return a sorted list of 'code (Name)' strings for unique non-null values."""
        codes = pd.to_numeric(series, errors='coerce').dropna().unique()
        decoded = sorted(
            f"{int(c)} ({code_map.get(int(c), 'Unknown')})" for c in codes
        )
        return decoded

    def _check_cross_country_fields(self, df: pd.DataFrame) -> list[dict]:
        """Kenya records (countrycode=2) should have -9 in health_facility_ug,
        and Uganda records (countrycode=1) should have -9 in health_facility_ke."""
        issues = []
        country_col = pd.to_numeric(df.get('countrycode'), errors='coerce')

        ke_rows = df[country_col == 2]
        if 'health_facility_ug' in df.columns and not ke_rows.empty:
            ug_fac = pd.to_numeric(ke_rows['health_facility_ug'], errors='coerce')
            contaminated = ke_rows[ug_fac.notna() & (ug_fac != self.skip_code)]
            if not contaminated.empty:
                decoded = self._decode_facility_codes(
                    contaminated['health_facility_ug'], self._FACILITY_CODES_UG
                )
                issues.append(dict(
                    check='cross_country_facility',
                    severity='WARNING',
                    field='health_facility_ug',
                    record_count=int(len(contaminated)),
                    detail=(
                        f"{len(contaminated)} Kenya record(s) have a non-skip value "
                        f"in health_facility_ug: {decoded}"
                    ),
                    affected_subjids=self._subjids_for_mask(df, contaminated.index),
                    affected_tablets=self._tablets_for_mask(df, contaminated.index),
                ))

        ug_rows = df[country_col == 1]
        if 'health_facility_ke' in df.columns and not ug_rows.empty:
            ke_fac = pd.to_numeric(ug_rows['health_facility_ke'], errors='coerce')
            contaminated = ug_rows[ke_fac.notna() & (ke_fac != self.skip_code)]
            if not contaminated.empty:
                decoded = self._decode_facility_codes(
                    contaminated['health_facility_ke'], self._FACILITY_CODES_KE
                )
                issues.append(dict(
                    check='cross_country_facility',
                    severity='WARNING',
                    field='health_facility_ke',
                    record_count=int(len(contaminated)),
                    detail=(
                        f"{len(contaminated)} Uganda record(s) have a non-skip value "
                        f"in health_facility_ke: {decoded}"
                    ),
                    affected_subjids=self._subjids_for_mask(df, contaminated.index),
                    affected_tablets=self._tablets_for_mask(df, contaminated.index),
                ))
        return issues

    def _check_health_facility_codes(self, df: pd.DataFrame) -> list[dict]:
        """
        Validate that each record's health facility code is within the expected
        set for its country.  Kenya records use health_facility_ke (codes 21-24, 99),
        Uganda records use health_facility_ug (codes 11-14, 99).
        """
        issues = []
        country_col = pd.to_numeric(df.get('countrycode'), errors='coerce')
        valid_skip = {self.skip_code}

        checks = [
            (2, 'health_facility_ke', self._FACILITY_CODES_KE, 'Kenya'),
            (1, 'health_facility_ug', self._FACILITY_CODES_UG, 'Uganda'),
        ]
        for cc, field, code_map, label in checks:
            if field not in df.columns:
                continue
            rows = df[country_col == cc]
            if rows.empty:
                continue
            fac = pd.to_numeric(rows[field], errors='coerce')
            valid_codes = set(code_map.keys()) | valid_skip
            # Null / unparseable values are skipped (caught by required-field check)
            bad = rows[fac.notna() & ~fac.isin(valid_codes)]
            if not bad.empty:
                bad_decoded = self._decode_facility_codes(bad[field], code_map)
                issues.append(dict(
                    check='invalid_facility_code',
                    severity='ERROR',
                    field=field,
                    record_count=int(len(bad)),
                    detail=(
                        f"{len(bad)} {label} record(s) have an unrecognised "
                        f"{field} code: {bad_decoded}. "
                        f"Valid codes: {sorted(code_map.keys())}."
                    ),
                    affected_subjids=self._subjids_for_mask(df, bad.index),
                    affected_tablets=self._tablets_for_mask(df, bad.index),
                ))
        return issues

    def _check_duplicate_uniqueid(self, df: pd.DataFrame) -> list[dict]:
        issues = []
        if 'uniqueid' not in df.columns:
            return issues
        has_uid = df['uniqueid'].notna() & (df['uniqueid'].astype(str).str.strip() != '')
        uid_col = df.loc[has_uid, 'uniqueid'].astype(str)
        dup_mask = uid_col.duplicated(keep=False)
        n_dup = dup_mask.sum()
        n_unique_ids = uid_col[dup_mask].nunique()
        if n_dup:
            issues.append(dict(
                check='duplicate_uniqueid',
                severity='ERROR',
                field='uniqueid',
                record_count=int(n_dup),
                detail=(
                    f"{n_dup} row(s) share a uniqueid with at least one other row "
                    f"({n_unique_ids} distinct uniqueid(s) affected). "
                    f"Likely caused by overlapping archive snapshots."
                ),
                affected_subjids=self._subjids_for_mask(df, uid_col[dup_mask].index),
                affected_tablets=self._tablets_for_mask(df, uid_col[dup_mask].index),
            ))
        return issues

    def _check_duplicate_screening_id(self, df: pd.DataFrame) -> list[dict]:
        issues = []
        if 'screening_id' not in df.columns:
            return issues
        has_sid = df['screening_id'].notna() & (df['screening_id'].astype(str).str.strip() != '')
        sid_col = df.loc[has_sid, 'screening_id'].astype(str)
        dup_mask = sid_col.duplicated(keep=False)
        n_dup = dup_mask.sum()
        n_unique_sids = sid_col[dup_mask].nunique()
        if n_dup:
            issues.append(dict(
                check='duplicate_screening_id',
                severity='WARNING',
                field='screening_id',
                record_count=int(n_dup),
                detail=(
                    f"{n_dup} row(s) share a screening_id with at least one other row "
                    f"({n_unique_sids} distinct screening_id(s) affected). "
                    f"Could indicate repeat screening of the same participant or "
                    f"archive overlap."
                ),
                affected_subjids=self._subjids_for_mask(df, sid_col[dup_mask].index),
                affected_tablets=self._tablets_for_mask(df, sid_col[dup_mask].index),
            ))
        return issues

    def _check_consent_without_subjid(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag consented participants who have no subjid at all (null/empty).
        A subjid equal to the skip code (-9) means the participant declined
        or was found ineligible after consenting — this is valid and is not
        flagged here.
        """
        issues = []
        if 'consent' not in df.columns or 'subjid' not in df.columns:
            return issues
        skip_str = str(self.skip_code)
        consent_col = pd.to_numeric(df['consent'], errors='coerce')
        consented = df[consent_col == 1]
        subj_str = consented['subjid'].astype(str).str.strip()
        missing_subj = consented[
            consented['subjid'].isna()
            | subj_str.isin(['', 'nan'])
        ]
        # Exclude skip-code: participant declined or was ineligible — expected.
        missing_subj = missing_subj[
            subj_str.reindex(missing_subj.index) != skip_str
        ]
        if not missing_subj.empty:
            issues.append(dict(
                check='consented_without_subjid',
                severity='ERROR',
                field='subjid',
                record_count=int(len(missing_subj)),
                detail=(
                    f"{len(missing_subj)} consented participant(s) (consent=1) "
                    f"have no subjid (null/empty). Note: subjid={self.skip_code} "
                    f"indicates declined/ineligible and is excluded from this check."
                ),
                affected_subjids=self._subjids_for_mask(df, missing_subj.index),
                affected_tablets=self._tablets_for_mask(df, missing_subj.index),
            ))
        return issues

    def _check_missing_interviewer_id(self, df: pd.DataFrame) -> list[dict]:
        issues = []
        if 'interviewer_id' not in df.columns:
            return issues
        null_mask = df['interviewer_id'].isna()
        null_count = null_mask.sum()
        if null_count:
            issues.append(dict(
                check='missing_interviewer_id',
                severity='WARNING',
                field='interviewer_id',
                record_count=int(null_count),
                detail=f"{null_count} record(s) have no interviewer_id.",
                affected_subjids=self._subjids_for_mask(df, null_mask),
                affected_tablets=self._tablets_for_mask(df, null_mask),
            ))
        return issues

    def _check_countrycode_mismatch(
        self, df: pd.DataFrame, expected_code: int, country_name: str
    ) -> list[dict]:
        issues = []
        if 'countrycode' not in df.columns:
            return issues
        cc = pd.to_numeric(df['countrycode'], errors='coerce')
        mismatched = df[cc.notna() & (cc != expected_code)]
        if not mismatched.empty:
            bad_codes = [int(v) for v in sorted(cc[cc != expected_code].dropna().unique())]
            issues.append(dict(
                check='countrycode_mismatch',
                severity='ERROR',
                field='countrycode',
                record_count=int(len(mismatched)),
                detail=(
                    f"{len(mismatched)} record(s) in the {country_name} dataset have "
                    f"countrycode != {expected_code}: {bad_codes}. "
                    f"These are cross-country contaminated records."
                ),
                affected_subjids=self._subjids_for_mask(df, mismatched.index),
                affected_tablets=self._tablets_for_mask(df, mismatched.index),
            ))
        return issues

    # ------------------------------------------------------------------
    # Duplicate / related identity checks
    # ------------------------------------------------------------------

    def _check_duplicate_subjid(self, df: pd.DataFrame) -> list[dict]:
        """Exact duplicate subjid values across all records that have one.
        Skip-code values (e.g. -9, meaning declined or ineligible) are excluded
        — it is expected that many participants share this sentinel value.
        """
        if 'subjid' not in df.columns:
            return []
        skip_str = str(self.skip_code)
        has_val = (
            df['subjid'].notna()
            & (df['subjid'].astype(str).str.strip() != '')
            & (df['subjid'].astype(str).str.strip().str.lower() != 'nan')
        )
        col = df.loc[has_val, 'subjid'].map(self._strip_float_suffix)
        # Exclude skip-code: consented-but-ineligible/declined participants
        # legitimately receive -9 as their subjid — not a real duplicate.
        col = col[col != skip_str]
        dup_mask = col.duplicated(keep=False)
        n_dup = dup_mask.sum()
        if not n_dup:
            return []
        examples = sorted(col[dup_mask].unique())[:5]
        return [dict(
            check='duplicate_subjid',
            severity='ERROR',
            field='subjid',
            record_count=int(n_dup),
            detail=(
                f"{n_dup} consented record(s) share a subjid with at least one other "
                f"({col[dup_mask].nunique()} distinct subjid(s) affected). "
                f"Examples: {examples}"
            ),
            affected_subjids=self._subjids_for_mask(df, col[dup_mask].index),
            affected_tablets=self._tablets_for_mask(df, col[dup_mask].index),
        )]

    def _check_duplicate_phone(self, df: pd.DataFrame) -> list[dict]:
        """Exact duplicate mobile_number values after normalising formatting."""
        if self._PHONE_FIELD not in df.columns:
            return []
        raw = df[self._PHONE_FIELD].dropna()
        raw = raw[raw.astype(str).str.strip() != '']
        # Exclude skip-code: -9 means the question was skipped, not a real number.
        raw = raw[raw.map(self._strip_float_suffix) != str(self.skip_code)]
        if raw.empty:
            return []
        normalised = raw.map(self._normalize_phone)
        dup_mask = normalised.duplicated(keep=False)
        n_dup = dup_mask.sum()
        if not n_dup:
            return []
        examples = sorted(normalised[dup_mask].unique())[:5]
        return [dict(
            check='duplicate_phone',
            severity='WARNING',
            field=self._PHONE_FIELD,
            record_count=int(n_dup),
            detail=(
                f"{n_dup} record(s) share a mobile number with at least one other "
                f"({normalised[dup_mask].nunique()} distinct number(s) affected). "
                f"Examples: {examples}"
            ),
            affected_subjids=self._subjids_for_mask(df, normalised[dup_mask].index),
            affected_tablets=self._tablets_for_mask(df, normalised[dup_mask].index),
        )]

    def _check_similar_phones(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag pairs of same-length mobile numbers that differ by exactly one digit
        — the most common signature of a transposition or single-digit typo.
        Uses numpy vectorisation when available; falls back to a Python loop.
        """
        if self._PHONE_FIELD not in df.columns:
            return []
        raw = df[self._PHONE_FIELD].dropna()
        raw = raw[raw.astype(str).str.strip() != '']
        # Exclude skip-code: -9 means the question was skipped, not a real number.
        raw = raw[raw.map(self._strip_float_suffix) != str(self.skip_code)]
        if len(raw) < 2:
            return []

        norm = raw.map(self._normalize_phone)
        phones = norm.tolist()
        indices = norm.index.tolist()

        # Only same-length numbers can differ by exactly one digit.
        by_length: dict[int, list[tuple[str, object]]] = {}
        for p, idx in zip(phones, indices):
            by_length.setdefault(len(p), []).append((p, idx))

        pair_count = 0
        affected_idx: list = []
        phone_pairs: list[tuple[str, str]] = []

        for grp in by_length.values():
            if len(grp) < 2:
                continue
            grp_phones = [p for p, _ in grp]
            grp_indices = [i for _, i in grp]
            if _RAPIDFUZZ_AVAILABLE:
                m = np.frombuffer(''.join(grp_phones).encode(), dtype=np.uint8).reshape(
                    len(grp_phones), len(grp_phones[0])
                )
                rows, cols = np.triu_indices(len(grp_phones), k=1)
                one_off = (m[rows] != m[cols]).sum(axis=1) == 1
                pair_count += int(one_off.sum())
                for r, c in zip(rows[one_off].tolist(), cols[one_off].tolist()):
                    affected_idx.extend([grp_indices[r], grp_indices[c]])
                    phone_pairs.append((grp_phones[r], grp_phones[c]))
            else:
                for i in range(len(grp_phones)):
                    for j in range(i + 1, len(grp_phones)):
                        a, b = grp_phones[i], grp_phones[j]
                        if sum(ca != cb for ca, cb in zip(a, b)) == 1:
                            pair_count += 1
                            affected_idx.extend([grp_indices[i], grp_indices[j]])
                            phone_pairs.append((a, b))

        if not pair_count:
            return []
        affected_idx_unique = list(dict.fromkeys(affected_idx))  # deduplicate, preserve order
        pairs_str = '; '.join(f"{a} / {b}" for a, b in phone_pairs)
        return [dict(
            check='similar_phone',
            severity='WARNING',
            field=self._PHONE_FIELD,
            record_count=pair_count,
            detail=(
                f"{pair_count} pair(s) of mobile numbers differ by exactly "
                f"one digit (possible transposition or typo): {pairs_str}."
            ),
            affected_subjids=self._subjids_for_mask(df, affected_idx_unique),
            affected_tablets=self._tablets_for_mask(df, affected_idx_unique),
        )]

    def _check_duplicate_name(self, df: pd.DataFrame) -> list[dict]:
        """
        Exact duplicate participants_name values (case-insensitive).
        Returns one row per duplicate name so each group is actionable.
        """
        if self._NAME_FIELD not in df.columns:
            return []
        raw = df[self._NAME_FIELD].dropna()
        raw = raw[raw.astype(str).str.strip() != '']
        if raw.empty:
            return []
        normalised = raw.astype(str).str.strip().str.lower()
        dup_mask = normalised.duplicated(keep=False)
        if not dup_mask.any():
            return []
        has_dob = 'dob' in df.columns
        if has_dob:
            dob_parsed = self._parse_dob(df['dob']).dt.normalize()

        issues = []
        for name in sorted(normalised[dup_mask].unique()):
            name_mask = normalised == name
            count = int(name_mask.sum())
            matched_idx = name_mask.index[name_mask]

            if has_dob:
                dobs = dob_parsed.loc[matched_idx].dropna()
                unique_dobs = sorted(str(d.date()) for d in dobs.unique())
                # Different DOBs → different people sharing a name — not a data issue
                if len(unique_dobs) > 1:
                    continue
                dob_note = f" | DOB: {unique_dobs[0]}" if unique_dobs else ''
            else:
                dob_note = ''

            issues.append(dict(
                check='duplicate_name',
                severity='WARNING',
                field=self._NAME_FIELD,
                record_count=count,
                detail=f"{count} record(s) share the name '{name}'{dob_note}.",
                affected_subjids=self._subjids_for_mask(df, matched_idx),
                affected_tablets=self._tablets_for_mask(df, matched_idx),
            ))
        return issues

    def _check_similar_names(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag pairs of names that are highly similar but not identical AND share
        the same date of birth — the combination strongly indicates the same
        person enrolled twice with a name spelling variation.
        Uses rapidfuzz.process.cdist for vectorised pairwise scoring.
        """
        if self._NAME_FIELD not in df.columns:
            return []
        raw = df[self._NAME_FIELD].dropna()
        raw = raw[raw.astype(str).str.strip() != '']
        if len(raw) < 2:
            return []

        norm = raw.astype(str).str.strip().str.lower()
        names = norm.tolist()
        indices = norm.index.tolist()

        if _RAPIDFUZZ_AVAILABLE:
            threshold = self._NAME_SIMILARITY_THRESHOLD * 100  # rapidfuzz uses 0–100
            scores = fuzz_process.cdist(
                names, names, scorer=fuzz.ratio, score_cutoff=threshold - 1
            )
            n = len(names)
            rows, cols = np.triu_indices(n, k=1)
            pair_scores = scores[rows, cols]
            # < 100 excludes exact matches (handled by _check_duplicate_name)
            above = (pair_scores >= threshold) & (pair_scores < 100)
            pairs = [
                (names[r], names[c], indices[r], indices[c], s / 100)
                for r, c, s in zip(rows[above].tolist(), cols[above].tolist(), pair_scores[above].tolist())
            ]
        else:
            pairs = []
            threshold = self._NAME_SIMILARITY_THRESHOLD
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    if names[i] == names[j]:
                        continue
                    ratio = _difflib.SequenceMatcher(None, names[i], names[j]).ratio()
                    if ratio >= threshold:
                        pairs.append((names[i], names[j], indices[i], indices[j], ratio))

        if not pairs:
            return []

        has_dob = 'dob' in df.columns
        has_tablet = 'tabletnum' in df.columns

        # Pre-compute normalised DOB (date only) for each index so we can
        # filter pairs to those where both records share the same DOB.
        if has_dob:
            dob_series = self._parse_dob(df['dob']).dt.normalize()
        else:
            dob_series = None

        # Sort by similarity descending so highest-risk pairs appear first
        pairs.sort(key=lambda x: x[4], reverse=True)

        has_subjid = 'subjid' in df.columns
        skip_str = str(self.skip_code)

        issues = []
        for name_a, name_b, idx_a, idx_b, sim in pairs:
            # Only flag when DOBs are present and identical — the key signal
            # that two similar names are the same person, not just coincidence.
            if dob_series is not None:
                dob_a = dob_series.get(idx_a)
                dob_b = dob_series.get(idx_b)
                if pd.isna(dob_a) or pd.isna(dob_b) or dob_a != dob_b:
                    continue
                dob_note = f" | DOB A: {dob_a.date()} | DOB B: {dob_b.date()}"
            else:
                dob_note = ''

            # Skip if both records share the same subjid — same participant,
            # different name spellings across snapshots (not a duplicate enrolment).
            if has_subjid:
                def _clean_subjid(idx):
                    v = self._strip_float_suffix(df.at[idx, 'subjid']) if idx in df.index else ''
                    return v if v and v != skip_str and v.lower() != 'nan' else None
                sid_a, sid_b = _clean_subjid(idx_a), _clean_subjid(idx_b)
                if sid_a and sid_b and sid_a == sid_b:
                    continue

            if has_tablet:
                tab_a = df.at[idx_a, 'tabletnum'] if idx_a in df.index else ''
                tab_b = df.at[idx_b, 'tabletnum'] if idx_b in df.index else ''
                tablet_note = f" | Tablet: {tab_a} / {tab_b}"
            else:
                tablet_note = ''

            issues.append(dict(
                check='similar_name',
                severity='WARNING',
                field=self._NAME_FIELD,
                record_count=2,
                detail=(
                    f"Similar names: '{name_a}' / '{name_b}' "
                    f"(similarity={sim:.2f}){dob_note}{tablet_note}"
                ),
                affected_subjids=self._subjids_for_mask(df, [idx_a, idx_b]),
                affected_tablets=self._tablets_for_mask(df, [idx_a, idx_b]),
            ))
        return issues

    # ------------------------------------------------------------------
    # Temporal and logical checks (14–17)
    # ------------------------------------------------------------------

    def _check_interview_duration(self, df: pd.DataFrame) -> list[dict]:
        """
        Check for impossible, suspiciously short, or suspiciously long
        interview durations using starttime and stoptime.
        """
        issues = []
        if 'starttime' not in df.columns or 'stoptime' not in df.columns:
            return issues

        start = pd.to_datetime(df['starttime'], errors='coerce', format='%d/%m/%Y %H:%M:%S')
        stop = pd.to_datetime(df['stoptime'], errors='coerce', format='%d/%m/%Y %H:%M:%S')
        both_valid = start.notna() & stop.notna()
        delta_minutes = (stop - start).dt.total_seconds() / 60

        impossible = both_valid & (delta_minutes < 0)
        if impossible.any():
            examples = [f"{m:.1f} min" for m in delta_minutes[impossible].head(5)]
            issues.append(dict(
                check='impossible_duration',
                severity='ERROR',
                field='stoptime',
                record_count=int(impossible.sum()),
                detail=(
                    f"{impossible.sum()} record(s) have stoptime before starttime "
                    f"(negative duration). Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, impossible),
                affected_tablets=self._tablets_for_mask(df, impossible),
            ))

        skip_str = str(self.skip_code)
        if 'subjid' in df.columns:
            _subjid_str = df['subjid'].map(self._strip_float_suffix)
            enrolled = (
                df['subjid'].notna()
                & (_subjid_str != '')
                & (_subjid_str != skip_str)
            )
        else:
            enrolled = pd.Series(False, index=df.index)
        too_short = both_valid & (delta_minutes >= 0) & (delta_minutes < 8) & enrolled
        if too_short.any():
            examples = [f"{m:.1f} min" for m in delta_minutes[too_short].head(5)]
            issues.append(dict(
                check='duration_too_short',
                severity='WARNING',
                field='stoptime',
                record_count=int(too_short.sum()),
                detail=(
                    f"{too_short.sum()} record(s) have an interview duration "
                    f"under 8 minutes — form may not have been completed properly. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, too_short),
                affected_tablets=self._tablets_for_mask(df, too_short),
            ))

        too_long = both_valid & (delta_minutes > 30)
        if too_long.any():
            examples = [f"{m:.1f} min" for m in delta_minutes[too_long].head(5)]
            issues.append(dict(
                check='duration_too_long',
                severity='WARNING',
                field='stoptime',
                record_count=int(too_long.sum()),
                detail=(
                    f"{too_long.sum()} record(s) have an interview duration "
                    f"over 30 minutes — tablet may have been left open. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, too_long),
                affected_tablets=self._tablets_for_mask(df, too_long),
            ))

        return issues

    def _check_dob_age_consistency(self, df: pd.DataFrame) -> list[dict]:
        """
        Validate date of birth against eligibility rules and recorded age.
        Checks for future DOB, DOB implying ineligible age, and DOB/age mismatch.
        """
        issues = []
        if 'dob' not in df.columns:
            return issues

        today = pd.Timestamp.now().normalize()
        dob = self._parse_dob(df['dob'])
        valid_dob = dob.notna()

        future = valid_dob & (dob > today)
        if future.any():
            examples = [str(d.date()) for d in dob[future].dropna().head(5)]
            issues.append(dict(
                check='future_dob',
                severity='ERROR',
                field='dob',
                record_count=int(future.sum()),
                detail=(
                    f"{future.sum()} record(s) have a date of birth in the future. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, future),
                affected_tablets=self._tablets_for_mask(df, future),
            ))

        derived_age = (today - dob).dt.days / 365.25
        ineligible = valid_dob & ~future & ((derived_age < 10) | (derived_age > 110))
        if ineligible.any():
            examples = [
                f"{dob[i].date()} (age {derived_age[i]:.1f})"
                for i in dob[ineligible].dropna().head(5).index
            ]
            issues.append(dict(
                check='dob_eligibility',
                severity='ERROR',
                field='dob',
                record_count=int(ineligible.sum()),
                detail=(
                    f"{ineligible.sum()} record(s) have a dob that implies an age "
                    f"outside the eligible range 10-110 years. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, ineligible),
                affected_tablets=self._tablets_for_mask(df, ineligible),
            ))

        if 'respondants_age' in df.columns:
            recorded_age = pd.to_numeric(df['respondants_age'], errors='coerce')
            usable = valid_dob & recorded_age.notna() & (recorded_age != self.skip_code) & (recorded_age != _DONT_KNOW)
            age_diff = (derived_age - recorded_age).abs()
            mismatch = usable & (age_diff > 2)
            if mismatch.any():
                examples = [
                    f"dob={dob[i].date()}, derived={derived_age[i]:.1f}, recorded={int(recorded_age[i])}"
                    for i in dob[mismatch].dropna().head(5).index
                ]
                issues.append(dict(
                    check='dob_age_mismatch',
                    severity='WARNING',
                    field='dob',
                    record_count=int(mismatch.sum()),
                    detail=(
                        f"{mismatch.sum()} record(s) have a discrepancy of more than "
                        f"2 years between dob and respondants_age. "
                        f"Examples: {examples}"
                    ),
                    affected_subjids=self._subjids_for_mask(df, mismatch),
                affected_tablets=self._tablets_for_mask(df, mismatch),
                ))

        return issues

    def _check_visit_date(self, df: pd.DataFrame) -> list[dict]:
        """
        Check for future or stale visit dates using starttime.
        vdate mirrors starttime and is not checked separately.
        """
        issues = []
        if 'starttime' not in df.columns:
            return issues

        today = pd.Timestamp.now().normalize()
        stale_cutoff = today - pd.DateOffset(months=12)
        start = pd.to_datetime(df['starttime'], errors='coerce', format='%d/%m/%Y %H:%M:%S')
        valid_start = start.notna()

        future = valid_start & (start.dt.normalize() > today)
        if future.any():
            examples = [str(d.date()) for d in start[future].dropna().head(5)]
            issues.append(dict(
                check='future_visit_date',
                severity='ERROR',
                field='starttime',
                record_count=int(future.sum()),
                detail=(
                    f"{future.sum()} record(s) have a visit date in the future. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, future),
                affected_tablets=self._tablets_for_mask(df, future),
            ))

        stale = valid_start & (start.dt.normalize() < stale_cutoff)
        if stale.any():
            examples = [str(d.date()) for d in start[stale].dropna().head(5)]
            issues.append(dict(
                check='stale_visit_date',
                severity='WARNING',
                field='starttime',
                record_count=int(stale.sum()),
                detail=(
                    f"{stale.sum()} record(s) have a visit date more than "
                    f"12 months in the past — possible incomplete sync. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, stale),
                affected_tablets=self._tablets_for_mask(df, stale),
            ))

        return issues

    def _check_appointment_dates(self, df: pd.DataFrame) -> list[dict]:
        """
        Check that follow-up appointment dates are after the visit date and
        fall within a clinically plausible window.
          next_appt_3m : 60–120 days after starttime
          next_appt_6m : 150–210 days after starttime
        """
        issues = []
        if 'starttime' not in df.columns:
            return issues

        visit = pd.to_datetime(df['starttime'], errors='coerce', format='%d/%m/%Y %H:%M:%S')
        valid_visit = visit.notna()

        appt_windows = {
            'next_appt_3m': (60, 120),
            'next_appt_6m': (150, 210),
        }

        for col, (min_days, max_days) in appt_windows.items():
            if col not in df.columns:
                continue

            appt = pd.to_datetime(df[col], errors='coerce', format='%d/%m/%Y %H:%M:%S')
            both = valid_visit & appt.notna()

            before_visit = both & (appt < visit)
            if before_visit.any():
                issues.append(dict(
                    check='appointment_before_visit',
                    severity='ERROR',
                    field=col,
                    record_count=int(before_visit.sum()),
                    detail=(
                        f"{before_visit.sum()} record(s) have '{col}' earlier than "
                        f"starttime — appointment cannot precede the visit."
                    ),
                    affected_subjids=self._subjids_for_mask(df, before_visit),
                    affected_tablets=self._tablets_for_mask(df, before_visit),
                ))

            delta_days = (appt - visit).dt.days
            outside_window = both & ~before_visit & (
                (delta_days < min_days) | (delta_days > max_days)
            )
            if outside_window.any():
                issues.append(dict(
                    check='appointment_interval_unexpected',
                    severity='WARNING',
                    field=col,
                    record_count=int(outside_window.sum()),
                    detail=(
                        f"{outside_window.sum()} record(s) have '{col}' outside "
                        f"the expected {min_days}-{max_days} day window after starttime."
                    ),
                    affected_subjids=self._subjids_for_mask(df, outside_window),
                    affected_tablets=self._tablets_for_mask(df, outside_window),
                ))

        return issues

    # ------------------------------------------------------------------
    # Coding / consent integrity checks (18–19)
    # ------------------------------------------------------------------

    def _check_consent_flow(self, df: pd.DataFrame) -> list[dict]:
        """
        Check for invalid consent codes and non-consented records that have
        been incorrectly assigned a subject ID.
        """
        issues = []
        if 'consent' not in df.columns:
            return issues

        consent = pd.to_numeric(df['consent'], errors='coerce')
        valid_codes = {0, 1, self.skip_code}

        has_consent = consent.notna()
        invalid_code = has_consent & ~consent.isin(valid_codes)
        if invalid_code.any():
            bad_vals = sorted(consent[invalid_code].unique().tolist())
            issues.append(dict(
                check='invalid_consent_code',
                severity='ERROR',
                field='consent',
                record_count=int(invalid_code.sum()),
                detail=(
                    f"{invalid_code.sum()} record(s) have an unrecognised consent "
                    f"code: {bad_vals}. Expected: {sorted(valid_codes)}."
                ),
                affected_subjids=self._subjids_for_mask(df, invalid_code),
                affected_tablets=self._tablets_for_mask(df, invalid_code),
            ))

        if 'subjid' in df.columns:
            has_subjid = (
                df['subjid'].notna()
                & (df['subjid'].astype(str).str.strip() != '')
                & (df['subjid'].astype(str).str.strip().str.lower() != 'nan')
            )
            bad = (consent == 0) & has_subjid
            if bad.any():
                issues.append(dict(
                    check='non_consented_with_subjid',
                    severity='ERROR',
                    field='subjid',
                    record_count=int(bad.sum()),
                    detail=(
                        f"{bad.sum()} record(s) with consent=0 have a subjid — "
                        f"subjid should only be assigned to consented participants."
                    ),
                    affected_subjids=self._subjids_for_mask(df, bad),
                    affected_tablets=self._tablets_for_mask(df, bad),
                ))

        return issues

    def _check_client_sex(self, df: pd.DataFrame) -> list[dict]:
        """
        Check for invalid sex codes and consented participants with no sex recorded.
        Valid codes: 1 (male), 2 (female), skip_code.
        """
        issues = []
        if 'client_sex' not in df.columns:
            return issues

        sex = pd.to_numeric(df['client_sex'], errors='coerce')
        valid_codes = {1, 2, self.skip_code}

        invalid_code = sex.notna() & ~sex.isin(valid_codes)
        if invalid_code.any():
            bad_vals = sorted(sex[invalid_code].unique().tolist())
            issues.append(dict(
                check='invalid_sex_code',
                severity='ERROR',
                field='client_sex',
                record_count=int(invalid_code.sum()),
                detail=(
                    f"{invalid_code.sum()} record(s) have an unrecognised client_sex "
                    f"code: {bad_vals}. Expected: 1 (male), 2 (female), "
                    f"{self.skip_code} (skip)."
                ),
                affected_subjids=self._subjids_for_mask(df, invalid_code),
                affected_tablets=self._tablets_for_mask(df, invalid_code),
            ))

        if 'consent' in df.columns:
            consent = pd.to_numeric(df['consent'], errors='coerce')
            consented = consent == 1
            missing_sex = sex.isna() | (sex == self.skip_code)
            bad = consented & missing_sex
            if bad.any():
                issues.append(dict(
                    check='missing_sex_for_consented',
                    severity='WARNING',
                    field='client_sex',
                    record_count=int(bad.sum()),
                    detail=(
                        f"{bad.sum()} consented record(s) have no valid sex code "
                        f"(null or system-skipped)."
                    ),
                    affected_subjids=self._subjids_for_mask(df, bad),
                affected_tablets=self._tablets_for_mask(df, bad),
                ))

        return issues

    # ------------------------------------------------------------------
    # Operational signal checks (20–23)
    # ------------------------------------------------------------------

    def _check_interviewer_productivity(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag interviewers with an implausibly high number of interviews on a
        single day (> 15), and records with a starttime between midnight and
        04:59 (possible tablet clock error or backdating).
        """
        issues = []
        if 'interviewer_id' not in df.columns or 'starttime' not in df.columns:
            return issues

        start = pd.to_datetime(df['starttime'], errors='coerce', format='%d/%m/%Y %H:%M:%S')
        valid_start = start.notna() & df['interviewer_id'].notna()

        work_df = df.loc[valid_start, ['interviewer_id']].copy()
        work_df['_date'] = start[valid_start].dt.normalize()
        daily_counts = work_df.groupby(
            ['interviewer_id', '_date']
        )['interviewer_id'].transform('count')

        excessive = pd.Series(False, index=df.index)
        excessive[valid_start] = daily_counts > 25
        if excessive.any():
            issues.append(dict(
                check='excessive_daily_interviews',
                severity='WARNING',
                field='interviewer_id',
                record_count=int(excessive.sum()),
                detail=(
                    f"{excessive.sum()} record(s) belong to an interviewer-day "
                    f"combination with more than 25 interviews."
                ),
                affected_subjids=self._subjids_for_mask(df, excessive),
                affected_tablets=self._tablets_for_mask(df, excessive),
            ))

        unusual_hour = start.notna() & start.dt.hour.between(0, 4)
        if unusual_hour.any():
            examples = [str(t) for t in start[unusual_hour].head(5)]
            issues.append(dict(
                check='unusual_interview_hour',
                severity='WARNING',
                field='starttime',
                record_count=int(unusual_hour.sum()),
                detail=(
                    f"{unusual_hour.sum()} record(s) have a starttime between "
                    f"midnight and 04:59 — possible backdating or clock error. "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, unusual_hour),
                affected_tablets=self._tablets_for_mask(df, unusual_hour),
            ))

        return issues

    def _check_screening_id_format(
        self, df: pd.DataFrame, country_name: str
    ) -> list[dict]:
        """
        Check that screening IDs contain only permitted characters and start
        with the expected country prefix (KE for Kenya, UG for Uganda).
        """
        issues = []
        if 'screening_id' not in df.columns:
            return issues

        raw = df['screening_id'].dropna()
        raw = raw[raw.astype(str).str.strip() != '']
        if raw.empty:
            return issues

        sid = raw.astype(str).str.strip()

        bad_format = ~sid.str.match(self._SCREENING_ID_RE)
        if bad_format.any():
            examples = sid[bad_format].head(5).tolist()
            issues.append(dict(
                check='invalid_screening_id_format',
                severity='ERROR',
                field='screening_id',
                record_count=int(bad_format.sum()),
                detail=(
                    f"{bad_format.sum()} screening_id(s) contain invalid characters "
                    f"(only A-Z, a-z, 0-9, _, - are permitted). "
                    f"Examples: {examples}"
                ),
                affected_subjids=self._subjids_for_mask(df, bad_format[bad_format].index),
                affected_tablets=self._tablets_for_mask(df, bad_format[bad_format].index),
            ))

        expected_prefix = self._COUNTRY_ID_PREFIXES.get(
            country_name.lower() if country_name else '', None
        )
        if expected_prefix:
            wrong_prefix = ~sid.str.startswith(expected_prefix)
            if wrong_prefix.any():
                examples = sid[wrong_prefix].head(5).tolist()
                issues.append(dict(
                    check='screening_id_country_mismatch',
                    severity='ERROR',
                    field='screening_id',
                    record_count=int(wrong_prefix.sum()),
                    detail=(
                        f"{wrong_prefix.sum()} screening_id(s) do not start with "
                        f"the expected prefix '{expected_prefix}' for {country_name}. "
                        f"Examples: {examples}"
                    ),
                    affected_subjids=self._subjids_for_mask(df, wrong_prefix[wrong_prefix].index),
                affected_tablets=self._tablets_for_mask(df, wrong_prefix[wrong_prefix].index),
                ))

        return issues

    def _check_tablet_record_counts(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag tablets that contributed only 1 or 2 records — a possible sign
        of data loss or an incomplete sync. Uses the _source_db column added
        by AccessReader.
        """
        issues = []
        if '_source_db' not in df.columns:
            return issues

        tablet_labels = df['_source_db'].dropna().map(
            lambda p: os.path.basename(os.path.dirname(str(p)))
        )
        if tablet_labels.empty:
            return issues

        counts = tablet_labels.value_counts()
        low = counts[counts <= 2]
        if low.empty:
            return issues

        issues.append(dict(
            check='low_tablet_record_count',
            severity='WARNING',
            field='_source_db',
            record_count=int(low.sum()),
            detail=(
                f"{len(low)} tablet(s) contributed 2 or fewer records: "
                f"{low.index.tolist()}. Possible test device or incomplete sync."
            ),
            affected_subjids='',
        ))
        return issues

    def _check_record_completeness(self, df: pd.DataFrame) -> list[dict]:
        """
        Flag columns where more than 50% of records are null. Each affected
        column gets its own issue row so callers can see exactly which fields
        are sparse.
        """
        issues = []
        if df.empty:
            return issues

        data_cols = [
            c for c in df.columns
            if not c.startswith('_') and c not in self._COMPLETENESS_EXCLUDE
        ]
        for col in data_cols:
            null_ratio = df[col].isna().mean()
            if null_ratio > 0.5:
                n_null = int(df[col].isna().sum())
                issues.append(dict(
                    check='sparse_column',
                    severity='WARNING',
                    field=col,
                    record_count=n_null,
                    detail=(
                        f"Column '{col}' is null in {null_ratio:.1%} of records "
                        f"({n_null}/{len(df)})."
                    ),
                    affected_subjids='',
                ))
        return issues
