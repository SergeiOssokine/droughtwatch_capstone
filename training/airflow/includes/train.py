"""Contains routines to train a CNN with Keras and keep track
of experiments with either WandB or MLFlow. See the configuration
options in setup/conf/training
"""

import glob
import logging
import os
import sys
from typing import Any, Dict, List

import keras
import mlflow
import numpy as np
import omegaconf
import tensorflow as tf
import wandb
from hydra import compose, initialize_config_dir
from keras import layers
from omegaconf import DictConfig
from rich.logging import RichHandler
from rich.traceback import install
from wandb.integration.keras import WandbMetricsLogger

from . import parse_data
from .training_utils import (
    convert_model_to_onnx,
    generate_random_id,
    upload_model_to_s3,
)

AUTOTUNE = tf.data.AUTOTUNE
print(tf.__version__)
tf.compat.v1.set_random_seed(23)

CONFIG_PATH = "/usr/local/airflow/conf"

# Sets up the logger to work with rich
logger = logging.getLogger(__name__)
logger.addHandler(RichHandler(rich_tracebacks=True, markup=True))
logger.setLevel("INFO")
# Setup rich to get nice tracebacks
install()


# default image side dimension (65 x 65 square)
IMG_DIM = 65
# 4 possible classes
NUM_CLASSES = 4
NUM_TRAIN = 500
NUM_VAL = 320

PROJECT_NAME = "droughtwatch_capstone"


def get_dataset(
    filelist: List[str],
    batch_size: int,
    buffer_size: int,
    keylist: List[str] | None = None,
    shuffle: bool = True,
):
    """Return a batched and shuffled dataset. The input should correspond
    to processed files.

    Args:
        filelist (List[str]): List of files comprising the processed TFRecords dataset
        batch_size (int): The batch size
        buffer_size (int): The buffer size for shuffling
        keylist (List[str], optional): The list of features to return.
        shuffle (bool, optional): Determines if we shuffle the dataset. Defaults to True.

    Returns:
        tf.Dataset: The dataset ready for training/validation
    """
    if keylist is None:
        # Use RGB bands as default
        keylist = ["B2", "B3", "B4"]
    dataset = parse_data.read_processed_tfrecord(filelist, keylist=keylist)
    if shuffle:
        dataset = dataset.shuffle(buffer_size)
    dataset = dataset.batch(batch_size)
    return dataset


def class_weights() -> Dict[int, float]:
    """Define class weights to account for uneven distribution of classes
    distribution of ground truth labels:
    0: ~60%
    1: ~15%
    2: ~15%
    3: ~10%

    Returns:
        Dict[int, float]: Class weights for evert class
    """

    class_weights_dict = {}
    class_weights_dict[0] = 1.0
    class_weights_dict[1] = 4.0
    class_weights_dict[2] = 4.0
    class_weights_dict[3] = 6.0
    return class_weights_dict


def construct_baseline_model(cfg: DictConfig) -> keras.Sequential:
    """Construct a simple baseline CNN

    Args:
        cfg (DictConfig): The config object which holds learning parameters

    Returns:
        keras.Sequential: The compiled baseline model
    """
    num_bands = len(cfg.features.list)
    lr = cfg.model.learning_rate
    model = keras.Sequential(
        [
            keras.Input(shape=[IMG_DIM, IMG_DIM, num_bands]),
            layers.Conv2D(32, kernel_size=(3, 3), activation="relu"),
            layers.MaxPooling2D(pool_size=(2, 2)),
            layers.Conv2D(32, kernel_size=(3, 3), activation="relu"),
            layers.MaxPooling2D(pool_size=(2, 2)),
            layers.Conv2D(64, kernel_size=(3, 3), activation="relu"),
            layers.MaxPooling2D(pool_size=(2, 2)),
            layers.Conv2D(128, kernel_size=(3, 3), activation="relu"),
            layers.Conv2D(128, kernel_size=(3, 3), activation="relu"),
            layers.MaxPooling2D(pool_size=(2, 2)),
            layers.Dropout(0.2),
            layers.Flatten(),
            layers.Dense(units=50, activation="relu"),
            layers.Dropout(0.2),
            layers.Dense(NUM_CLASSES, activation="softmax"),
        ]
    )
    ths = list(np.arange(0, 0.99, 0.01))
    metrics = []
    # Compute precision and recall at every epoch for every class
    if cfg.logging.style == "wandb":
        for i in range(NUM_CLASSES):
            name1 = f"pr_{i}"
            name2 = f"re_{i}"
            m1 = keras.metrics.Precision(thresholds=ths, class_id=i, name=name1)
            m2 = keras.metrics.Recall(thresholds=ths, class_id=i, name=name2)
            metrics.append(m1)
            metrics.append(m2)

    # Also compute accuracy
    metrics.append("accuracy")

    model.compile(
        loss="categorical_crossentropy",
        optimizer=keras.optimizers.Adam(learning_rate=lr),
        metrics=metrics,
    )

    return model


def train_model(
    model_config: str = "default",
    features_config: str = "default",
    logging_config: str = "default",
    override_args: Dict[str, Any] | None = None,
) -> None:
    """A wrapper around the training code that reads the config
    and applies any overrides before calling the actual training.
    See ./setp/conf/training for options.

    Args:
        model_config (str, optional): Which model configuration to use. Defaults to "default".
        features_config (str, optional): Which features to use. Defaults to "default".
        logging_config (str, optional): What logging to use. Defaults to "default".
        override_args (Dict[str, Any] | None, optional): Any direct overrides to give.
            Defaults to None.
    """
    if override_args is None:
        override_args = {}

    initialize_config_dir(
        version_base=None, config_dir=CONFIG_PATH, job_name="train_model"
    )

    cfg = compose(
        config_name="config",
        overrides=[
            f"training/model={model_config}",
            f"training/features={features_config}",
            f"training/logging={logging_config}",
            *[f"{k}={v}" for k, v in override_args.items()],
        ],
    )

    train_cnn(cfg.training)


def train_cnn(cfg: DictConfig):
    """Train a baseline CNN model.
    For the possible settings see setup/conf/training/*

    Args:
        cfg (DictConfig): All settings

    Raises:
        NotImplementedError: If experiment tracking style is not supported
    """
    # Model related settings
    keylist = cfg.features.list
    batch_size = cfg.model.batch_size
    epochs = cfg.model.epochs
    # Get logging style and options
    logging_style = cfg.logging.style

    # load training data in TFRecord format
    filelist = glob.glob(os.path.join(cfg.data.train_data, "processed_part*"))
    train_dataset = get_dataset(filelist, batch_size, NUM_TRAIN, keylist=keylist)

    # load validation data in TFRecord format
    filelist = glob.glob(os.path.join(cfg.data.val_data, "processed_part*"))
    val_dataset = get_dataset(filelist, batch_size, NUM_VAL, keylist=keylist)

    model = construct_baseline_model(cfg)
    run_name = f"{cfg.model.name}_{generate_random_id()}"
    config = omegaconf.OmegaConf.to_container(cfg, resolve=True, throw_on_missing=True)
    config.pop("logging")
    if logging_style == "wandb":
        # Do cloud-based wandb logging
        try:
            key = os.environ["WANDB_API_KEY"]
        except KeyError:
            logger.critical(
                "Logging style was set to wandb, but the WANDB_API_KEY is not set."
                "Make sure to change it inside the .env file!"
            )
            sys.exit(-1)
        wandb.login(key=key)

        # initialize wandb logging for your project and save your settings

        run = wandb.init(name=run_name, project=PROJECT_NAME)

        wf_cfg = wandb.config
        wf_cfg.setdefaults(config)
        callbacks = [WandbMetricsLogger()]

    elif logging_style == "mlflow":
        # Do local MLFlow logging
        mlflow.set_tracking_uri("http://mlflow-server:5012")
        mlflow.set_experiment(PROJECT_NAME)
        run = mlflow.start_run(run_name=run_name)
        callbacks = [mlflow.keras.callback.MlflowCallback(run)]
    else:
        logger.critical(f"Logging style {logging_style} unknown! Exiting")
        raise NotImplementedError

    if epochs > 0:
        model.fit(
            train_dataset,
            epochs=epochs,
            validation_data=val_dataset,
            class_weight=class_weights(),
            callbacks=callbacks,
        )
    if logging_style == "mlflow":
        mlflow.keras.log_model(model, "artifacts")
        mlflow.log_params(config)

    if cfg.model.register:
        # We want to register this model in the model registry so we can
        # later use it in production.

        # Convert the trained model to ONNX
        onnx_model = convert_model_to_onnx(model)

        model_s3_path = f"s3://{cfg.model_registry_s3_bucket}/{cfg.model.name}"
        config_yaml = omegaconf.OmegaConf.to_yaml(cfg, resolve=True)

        if logging_style == "mlflow":
            model_uri = f"runs:/{run.info.run_id}/model"
            mlflow.register_model(model_uri=model_uri, name=cfg.model.name)
            mlflow.onnx.log_model(onnx_model, "artifacts-generic")
            # Record the S3 path
            mlflow.log_param("model_s3_path", model_s3_path)
            mlflow.end_run()
            # Upload the model to S3
            upload_model_to_s3(
                onnx_model, cfg.model.name, cfg.model_registry_s3_bucket, config_yaml
            )
        elif logging_style == "wandb":
            # For WandB we upload the model first, then link it
            upload_model_to_s3(
                onnx_model, cfg.model.name, cfg.model_registry_s3_bucket, config_yaml
            )
            model_artifact = wandb.Artifact(cfg.model.name, type="model")
            s3_path = f"s3://{cfg.model_registry_s3_bucket}/{cfg.model.name}/model.onnx"
            model_artifact.add_reference(s3_path)
            run.log_artifact(model_artifact)
            run.link_artifact(
                model_artifact,
                f"{cfg.logging.wandb_org_name}/wandb-registry-model/{PROJECT_NAME}_{cfg.model.name}",
            )
            run.finish()


if __name__ == "__main__":
    train_model(
        model_config="default",
        logging_config="default",
        # override_args={"training.features.list": ["NDMI"]},
    )
