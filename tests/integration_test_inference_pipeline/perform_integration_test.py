"""
Performs a simple integration test by
launching the lambda container calling the lambda
API and then comparing the expected and recieved results.
Assumes everything is already set-up
"""

import logging
import sys
from typing import Any, Dict

import boto3
import requests
from deepdiff import DeepDiff
from omegaconf import DictConfig
from rich.logging import RichHandler
from rich.traceback import install
from test_utils import clean_up, launch_lambda_container, print_difference

logger = logging.getLogger(__name__)
logger.addHandler(RichHandler(rich_tracebacks=True, markup=True))
logger.setLevel("INFO")
# Setup rich to get nice tracebacks
install()

LAMBDA_URL = "http://localhost:8080/2015-03-31/functions/function/invocations"


def integration_test(config: DictConfig, name: str, settings: Dict[str, Any]) -> None:
    """Perform a single integration test for a given lambda.
    Will do the following
    - spin up the lambda container
    - set the right lambda handler
    - send an API request to the lambda with the payload
    - compare the resulting response to the expectations

    Args:
        config (DictConfig): The config to pass to the lambda docker
        name (str): The name of the lambda function to test
        settings (Dict[str, Any]): Local settings that describe
            test input/output
    """
    expectation = settings["expectation"]
    payload = settings["payload"]
    target = settings["target"]
    logger.info(f"Starting the {name} lambda integration test")
    # Launch the image with the correct CMD
    logger.info("Launching lambda docker container")
    container = launch_lambda_container(name, config)
    logger.info("Done")
    # Send request to the right port
    logger.info("Sending API request")
    response = requests.post(LAMBDA_URL, json=payload, timeout=500).json()
    body = response["body"]
    status = response["statusCode"]
    if status != 200:
        logger.critical(f"Received status {status}")
        logger.info(body)
        clean_up(container)
        sys.exit(1)

    logger.info(f"Response was {response}")
    # Perform checks
    logger.info("Performing checks")
    logger.info(
        "Will check the prediction file was created in right place with right size"
    )
    s3 = boto3.client("s3", endpoint_url=config.aws_endpoint_url)
    response_check = s3.list_objects_v2(
        Bucket=config.data_bucket_name, Prefix=config.data_path
    )
    result = {}
    for it in response_check["Contents"]:
        key = it["Key"]
        if target in key:
            result[key] = it["Size"]

    # We check the following:
    # 1. The processed data is present in the s3 bucket
    # 2. Check that the processed data is the right size
    if DeepDiff(result, expectation):
        logger.critical("The lambda function result and the expectations differ:")
        logger.info("Cleaning up and exiting")
        print_difference(expectation, result)
        clean_up(container)
        sys.exit(1)
    else:
        logger.info("Result and expectation match:")
        print_difference(expectation, result)

    logger.info("Checks completed!")
    logger.info("Cleaning up")
    clean_up(container)
    logger.info("Done")
