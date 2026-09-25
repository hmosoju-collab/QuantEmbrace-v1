variable "alert_email" {
  description = "Email address for SNS alert subscriptions. Leave empty to skip subscription."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly USD cost budget for the backtesting-lab cost center. Alert fires at 80% (actual) and 100% (forecasted)."
  type        = number
  default     = 100
}

variable "tags" {
  description = "Tags applied to all resources in this module."
  type        = map(string)
  default     = {}
}
