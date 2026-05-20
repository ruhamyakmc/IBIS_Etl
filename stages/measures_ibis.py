from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from modules.data_validator import DataValidator
from stages.base import BaseStage, StageResult

logger = logging.getLogger(__name__)

SQL_MEASURES_DIR = os.path.join(os.path.dirname(__file__), '..', 'sql', 'measures')

# Country name → (facility column, code→name map)
_FACILITY_CONFIG: dict[str, tuple[str, dict]] = {
    'kenya':  ('health_facility_ke', DataValidator._FACILITY_CODES_KE),
    'uganda': ('health_facility_ug', DataValidator._FACILITY_CODES_UG),
}


def _load_sql_files(directory: str) -> list[Path]:
    return sorted(Path(directory).glob('*.sql'))


def _facility_name(code, code_map: dict) -> str:
    """Return a decoded facility label, e.g. '11 (Bushenyi HCIV)'."""
    try:
        icode = int(float(str(code)))
    except (ValueError, TypeError):
        return str(code)
    return f"{icode} ({code_map.get(icode, 'Unknown')})"


class MeasuresIbis(BaseStage):
    name = 'measures_ibis'
    dependencies: list[str] = ['transform_ibis']

    def run(self) -> StageResult:
        trial = self.config.get('trial')
        country_code_map: dict[str, int] = trial.get('country_code_map', {})

        silver_df = pd.read_sql('SELECT * FROM silver_ibis.baseline', self.engine)

        if silver_df.empty:
            logger.warning("silver_ibis.baseline is empty — skipping measures.")
            return StageResult(success=True, rows_written=0)

        errors: list[str] = []
        all_reports: list[pd.DataFrame] = []

        for country, country_group in silver_df.groupby('country'):
            country_str = str(country)
            country_code = country_code_map.get(country_str)
            if country_code is None:
                logger.warning(
                    f"[{country_str}] No country_code in config — "
                    f"countrycode mismatch check skipped."
                )

            fac_field, fac_codes = _FACILITY_CONFIG.get(country_str, (None, {}))

            # Run identity checks (duplicate phone/name/subjid) across the full
            # country dataset so cross-facility duplicates are not missed.
            try:
                id_validator = DataValidator()
                id_report = id_validator.validate(
                    country_group.copy(),
                    country_code=country_code,
                    country_name=country_str,
                    site_name='',
                    skip_identity=False,
                )
                # Keep only the identity-check rows to avoid duplicating other checks.
                _IDENTITY_CHECKS = {
                    'duplicate_subjid', 'duplicate_phone', 'similar_phone',
                    'duplicate_name', 'similar_name',
                }
                id_report = id_report[id_report['check'].isin(_IDENTITY_CHECKS)]
                if not id_report.empty:
                    all_reports.append(id_report)
            except Exception as exc:
                logger.error(f"[{country_str}] Country-level identity check failed: {exc}")
                errors.append(f"[{country_str}] Country-level identity check failed: {exc}")

            # Build (site_name, sub-group) pairs — one per health facility
            if fac_field and fac_field in country_group.columns:
                fac_col = pd.to_numeric(country_group[fac_field], errors='coerce')
                tmp = country_group.copy()
                tmp['_fac'] = fac_col

                site_groups: list[tuple[str, pd.DataFrame]] = []
                for fac_code, fac_group in tmp.groupby('_fac', dropna=False):
                    if pd.isna(fac_code):
                        site = '(Unknown facility)'
                    else:
                        site = _facility_name(fac_code, fac_codes)
                    site_groups.append((site, fac_group.drop(columns=['_fac'])))
            else:
                # No facility breakdown available — validate whole country as one group
                site_groups = [('', country_group)]

            for site, group in site_groups:
                label = f"{country_str}/{site}" if site else country_str
                try:
                    validator = DataValidator()
                    report = validator.validate(
                        group.copy(),
                        country_code=country_code,
                        country_name=country_str,
                        site_name=site,
                        skip_identity=True,
                    )
                    all_reports.append(report)
                except Exception as exc:
                    msg = f"[{label}] Validation failed: {exc}"
                    logger.error(msg)
                    errors.append(msg)

        if not all_reports:
            logger.warning("All validations failed — skipping report write.")
            return StageResult(success=False, rows_written=0, errors=errors)

        full_report = pd.concat(all_reports, ignore_index=True)
        full_report.to_sql(
            'ds_validation_report', self.engine, schema='gold_ibis',
            if_exists='replace', index=False,
        )
        logger.info(
            f"Wrote {len(full_report)} validation issue(s) → gold_ibis.ds_validation_report."
        )

        # Run measures SQL files
        sql_files = _load_sql_files(SQL_MEASURES_DIR)
        if not sql_files:
            msg = f"No SQL files found in '{SQL_MEASURES_DIR}'."
            logger.error(msg)
            errors.append(msg)
            return StageResult(success=False, rows_written=len(full_report), errors=errors)
        with self.engine.begin() as conn:
            for sql_path in sql_files:
                sql = sql_path.read_text()
                try:
                    conn.execute(text(sql))
                    logger.info(f"Executed: {sql_path.name}")
                except Exception as exc:
                    msg = f"SQL error in '{sql_path.name}': {exc}"
                    logger.error(msg)
                    errors.append(msg)
                    raise

        return StageResult(success=len(errors) == 0, rows_written=len(full_report), errors=errors)
