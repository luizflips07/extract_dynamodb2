-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/produtos.yaml
CREATE OR REPLACE VIEW trusted.local_produtos AS
SELECT
  id,
  nome,
  categoria,
  preco,
  ativo,
  CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at,
  sequencenumber AS _sequence
FROM (
  SELECT
    json_extract_scalar(newimage, '$["id"]["s"]') AS id,
    json_extract_scalar(newimage, '$["nome"]["s"]') AS nome,
    json_extract_scalar(newimage, '$["categoria"]["s"]') AS categoria,
    CAST(json_extract_scalar(newimage, '$["preco"]["n"]') AS decimal(10,2)) AS preco,
    CAST(json_extract_scalar(newimage, '$["ativo"]["bool"]') AS boolean) AS ativo,
    eventname,
    approximatecreationdatetime,
    sequencenumber,
    ROW_NUMBER() OVER (
      PARTITION BY json_extract_scalar(keys, '$["id"]["s"]')
      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC
    ) AS _rn
  FROM raw.dynamodb_produtos
)
WHERE _rn = 1 AND eventname <> 'REMOVE';
