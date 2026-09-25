variable "aws_region" {
  description = "AWS region (e.g. ap-south-1)."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the backtest VPC. Must not overlap the live VPC (10.0.0.0/16)."
  type        = string
  default     = "10.40.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR block for the single public subnet."
  type        = string
  default     = "10.40.1.0/24"
}

variable "availability_zone" {
  description = "AZ for the public subnet (e.g. ap-south-1a)."
  type        = string
}

variable "tags" {
  description = "Tags applied to all resources in this module."
  type        = map(string)
  default     = {}
}
