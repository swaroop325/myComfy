variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "az" {
  description = "Single AZ shared by subnet, instance and data volume (EBS is AZ-bound)"
  type        = string
  default     = "ap-southeast-1a"
}

variable "project_name" {
  type    = string
  default = "comfyui-private"
}

variable "instance_type" {
  description = "g4dn.xlarge = 1x T4 16GB. Change to g6e.* / p4de.24xlarge when quota and region allow."
  type        = string
  default     = "g4dn.xlarge"
}

variable "root_volume_gb" {
  description = "Must be >= the DLAMI snapshot size (75 GB)"
  type        = number
  default     = 80
}

variable "data_volume_gb" {
  type    = number
  default = 200
}

variable "idle_minutes" {
  description = "Instance shuts itself down (stop) after this many idle minutes"
  type        = number
  default     = 30
}
