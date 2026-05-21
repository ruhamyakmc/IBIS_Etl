-- Migration 001: add table_name column to bronze_ibis.meta
-- Required to distinguish baseline vs followup ingestion records from the same file.
-- Safe to run repeatedly (IF NOT EXISTS / IF table exists).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'bronze_ibis' AND table_name = 'meta'
    ) THEN
        ALTER TABLE bronze_ibis.meta
            ADD COLUMN IF NOT EXISTS table_name TEXT;

        UPDATE bronze_ibis.meta
        SET table_name = 'baseline'
        WHERE table_name IS NULL;
    END IF;
END $$;
