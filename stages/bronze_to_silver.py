from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pandas as pd

from modules.data_cleaner import DataCleaner
from stages.base import BaseStage, StageResult

logger = logging.getLogger(__name__)


class BronzeToSilver(BaseStage):
    name = 'bronze_to_silver'
    dependencies: list[str] = ['mdb_to_bronze']

    def run(self) -> StageResult:
        trial = self.config.get('trial')
        dedup_key = trial['dedup_key']
        country_code_map: dict[str, int] = trial.get('country_code_map', {})

        errors: list[str] = []
        total_written = 0

        n, errs = self._process_table('baseline', dedup_key, country_code_map)
        total_written += n
        errors.extend(errs)

        n, errs = self._process_table('followup', dedup_key, country_code_map)
        total_written += n
        errors.extend(errs)

        return StageResult(success=len(errors) == 0, rows_written=total_written, errors=errors)

    def _process_table(
        self,
        table_name: str,
        dedup_key: str,
        country_code_map: dict[str, int],
    ) -> tuple[int, list[str]]:
        """Clean bronze_ibis.<table_name> → silver_ibis.<table_name>. Returns (rows_written, errors)."""
        bronze_df = pd.read_sql(f'SELECT * FROM bronze_ibis.{table_name}', self.engine)

        if bronze_df.empty:
            logger.warning(f"bronze_ibis.{table_name} is empty — skipping.")
            return 0, []

        logger.info(f"Read {len(bronze_df)} rows from bronze_ibis.{table_name}.")

        errors: list[str] = []
        all_cleaned: list[pd.DataFrame] = []

        for country, group in bronze_df.groupby('country'):
            try:
                country_code = country_code_map.get(str(country))
                cleaner = DataCleaner(group.copy())

                if country_code is not None:
                    df = cleaner.filter_by_countrycode(country_code)
                    cleaner = DataCleaner(df)
                else:
                    logger.warning(
                        f"[{country}] No country code for '{table_name}'; skipping country filter."
                    )
                    df = group.copy()

                df = cleaner.drop_exact_duplicates()
                cleaner = DataCleaner(df)

                if dedup_key in df.columns:
                    if dedup_key != 'uniqueid':
                        df = df.rename(columns={dedup_key: 'uniqueid'})
                        df = DataCleaner(df).deduplicate_by_uniqueid()
                        df = df.rename(columns={'uniqueid': dedup_key})
                    else:
                        df = DataCleaner(df).deduplicate_by_uniqueid()
                else:
                    logger.warning(
                        f"[{country}] Dedup key '{dedup_key}' not found in {table_name}."
                    )

                all_cleaned.append(df)
                logger.info(f"[{country}/{table_name}] {len(df)} rows after deduplication.")
            except Exception as exc:
                msg = f"[{country}] Failed during silver processing of {table_name}: {exc}"
                logger.error(msg)
                errors.append(msg)

        if not all_cleaned:
            return 0, errors

        silver_df = pd.concat(all_cleaned, ignore_index=True)
        silver_df = silver_df.drop(columns=['_source_db'], errors='ignore')

        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        meta = pd.DataFrame([{
            'run_uuid': run_id,
            'file_name': f'(silver consolidation — {table_name})',
            'file_path': '',
            'country': '(all)',
            'community': '(all)',
            'extracted_at': now,
            'last_modified': now,
            'loaded': True,
        }])

        with self.engine.begin() as conn:
            silver_df.to_sql(table_name, conn, schema='silver_ibis', if_exists='replace', index=False)
            meta.to_sql('meta', conn, schema='silver_ibis', if_exists='append', index=False)

        logger.info(f"Wrote {len(silver_df)} rows → silver_ibis.{table_name}.")
        return len(silver_df), errors
