# CMK do RAW. Uma statement por FONTE (escala com N tabelas): libera as roles
# <project>-<source>-*-writer daquela conta e, opcionalmente, roles de operação (export inicial).
data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "AdminAccount"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  dynamic "statement" {
    for_each = local.sources
    content {
      sid       = "Producer${replace(statement.key, "/[^A-Za-z0-9]/", "")}"
      actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
      resources = ["*"]
      principals {
        type        = "AWS"
        identifiers = ["arn:aws:iam::${statement.value.account_id}:root"]
      }
      condition {
        test     = "ArnLike"
        variable = "aws:PrincipalArn"
        values = concat(
          ["arn:aws:iam::${statement.value.account_id}:role/${local.cfg.project}-${statement.key}-*-writer"],
          try(statement.value.initial_load_role_arns, [])
        )
      }
    }
  }
}

resource "aws_kms_key" "raw" {
  description             = "CMK do bucket RAW ${local.bucket}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "raw" {
  name          = "alias/${local.bucket}"
  target_key_id = aws_kms_key.raw.key_id
}
