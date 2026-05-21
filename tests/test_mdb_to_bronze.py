import pandas as pd
from unittest.mock import MagicMock, patch

from stages.mdb_to_bronze import MdbToBronze


def _make_config(access_table_name='baseline'):
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        'communities': {
            'ug1': {'country': 'uganda', 'community_name': 'Mbarara'},
        },
        'trial': {'country_code_map': {'uganda': 1}},
        'access_table_name': access_table_name,
        'excluded_tablets': [],
    }.get(key, default)
    return config


def test_ingest_file_uses_table_name_in_to_sql():
    """_ingest_file writes to bronze_ibis.<table_name>, not hardcoded 'baseline'."""
    engine = MagicMock()

    from sqlalchemy.exc import ProgrammingError
    connect_ctx = MagicMock()
    connect_ctx.__enter__ = MagicMock(return_value=MagicMock(
        execute=MagicMock(side_effect=ProgrammingError('', {}, Exception()))
    ))
    connect_ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = connect_ctx

    begin_ctx = MagicMock()
    begin_conn = MagicMock()
    begin_ctx.__enter__ = MagicMock(return_value=begin_conn)
    begin_ctx.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value = begin_ctx

    raw = pd.DataFrame({'uniqueid': ['x'], 'subjid': ['s1']})

    with patch('stages.mdb_to_bronze.read_mdb_table', return_value=raw), \
         patch('os.path.getmtime', return_value=0.0), \
         patch.object(pd.DataFrame, 'to_sql') as mock_to_sql:
        stage = MdbToBronze(config=_make_config(), engine=engine)
        stage._ingest_file('/fake/tablet.mdb', 'followup', 'uganda', 'Mbarara')

    # First to_sql call is for the data; second is for meta
    data_call = mock_to_sql.call_args_list[0]
    assert data_call.args[0] == 'followup'
    assert data_call.kwargs.get('schema') == 'bronze_ibis'


def test_ingest_file_writes_table_name_to_meta():
    """Meta row includes table_name so baseline and followup are distinguished."""
    engine = MagicMock()

    from sqlalchemy.exc import ProgrammingError
    connect_ctx = MagicMock()
    connect_ctx.__enter__ = MagicMock(return_value=MagicMock(
        execute=MagicMock(side_effect=ProgrammingError('', {}, Exception()))
    ))
    connect_ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = connect_ctx

    begin_ctx = MagicMock()
    begin_conn = MagicMock()
    begin_ctx.__enter__ = MagicMock(return_value=begin_conn)
    begin_ctx.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value = begin_ctx

    raw = pd.DataFrame({'uniqueid': ['x']})
    captured = {}

    def patched_to_sql(self, name, conn, schema=None, **kwargs):
        if name == 'meta':
            captured['meta_df'] = self.copy()

    with patch('stages.mdb_to_bronze.read_mdb_table', return_value=raw), \
         patch('os.path.getmtime', return_value=0.0), \
         patch.object(pd.DataFrame, 'to_sql', patched_to_sql):
        stage = MdbToBronze(config=_make_config(), engine=engine)
        stage._ingest_file('/fake/tablet.mdb', 'followup', 'uganda', 'Mbarara')

    assert 'table_name' in captured['meta_df'].columns
    assert captured['meta_df']['table_name'].iloc[0] == 'followup'
