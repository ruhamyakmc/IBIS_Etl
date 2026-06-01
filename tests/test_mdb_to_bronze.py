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


def test_run_ingests_followup_when_table_present():
    """run() ingests followup from MDB files that have a followup table."""
    config = _make_config()
    engine = MagicMock()

    ingested_tables: list[str] = []

    def fake_ingest(db_path, table_name, country, community):
        ingested_tables.append(table_name)
        return 1

    with patch('stages.mdb_to_bronze.get_country_paths',
               return_value={'extract_path': '/fake'}), \
         patch('stages.mdb_to_bronze.glob_module.glob', return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.select_latest_per_tablet',
               return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.list_mdb_tables',
               return_value=['baseline', 'followup']), \
         patch.object(MdbToBronze, '_ingest_file', side_effect=fake_ingest):
        stage = MdbToBronze(config=config, engine=engine)
        result = stage.run()

    assert 'baseline' in ingested_tables
    assert 'followup' in ingested_tables
    assert result.success


def test_run_skips_followup_when_table_absent():
    """run() does not fail when followup table is missing from an MDB file."""
    config = _make_config()
    engine = MagicMock()

    ingested_tables: list[str] = []

    def fake_ingest(db_path, table_name, country, community):
        ingested_tables.append(table_name)
        return 1

    with patch('stages.mdb_to_bronze.get_country_paths',
               return_value={'extract_path': '/fake'}), \
         patch('stages.mdb_to_bronze.glob_module.glob', return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.select_latest_per_tablet',
               return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.list_mdb_tables',
               return_value=['baseline']), \
         patch.object(MdbToBronze, '_ingest_file', side_effect=fake_ingest):
        stage = MdbToBronze(config=config, engine=engine)
        result = stage.run()

    assert 'baseline' in ingested_tables
    assert 'followup' not in ingested_tables
    assert result.success


def test_run_quarantines_corrupt_mdb_and_continues():
    """run() moves a corrupt MDB's tablet folder to Quarantine/ and succeeds."""
    config = _make_config()
    engine = MagicMock()

    def fake_ingest(db_path, table_name, country, community):
        raise RuntimeError("mdb-export failed for 'IBIS_pilot.mdb': offset 4096 is beyond EOF")

    with patch('stages.mdb_to_bronze.get_country_paths',
               return_value={'extract_path': '/fake/Extracted/Uganda'}), \
         patch('stages.mdb_to_bronze.glob_module.glob',
               return_value=['/fake/Extracted/Uganda/Tablet53_2026_05_28/IBIS_pilot.mdb']), \
         patch('stages.mdb_to_bronze.select_latest_per_tablet',
               return_value=['/fake/Extracted/Uganda/Tablet53_2026_05_28/IBIS_pilot.mdb']), \
         patch('stages.mdb_to_bronze.list_mdb_tables', return_value=[]), \
         patch.object(MdbToBronze, '_ingest_file', side_effect=fake_ingest), \
         patch('stages.mdb_to_bronze.os.makedirs'), \
         patch('stages.mdb_to_bronze.shutil') as mock_shutil:
        stage = MdbToBronze(config=config, engine=engine)
        result = stage.run()

    assert result.success
    mock_shutil.move.assert_called_once_with(
        '/fake/Extracted/Uganda/Tablet53_2026_05_28',
        '/fake/Extracted/Uganda/Quarantine/Tablet53_2026_05_28',
    )


def test_run_continues_when_list_mdb_tables_raises():
    """run() does not fail when list_mdb_tables raises for a file."""
    config = _make_config()
    engine = MagicMock()

    ingested_tables: list[str] = []

    def fake_ingest(db_path, table_name, country, community):
        ingested_tables.append(table_name)
        return 1

    with patch('stages.mdb_to_bronze.get_country_paths',
               return_value={'extract_path': '/fake'}), \
         patch('stages.mdb_to_bronze.glob_module.glob', return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.select_latest_per_tablet',
               return_value=['/fake/t1.mdb']), \
         patch('stages.mdb_to_bronze.list_mdb_tables',
               side_effect=RuntimeError('mdb-tables failed')), \
         patch.object(MdbToBronze, '_ingest_file', side_effect=fake_ingest):
        stage = MdbToBronze(config=config, engine=engine)
        result = stage.run()

    assert 'followup' not in ingested_tables
    assert result.success
