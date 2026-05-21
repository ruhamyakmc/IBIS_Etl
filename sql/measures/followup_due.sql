-- DROP+CREATE is safe here: this pipeline runs in batch mode with no concurrent
-- readers expected during ETL execution.
DROP TABLE IF EXISTS gold_ibis.ds_followup_due;
CREATE TABLE gold_ibis.ds_followup_due AS
SELECT
    b.subjid,
    b.countrycode,
    b.arm_text,
    b.health_facility_ug,
    b.health_facility_ke,
    b.starttime                                                         AS baseline_date,
    b.next_appt_3m,
    b.next_appt_6m,
    (f.subjid IS NOT NULL)                                              AS has_followup,
    f.starttime                                                         AS followup_date,
    CASE
        WHEN f.subjid IS NOT NULL
         AND (
             f.starttime IS NULL
             OR TO_DATE(f.starttime,  'DD/MM/YYYY HH24:MI:SS')
                NOT BETWEEN
                    TO_DATE(b.next_appt_3m, 'DD/MM/YYYY HH24:MI:SS')
                AND TO_DATE(b.next_appt_6m, 'DD/MM/YYYY HH24:MI:SS')
         )
        THEN TRUE
        ELSE FALSE
    END                                                                 AS followup_out_of_window,
    CASE
        WHEN b.next_appt_3m IS NULL OR b.next_appt_6m IS NULL
            THEN 'missing_appt'
        WHEN f.subjid IS NOT NULL
         AND f.starttime IS NOT NULL
         AND TO_DATE(f.starttime,  'DD/MM/YYYY HH24:MI:SS')
             BETWEEN
                 TO_DATE(b.next_appt_3m, 'DD/MM/YYYY HH24:MI:SS')
             AND TO_DATE(b.next_appt_6m, 'DD/MM/YYYY HH24:MI:SS')
            THEN 'attended'
        WHEN CURRENT_DATE < TO_DATE(b.next_appt_3m, 'DD/MM/YYYY HH24:MI:SS')
            THEN 'upcoming'
        WHEN CURRENT_DATE <= TO_DATE(b.next_appt_6m, 'DD/MM/YYYY HH24:MI:SS')
            THEN 'due'
        ELSE
            'overdue'
    END                                                                 AS window_status
FROM gold_ibis.baseline b
LEFT JOIN gold_ibis.followup f ON f.subjid = b.subjid
WHERE b.consent::integer = 1
  AND b.subjid IS NOT NULL;
