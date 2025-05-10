import argparse
from logging import Logger
from typing import Any

from context_logger import get_logger, setup_logging


def init_application(app_name: str, description: str, *parser_inits: Any) -> tuple[argparse.Namespace, Logger]:
    # Set up argument parsing
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--log-file",
        type=str,
        help="Path to the log file.",
        default=f"/var/log/effective-range/{app_name}/{app_name}.log",
    )
    parser.add_argument("--log-level", type=str, help="Log level (e.g., DEBUG, INFO, WARNING, ERROR).", default="INFO")
    for parser_init in parser_inits:
        parser = parser_init(parser)
    # Parse the arguments
    args = parser.parse_args()
    setup_logging(app_name, log_file_path=args.log_file, log_level=args.log_level)
    logger = get_logger(app_name)
    return args, logger
