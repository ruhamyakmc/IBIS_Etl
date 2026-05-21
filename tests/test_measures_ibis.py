import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from stages.measures_ibis import MeasuresIbis


def test_measures_ibis_writes_validator_report(monkeypatch):
    silver_df = pd.DataFrame({
        'uniqueid': ['a', 'b'],
        'countrycode': [2, 2],
        'tabletnum': [221, 221],
        'screening_id': ['KE001', 'KE002'],
        'starttime': [None, None],
        'stoptime': [None, None],
        'client_sex': [1, 1],
        'health_facility': ['HF1', 'HF1'],
        'country': ['kenya', 'kenya'],
    })

    report_df = pd.DataFrame([{
        'check': 'missing_required',
        'severity': 'WARNING',
        'field': 'starttime',
        'record_count': 2,
        'detail': 'starttime missing',
        'affected_subjids': '',
    }])

    engine = MagicMock()
    mock_conn = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)

    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        'trial': {'country_code_map': {'kenya': 2}},
    }.get(key, default)

    written = {}

    def fake_to_sql(df_self, name, eng=None, schema=None, if_exists='append', index=True):
        written[f"{schema}.{name}"] = True

    with patch('pandas.DataFrame.to_sql', fake_to_sql):
        with patch('stages.measures_ibis.pd.read_sql', return_value=silver_df):
            with patch('stages.measures_ibis.DataValidator') as MockValidator:
                MockValidator.return_value.validate.return_value = report_df
                with patch('stages.measures_ibis.SQL_MEASURES_DIR', '/nonexistent'):
                    mock_sql_path = MagicMock()
                    mock_sql_path.read_text.return_value = 'SELECT 1;'
                    mock_sql_path.name = 'test.sql'
                    with patch('stages.measures_ibis._load_sql_files', return_value=[mock_sql_path]):
                        stage = MeasuresIbis(config=config, engine=engine)
                        result = stage.run()

    assert result.success
    assert 'gold_ibis.ds_validation_report' in written


def test_measures_ibis_warns_on_missing_country_code():
    """When a country has no entry in country_code_map, a warning is logged and validation continues."""
    silver_df = pd.DataFrame({
        'uniqueid': ['a'],
        'countrycode': [9],
        'tabletnum': [100],
        'screening_id': ['XX001'],
        'starttime': [None],
        'stoptime': [None],
        'client_sex': [1],
        'health_facility': ['HF1'],
        'country': ['unknown_country'],
    })

    report_df = pd.DataFrame([{
        'check': 'test_check',
        'severity': 'WARNING',
        'field': 'countrycode',
        'record_count': 1,
        'detail': 'unknown country',
        'affected_subjids': '',
    }])

    engine = MagicMock()
    config = MagicMock()
    # country_code_map is empty — 'unknown_country' will not be found
    config.get.side_effect = lambda key, default=None: {
        'trial': {'country_code_map': {}},
    }.get(key, default)

    written = {}

    def fake_to_sql(df_self, name, eng=None, schema=None, if_exists='append', index=True):
        written[f"{schema}.{name}"] = True

    with patch('pandas.DataFrame.to_sql', fake_to_sql):
        with patch('stages.measures_ibis.pd.read_sql', return_value=silver_df):
            with patch('stages.measures_ibis.DataValidator') as MockValidator:
                MockValidator.return_value.validate.return_value = report_df
                with patch('stages.measures_ibis._load_sql_files', return_value=[]):
                    stage = MeasuresIbis(config=config, engine=engine)
                    result = stage.run()

    # Validation ran (MockValidator was called) and report was written
    MockValidator.return_value.validate.assert_called()
    call_kwargs = MockValidator.return_value.validate.call_args
    # country_code should be None (not found in map)
    assert call_kwargs.kwargs.get('country_code') is None or call_kwargs[1].get('country_code') is None
    assert 'gold_ibis.ds_validation_report' in written
