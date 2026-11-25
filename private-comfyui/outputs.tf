output "instance_id" {
  value = aws_instance.comfyui.id
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}

output "tunnel_command" {
  value = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.comfyui.id} --document-name AWS-StartPortForwardingSession --parameters portNumber=8188,localPortNumber=8188"
}
