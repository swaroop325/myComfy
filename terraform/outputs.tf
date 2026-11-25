# Terraform Outputs

output "instance_id" {
  description = "EC2 Instance ID"
  value       = aws_instance.comfyui.id
}

output "instance_public_ip" {
  description = "Public IP of the EC2 instance (changes if the instance is stopped/started)"
  value       = aws_instance.comfyui.public_ip
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = "ssh -i ${path.module}/${var.key_name}.pem ubuntu@${aws_instance.comfyui.public_ip}"
}

output "comfyui_tunnel_command" {
  description = "SSH tunnel to reach ComfyUI at http://localhost:8188"
  value       = "ssh -i ${path.module}/${var.key_name}.pem -N -L 8188:127.0.0.1:8188 ubuntu@${aws_instance.comfyui.public_ip}"
}

output "aws_region" {
  description = "AWS region"
  value       = var.aws_region
}

output "webhook_url" {
  description = "Telegram webhook URL (register with scripts/set_webhook.sh)"
  value       = "${aws_apigatewayv2_api.bot.api_endpoint}/telegram"
}

output "queue_url" {
  description = "Job queue URL"
  value       = aws_sqs_queue.jobs.url
}

output "bucket" {
  description = "Artifacts bucket"
  value       = aws_s3_bucket.artifacts.bucket
}

output "key_file_path" {
  description = "Path to the SSH private key"
  value       = "${path.module}/${var.key_name}.pem"
}

output "table" {
  description = "DynamoDB table (scripts/keep_awake.sh writes the keep-awake hold here)"
  value       = aws_dynamodb_table.main.name
}
