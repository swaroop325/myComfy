# Series maker application infrastructure: state, queue, storage, API, Lambdas.
# Build the Lambda package first: terraform/build.sh

variable "job_visibility_timeout" {
  description = "SQS visibility timeout in seconds; must exceed the longest expected job"
  type        = number
  default     = 1800
}

variable "episode_retention_days" {
  description = "Days before objects under episodes/ expire"
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambdas"
  type        = number
  default     = 14
}

variable "publish_timeout" {
  description = "Publish Lambda timeout in seconds (Instagram status polling runs up to ~600 s)"
  type        = number
  default     = 720
}

variable "idle_minutes" {
  description = "Minutes of idleness before the reaper stops the instance"
  type        = number
  default     = 15
}

variable "bootstrap_grace_minutes" {
  description = "Reaper keeps a never-adopted instance (no worker activity yet) this long after launch"
  type        = number
  default     = 90
}

variable "stuck_after_minutes" {
  description = "Queued jobs keep the instance up only while worker activity is fresher than this"
  type        = number
  default     = 20
}

variable "job_retention_seconds" {
  description = "Jobs queue message retention; bounds how long an undrained job can keep restarting the box"
  type        = number
  default     = 7200
}

variable "bot_timeout" {
  description = "Bot Lambda timeout in seconds (API Gateway HTTP API integrations time out at 30 s)"
  type        = number
  default     = 15
}

variable "instagram_graph_base" {
  description = "Graph API base for publishing: https://graph.instagram.com (Instagram Login) or https://graph.facebook.com (Facebook Login)"
  type        = string
  default     = "https://graph.instagram.com"
}

data "aws_caller_identity" "current" {}

locals {
  common_tags = {
    Project = var.project_name
  }
  lambda_zip = "${path.module}/build/lambda.zip"
  ssm_base   = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/seriesmaker"

  lambda_env = {
    TABLE                = aws_dynamodb_table.main.name
    QUEUE_URL            = aws_sqs_queue.jobs.url
    INSTANCE_ID          = aws_instance.comfyui.id
    BUCKET               = aws_s3_bucket.artifacts.bucket
    OWNER_CHAT_ID_PARAM  = "/seriesmaker/owner_chat_id"
    TELEGRAM_TOKEN_PARAM = "/seriesmaker/telegram_token"
    WEBHOOK_SECRET_PARAM = "/seriesmaker/webhook_secret"
    IDLE_MINUTES         = tostring(var.idle_minutes)
    BOOTSTRAP_GRACE_MIN  = tostring(var.bootstrap_grace_minutes)
    STUCK_AFTER_MIN      = tostring(var.stuck_after_minutes)
  }
}

# ---------- DynamoDB ----------
resource "aws_dynamodb_table" "main" {
  name         = "${var.project_name}-seriesmaker"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = local.common_tags
}

# ---------- SQS ----------
resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${var.project_name}-jobs-dlq"
  message_retention_seconds = 1209600
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.project_name}-jobs"
  visibility_timeout_seconds = var.job_visibility_timeout
  message_retention_seconds  = var.job_retention_seconds
  # maxReceiveCount must equal MAX_RECEIVES in worker/main.py (the worker gives up and
  # deletes the message on that attempt, so the DLQ only sees crashes mid-attempt).
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
  tags = local.common_tags
}

# ---------- S3 ----------
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-seriesmaker-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "artifacts_tls" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
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
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket     = aws_s3_bucket.artifacts.id
  policy     = data.aws_iam_policy_document.artifacts_tls.json
  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-episodes"
    status = "Enabled"
    filter {
      prefix = "episodes/"
    }
    expiration {
      days = var.episode_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_notification" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.notify.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "episodes/"
    filter_suffix       = "final.mp4"
  }

  depends_on = [aws_lambda_permission.notify_s3]
}

# ---------- Lambdas ----------
resource "aws_lambda_function" "bot" {
  depends_on       = [aws_cloudwatch_log_group.lambda["bot"]]
  function_name    = "${var.project_name}-bot"
  role             = aws_iam_role.bot.arn
  runtime          = "python3.12"
  handler          = "seriesmaker.bot.handler.lambda_handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = var.bot_timeout
  memory_size      = 256

  environment {
    variables = merge(local.lambda_env, {
      PUBLISH_FUNCTION = "${var.project_name}-publish"
    })
  }

  tags = local.common_tags
}

resource "aws_lambda_function" "reaper" {
  depends_on       = [aws_cloudwatch_log_group.lambda["reaper"]]
  function_name    = "${var.project_name}-reaper"
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.12"
  handler          = "seriesmaker.reaper.handler.lambda_handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = 60
  memory_size      = 256

  environment {
    variables = local.lambda_env
  }

  tags = local.common_tags
}

resource "aws_lambda_function" "notify" {
  depends_on       = [aws_cloudwatch_log_group.lambda["notify"]]
  function_name    = "${var.project_name}-notify"
  role             = aws_iam_role.notify.arn
  runtime          = "python3.12"
  handler          = "seriesmaker.notify.handler.lambda_handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = 30
  memory_size      = 256

  environment {
    variables = local.lambda_env
  }

  tags = local.common_tags
}

resource "aws_lambda_function" "publish" {
  depends_on       = [aws_cloudwatch_log_group.lambda["publish"]]
  function_name    = "${var.project_name}-publish"
  role             = aws_iam_role.publish.arn
  runtime          = "python3.12"
  handler          = "seriesmaker.publish.handler.lambda_handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = var.publish_timeout
  memory_size      = 256

  environment {
    variables = merge(local.lambda_env, {
      INSTAGRAM_GRAPH_BASE = var.instagram_graph_base
    })
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(["bot", "reaper", "notify", "publish"])
  name              = "/aws/lambda/${var.project_name}-${each.key}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

# Async invoke from the bot: never retry (a retry after a partial publish could double-post).
resource "aws_lambda_function_event_invoke_config" "publish" {
  function_name                = aws_lambda_function.publish.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}

# ---------- API Gateway HTTP API ----------
resource "aws_apigatewayv2_api" "bot" {
  name          = "${var.project_name}-bot"
  protocol_type = "HTTP"
  tags          = local.common_tags
}

resource "aws_apigatewayv2_integration" "bot" {
  api_id                 = aws_apigatewayv2_api.bot.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.bot.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "telegram" {
  api_id    = aws_apigatewayv2_api.bot.id
  route_key = "POST /telegram"
  target    = "integrations/${aws_apigatewayv2_integration.bot.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.bot.id
  name        = "$default"
  auto_deploy = true
  tags        = local.common_tags
}

resource "aws_lambda_permission" "bot_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bot.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.bot.execution_arn}/*/*/telegram"
}

# ---------- EventBridge (reaper) ----------
resource "aws_cloudwatch_event_rule" "reaper" {
  name                = "${var.project_name}-reaper"
  schedule_expression = "rate(5 minutes)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "reaper" {
  rule = aws_cloudwatch_event_rule.reaper.name
  arn  = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "reaper_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reaper.arn
}

resource "aws_lambda_permission" "notify_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.artifacts.arn

  source_account = data.aws_caller_identity.current.account_id
}
