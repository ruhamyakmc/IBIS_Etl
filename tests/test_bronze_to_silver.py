import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from stages.bronze_to_silver import BronzeToSilver


def _make_config(dedup_key='uniqueid', strategy='latest_snapshot'):
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        'trial': {
            'dedup_key': dedup_key,
            'dedup_strategy': strategy,
            'country_code_map': {'kenya': 2, 'uganda': 1},
        },
    }.get(key, default)
    return config


def test_bronze_to_silver_deduplicates():
    """Stage deduplicates bronze rows and writes to silver."""
    baseline_raw = pd.DataFrame({
        'uniqueid': ['a', 'a', 'b'],
        'countrycode': [2, 2, 2],
        '_source_db': ['x', 'x', 'y'],
        'run_uuid': ['r1', 'r1', 'r2'],
        'file_name': ['f1', 'f1', 'f2'],
        'file_path': ['p1', 'p1', 'p2'],
        'country': ['kenya', 'kenya', 'kenya'],
        'community': ['Sindo', 'Sindo', 'Sindo'],
        'extracted_at': [None, None, None],
    })

    engine = MagicMock()

    def fake_read_sql(query, engine):
        if 'followup' in query:
            return pd.DataFrame()
        return baseline_raw

    # patch DataFrame.to_sql to a no-op (avoids needing a real DB)
    with patch.object(pd.DataFrame, 'to_sql'):
        with patch('stages.bronze_to_silver.pd.read_sql', side_effect=fake_read_sql):
            stage = BronzeToSilver(config=_make_config(), engine=engine)
            result = stage.run()

    assert result.success
    # 2 unique uniqueid values → 2 rows written (followup is empty, contributes 0)
    assert result.rows_written == 2


def test_bronze_to_silver_processes_followup():
    """Stage also cleans bronze_ibis.followup → silver_ibis.followup."""
    baseline_raw = pd.DataFrame({
        'uniqueid': ['a'],
        'countrycode': [1],
        'country': ['uganda'],
        'community': ['Mbarara'],
        'extracted_at': [None],
        'run_uuid': ['r1'],
        'file_name': ['f1'],
        'file_path': ['p1'],
    })
    followup_raw = pd.DataFrame({
        'uniqueid': ['a', 'a'],
        'countrycode': [1, 1],
        'country': ['uganda', 'uganda'],
        'community': ['Mbarara', 'Mbarara'],
        'extracted_at': [None, None],
        'run_uuid': ['r1', 'r1'],
        'file_name': ['f1', 'f1'],
        'file_path': ['p1', 'p1'],
    })

    engine = MagicMock()
    written: dict[str, pd.DataFrame] = {}

    def fake_to_sql(self, name, conn, schema=None, **kwargs):
        written[f"{schema}.{name}"] = self.copy()

    def fake_read_sql(query, engine):
        if 'followup' in query:
            return followup_raw
        return baseline_raw

    with patch('stages.bronze_to_silver.pd.read_sql', side_effect=fake_read_sql), \
         patch.object(pd.DataFrame, 'to_sql', fake_to_sql):
        stage = BronzeToSilver(config=_make_config(), engine=engine)
        result = stage.run()

    assert result.success
    assert result.rows_written == 2
    assert 'silver_ibis.followup' in written
    # 2 duplicate uniqueid rows → deduped to 1
    assert len(written['silver_ibis.followup']) == 1


def test_bronze_to_silver_succeeds_when_followup_empty():
    """Stage succeeds (with warning) if bronze_ibis.followup is empty."""
    baseline_raw = pd.DataFrame({
        'uniqueid': ['a'],
        'countrycode': [1],
        'country': ['uganda'],
        'community': ['Mbarara'],
        'extracted_at': [None],
        'run_uuid': ['r1'],
        'file_name': ['f1'],
        'file_path': ['p1'],
    })

    engine = MagicMock()

    def fake_read_sql(query, engine):
        if 'followup' in query:
            return pd.DataFrame()
        return baseline_raw

    with patch('stages.bronze_to_silver.pd.read_sql', side_effect=fake_read_sql), \
         patch.object(pd.DataFrame, 'to_sql'):
        stage = BronzeToSilver(config=_make_config(), engine=engine)
        result = stage.run()

    assert result.success
    assert result.rows_written == 1
