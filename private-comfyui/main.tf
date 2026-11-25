terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# GPU-ready Ubuntu 22.04 (NVIDIA driver + CUDA preinstalled, SSM agent included)
data "aws_ami" "dlami" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "${var.project_name}-vpc", Project = var.project_name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_name}-igw", Project = var.project_name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = var.az
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-subnet", Project = var.project_name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.project_name}-rt", Project = var.project_name }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# No inbound rules at all: access is via SSM port-forward only.
resource "aws_security_group" "sg" {
  name        = "${var.project_name}-sg"
  description = "No inbound; egress only (SSM tunnel access)"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-sg", Project = var.project_name }
}

resource "aws_instance" "comfyui" {
  ami                    = data.aws_ami.dlami.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.sg.id]
  iam_instance_profile   = aws_iam_instance_profile.node.name

  # shutdown from inside the guest = stop (never terminate)
  instance_initiated_shutdown_behavior = "stop"

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = templatefile("${path.module}/user_data.sh", { idle_minutes = var.idle_minutes })

  # A newer AMI or edited user_data must never silently replace the box.
  # Rebuild deliberately: terraform apply -replace=aws_instance.comfyui
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  tags = { Name = "${var.project_name}-server", Project = var.project_name }
}

# Persistent data volume: models, custom_nodes, output, user, ComfyUI checkout
resource "aws_ebs_volume" "data" {
  availability_zone = var.az
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "comfyui-data", Project = var.project_name }

  lifecycle {
    prevent_destroy = false # lifted deliberately for full teardown (2026-11-24); restore to true when redeploying
  }
}

resource "aws_volume_attachment" "data" {
  device_name  = "/dev/sdf"
  volume_id    = aws_ebs_volume.data.id
  instance_id  = aws_instance.comfyui.id
  skip_destroy = true
}
