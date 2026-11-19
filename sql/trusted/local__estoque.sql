-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/estoque.yaml
CREATE OR REPLACE VIEW trusted.local_estoque AS
SELECT
  produto_id,
  quantidade,
  atualizado_em,
  CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at,
  sequencenumber AS _sequence
FROM (
  SELECT
    json_extract_scalar(newimage, '$["produto_id"]["s"]') AS produto_id,
    CAST(json_extract_scalar(newimage, '$["quantidade"]["n"]') AS int) AS quantidade,
    CAST(from_iso8601_timestamp(json_extract_scalar(newimage, '$["atualizado_em"]["s"]')) AS timestamp) AS atualizado_em,
    eventname,
    approximatecreationdatetime,
    sequencenumber,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(keys, '$["produto_id"]["s"]')
      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC
    ) AS _rn
  FROM raw.dynamodb_estoque
)
WHERE _rn = 1 AND eventname <> 'REMOVE';
