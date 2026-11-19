-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/vendas.yaml
CREATE OR REPLACE VIEW trusted.local_vendas AS
SELECT
  cliente_id,
  venda_id,
  produto_id,
  quantidade,
  valor_total,
  data,
  status,
  pago,
  CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at,
  sequencenumber AS _sequence
FROM (
  SELECT
    json_extract_scalar(newimage, '$["cliente_id"]["s"]') AS cliente_id,
    json_extract_scalar(newimage, '$["venda_id"]["s"]') AS venda_id,
    json_extract_scalar(newimage, '$["produto_id"]["s"]') AS produto_id,
    CAST(json_extract_scalar(newimage, '$["quantidade"]["n"]') AS int) AS quantidade,
    CAST(json_extract_scalar(newimage, '$["valor_total"]["n"]') AS decimal(12,2)) AS valor_total,
    CAST(from_iso8601_timestamp(json_extract_scalar(newimage, '$["data"]["s"]')) AS timestamp) AS data,
    json_extract_scalar(newimage, '$["status"]["s"]') AS status,
    CAST(json_extract_scalar(newimage, '$["pago"]["bool"]') AS boolean) AS pago,
    eventname,
    approximatecreationdatetime,
    sequencenumber,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(keys, '$["cliente_id"]["s"]'), json_extract_scalar(keys, '$["venda_id"]["s"]')
      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC
    ) AS _rn
  FROM raw.dynamodb_vendas
)
WHERE _rn = 1 AND eventname <> 'REMOVE';
