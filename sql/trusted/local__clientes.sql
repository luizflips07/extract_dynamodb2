-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/clientes.yaml
CREATE OR REPLACE VIEW trusted.local_clientes AS
SELECT
  id,
  nome,
  email,
  ativo,
  criado_em,
  cidade,
  uf,
  CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at,
  sequencenumber AS _sequence
FROM (
  SELECT
    json_extract_scalar(newimage, '$["id"]["s"]') AS id,
    json_extract_scalar(newimage, '$["nome"]["s"]') AS nome,
    json_extract_scalar(newimage, '$["email"]["s"]') AS email,
    CAST(json_extract_scalar(newimage, '$["ativo"]["bool"]') AS boolean) AS ativo,
    CAST(from_iso8601_timestamp(json_extract_scalar(newimage, '$["criado_em"]["s"]')) AS timestamp) AS criado_em,
    json_extract_scalar(newimage, '$["cidade"]["s"]') AS cidade,
    json_extract_scalar(newimage, '$["uf"]["s"]') AS uf,
    eventname,
    approximatecreationdatetime,
    sequencenumber,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(keys, '$["id"]["s"]')
      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC
    ) AS _rn
  FROM raw.dynamodb_clientes
)
WHERE _rn = 1 AND eventname <> 'REMOVE';
