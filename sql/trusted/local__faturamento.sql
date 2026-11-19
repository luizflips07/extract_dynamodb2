-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/faturamento.yaml
CREATE OR REPLACE VIEW trusted.local_faturamento AS
SELECT
  fatura_id,
  cliente_id,
  valor,
  data_emissao,
  CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at,
  sequencenumber AS _sequence
FROM (
  SELECT
    json_extract_scalar(newimage, '$["fatura_id"]["s"]') AS fatura_id,
    json_extract_scalar(newimage, '$["cliente_id"]["s"]') AS cliente_id,
    CAST(json_extract_scalar(newimage, '$["valor"]["n"]') AS decimal(18,2)) AS valor,
    CAST(from_iso8601_timestamp(json_extract_scalar(newimage, '$["data_emissao"]["s"]')) AS timestamp) AS data_emissao,
    eventname,
    approximatecreationdatetime,
    sequencenumber,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(keys, '$["fatura_id"]["s"]')
      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC
    ) AS _rn
  FROM raw.dynamodb_faturamento
)
WHERE _rn = 1 AND eventname <> 'REMOVE';
