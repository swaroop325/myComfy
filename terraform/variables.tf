# Variables for ComfyUI AWS Infrastructure

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-southeast-1"
}

variable "project_name" {
  description = "Project name for tagging"
  type        = string
  default     = "comfyui"
}

variable "allowed_ip" {
  description = "Your IP address to whitelist for access (CIDR format: x.x.x.x/32)"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type (GPU instance for ComfyUI)"
  type        = string
  # g4dn.12xlarge: 48 vCPUs, 192GB RAM, 4x NVIDIA T4 GPUs (64GB VRAM), ~$3.912/hr
  # g4dn.metal: 96 vCPUs, 384GB RAM, 8x NVIDIA T4 GPUs (128GB VRAM), ~$7.824/hr
  # g4dn.xlarge: 4 vCPUs, 16GB RAM, 1x NVIDIA T4 GPU (16GB VRAM), ~$0.526/hr
  default = "g4dn.12xlarge"
}

variable "key_name" {
  description = "Name of the SSH key pair"
  type        = string
  default     = "comfyui-key"
}

variable "volume_size" {
  description = "Root volume size in GB (need space for models)"
  type        = number
  default     = 300 # ~40 GB LLM plus ComfyUI models
}

variable "llm_model" {
  description = "Ollama model tag pulled at first boot (TO BE CHANGED after the LLM speed spike)"
  type        = string
  default     = "llama3.3:70b"
}
