# Make sure to create state bucket beforehand
terraform {
  required_version = ">= 1.0"

}

provider "aws" {
  region            = var.aws_region
}

data "aws_caller_identity" "current_identity" {}

locals {
  account_id = data.aws_caller_identity.current_identity.account_id
}

# # Bucket where new data should be added
# resource "aws_s3_bucket" "new_data_bucket" {
#   bucket = var.data_bucket
# }

# # Enable s3 notifications on the bucket
# resource "aws_s3_bucket_notification" "bucket_notification" {
#   bucket      = aws_s3_bucket.new_data_bucket.id
#   eventbridge = true
# }


# Processing lambda
module "processing_lambda_function" {
  source               = "./modules/lambda"
  image_uri            = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.lambda_image_name}"
  lambda_function_name = var.processing_lambda_function_name
  model_bucket         = var.model_bucket
  data_bucket          = var.data_bucket
  model_path           = var.model_path
  image_config_cmd     = var.processing_image_config_cmd

}


# Inference lambda
module "inference_lambda_function" {
  source               = "./modules/lambda"
  image_uri            = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.lambda_image_name}"
  lambda_function_name = var.inference_lambda_function_name
  model_bucket         = var.model_bucket
  model_path           = var.model_path
  data_bucket          = var.data_bucket
  image_config_cmd     = var.inference_image_config_cmd

}

# Observe lambda
module "observe_lambda_function" {
  source               = "./modules/lambda"
  image_uri            = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.lambda_image_name}"
  lambda_function_name = var.observe_lambda_function_name
  model_bucket         = var.model_bucket
  model_path           = var.model_path
  data_bucket          = var.data_bucket
  image_config_cmd     = var.observe_image_config_cmd

}

# Collect the ARN so we can pass them to the step function
# These are returned by each lambda module
locals {
  lambda_arns = {
    processing = module.processing_lambda_function.lambda_arn
    inference  = module.inference_lambda_function.lambda_arn
    observe    = module.observe_lambda_function.lambda_arn
  }
}

# The main pipeline, built as a StepFunction state machine
# Uses the 3 lambdas above to do everything
module "inference_pipeline" {
  source        = "./modules/step_function"
  lambda_arns   = local.lambda_arns
  pipeline_name = var.pipeline_name
}

locals {
  pipeline_arn = module.inference_pipeline.pipeline_arn
}

# An EventBridge trigger that runs the pipeline every 24 hours
module "schduler_trigger" {
  source         = "./modules/event_bridge_scheduler"
  pipeline_arn   = local.pipeline_arn
  data_bucket    = var.data_bucket
  scheduler_name = var.scheduler_name
  time_interval  = var.time_interval
}

# RDS Postgres database
module "inference_db"{
  source = "./modules/rds"
  db_name = var.db_name
  db_username = var.db_username
  db_password = var.db_password
}