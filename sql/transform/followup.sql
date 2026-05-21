-- DROP+CREATE is safe here: this pipeline runs in batch mode with no concurrent
-- readers expected during ETL execution. The brief table-absence window between
-- DROP and CREATE is acceptable.
DROP TABLE IF EXISTS gold_ibis.followup;
CREATE TABLE gold_ibis.followup AS
SELECT * FROM (
    SELECT f.*
    FROM silver_ibis.followup f
    WHERE uniqueid IS NOT NULL
) t
-- Exclude ETL pipeline tracking columns; all survey data columns are retained.
;

ALTER TABLE gold_ibis.followup
    DROP COLUMN IF EXISTS run_uuid,
    DROP COLUMN IF EXISTS file_name,
    DROP COLUMN IF EXISTS file_path,
    DROP COLUMN IF EXISTS extracted_at;
