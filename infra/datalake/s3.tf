# ---------- Bucket de logs de acesso ----------
resource "aws_s3_bucket" "logs" {
  bucket = "${local.bucket}-access-logs"
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
  }
}

# ---------- Bucket RAW ----------
resource "aws_s3_bucket" "raw" {
  bucket = local.bucket
}

resource "aws_s3_bucket_ownership_controls" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.raw.arn
    }
  }
}

resource "aws_s3_bucket_logging" "raw" {
  bucket        = aws_s3_bucket.raw.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "raw/"
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    id     = "tiering"
    status = "Enabled"
    filter {
      prefix = "dynamodb/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 180
      storage_class = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------- Bucket policy: segregação por tabela (prefixo exclusivo por role) ----------
data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyWrongKmsKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.raw.arn]
    }
  }

  # Cada role escreve SOMENTE no prefixo da sua tabela.
  # Limite: bucket policy <= 20 KB (~40-50 tabelas). Acima disso, migrar para uma
  # statement por fonte com ArnLike no padrão do nome da role (ver docs).
  dynamic "statement" {
    for_each = local.table_info
    content {
      sid       = "Write${replace(statement.key, "/[^A-Za-z0-9]/", "")}"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.raw.arn}/${statement.value.prefix}/*"]
      # Principal = raiz da conta produtora + condição pela role: o S3 NÃO valida a existência
      # da role (um ARN de role direto no Principal falha se a role ainda não foi criada).
      principals {
        type        = "AWS"
        identifiers = ["arn:aws:iam::${local.sources[statement.value.source].account_id}:root"]
      }
      condition {
        test     = "ArnEquals"
        variable = "aws:PrincipalArn"
        values   = [statement.value.role_arn]
      }
    }
  }

  # Export PITR (carga inicial) por fonte, prefixo <tabela>-initial
  dynamic "statement" {
    for_each = {
      for k, s in local.sources : k => s
      if length([for t in values(local.table_info) : t.prefix if t.source == k]) > 0
    }
    content {
      sid     = "InitialExport${replace(statement.key, "/[^A-Za-z0-9]/", "")}"
      actions = ["s3:PutObject", "s3:AbortMultipartUpload"]
      resources = [
        for t in values(local.table_info) :
        "${aws_s3_bucket.raw.arn}/${t.prefix}-initial/*" if t.source == statement.key
      ]
      principals {
        type        = "AWS"
        identifiers = ["arn:aws:iam::${statement.value.account_id}:root"]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "raw" {
  bucket     = aws_s3_bucket.raw.id
  policy     = data.aws_iam_policy_document.bucket.json
  depends_on = [aws_s3_bucket_public_access_block.raw]
}
