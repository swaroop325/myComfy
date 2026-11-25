resource "aws_iam_role" "node" {
  name = "${var.project_name}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Optional HF token in SSM Parameter Store (/comfyui/hf_token)
resource "aws_iam_role_policy" "params" {
  name = "read-comfyui-params"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/comfyui/*"
    }]
  })
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.project_name}-node"
  role = aws_iam_role.node.name
}
