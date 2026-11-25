# IAM for the Lambdas and the EC2 worker (least privilege).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ---------- Shared statements ----------
data "aws_iam_policy_document" "table_rw" {
  statement {
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.main.arn]
  }
}

# Per-role SSM read access by explicit parameter ARN (parameters are created out of band).
locals {
  ssm_params = {
    bot     = ["telegram_token", "webhook_secret", "owner_chat_id"]
    worker  = ["telegram_token", "owner_chat_id"]
    notify  = ["telegram_token", "owner_chat_id"]
    reaper  = ["telegram_token", "owner_chat_id"] # owner notices (worker never started, stuck queue)
    publish = ["instagram_token", "instagram_user_id", "instagram_enabled", "telegram_token", "owner_chat_id"]
  }
}

data "aws_iam_policy_document" "ssm_get" {
  for_each = local.ssm_params
  statement {
    sid       = "ReadParameters"
    actions   = ["ssm:GetParameter"]
    resources = [for p in each.value : "${local.ssm_base}/${p}"]
  }
}

# Reaper only reads state and updates/puts items (no delete/batch write).
data "aws_iam_policy_document" "table_reaper" {
  statement {
    actions = [
      "dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem",
      "dynamodb:UpdateItem", "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.main.arn]
  }
}

# ---------- EC2 worker ----------
resource "aws_iam_role" "worker" {
  name               = "${var.project_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "worker" {
  source_policy_documents = [
    data.aws_iam_policy_document.table_rw.json,
    data.aws_iam_policy_document.ssm_get["worker"].json,
  ]

  statement {
    sid = "Queue"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "Objects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  # Stale scene cleanup after an edit shortens an episode; never releases/.
  statement {
    sid       = "DeleteEpisodeObjects"
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/episodes/*"]
  }

  statement {
    sid       = "List"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "app"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.project_name}-worker"
  role = aws_iam_role.worker.name
}

# ---------- Bot Lambda ----------
resource "aws_iam_role" "bot" {
  name               = "${var.project_name}-bot"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "bot" {
  source_policy_documents = [
    data.aws_iam_policy_document.table_rw.json,
    data.aws_iam_policy_document.ssm_get["bot"].json,
  ]

  statement {
    sid       = "StartInstance"
    actions   = ["ec2:StartInstances"]
    resources = [aws_instance.comfyui.arn]
  }

  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "EnqueueJobs"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "InvokePublish"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.publish.arn]
  }
}

resource "aws_iam_role_policy" "bot" {
  name   = "app"
  role   = aws_iam_role.bot.id
  policy = data.aws_iam_policy_document.bot.json
}

# ---------- Reaper Lambda ----------
resource "aws_iam_role" "reaper" {
  name               = "${var.project_name}-reaper"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "reaper" {
  source_policy_documents = [
    data.aws_iam_policy_document.table_reaper.json,
    data.aws_iam_policy_document.ssm_get["reaper"].json,
  ]

  # Start: restart a stopped box with queued jobs (capped to once per 30 min in code).
  statement {
    sid       = "StopStartInstance"
    actions   = ["ec2:StopInstances", "ec2:StartInstances"]
    resources = [aws_instance.comfyui.arn]
  }

  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "QueueDepth"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "reaper" {
  name   = "app"
  role   = aws_iam_role.reaper.id
  policy = data.aws_iam_policy_document.reaper.json
}

# ---------- Notify Lambda ----------
resource "aws_iam_role" "notify" {
  name               = "${var.project_name}-notify"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "notify" {
  source_policy_documents = [
    data.aws_iam_policy_document.table_rw.json,
    data.aws_iam_policy_document.ssm_get["notify"].json,
  ]

  statement {
    sid       = "ReadObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role_policy" "notify" {
  name   = "app"
  role   = aws_iam_role.notify.id
  policy = data.aws_iam_policy_document.notify.json
}

# ---------- Publish Lambda ----------
resource "aws_iam_role" "publish" {
  name               = "${var.project_name}-publish"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "publish" {
  source_policy_documents = [
    data.aws_iam_policy_document.table_rw.json,
    data.aws_iam_policy_document.ssm_get["publish"].json,
  ]

  statement {
    sid       = "TokenRefresh"
    actions   = ["ssm:PutParameter"]
    resources = ["${local.ssm_base}/instagram_token"]
  }

  statement {
    sid       = "ReadObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role_policy" "publish" {
  name   = "app"
  role   = aws_iam_role.publish.id
  policy = data.aws_iam_policy_document.publish.json
}

# ---------- CloudWatch Logs for Lambdas ----------
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  for_each = {
    bot     = aws_iam_role.bot.name
    reaper  = aws_iam_role.reaper.name
    notify  = aws_iam_role.notify.name
    publish = aws_iam_role.publish.name
  }
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
