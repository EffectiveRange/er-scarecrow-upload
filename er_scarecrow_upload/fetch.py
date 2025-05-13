import os
import subprocess
from argparse import ArgumentParser
from datetime import datetime, timedelta, tzinfo
from logging import Logger
from pathlib import Path
from typing import Any, Optional

import pytz
import retrying
from context_logger import get_logger
from fabric import Connection

from er_scarecrow_upload.common import init_application

APPLICATION = "er-scarecrow-fetch"


def log_before_retry(exc: Any) -> bool:
    # log the exception with traceback
    get_logger(APPLICATION).warning("⚠️  Operation failed, retrying…")
    # returning True means “yes, please retry”
    return True


@retrying.retry(stop_max_attempt_number=3, wait_fixed=5000, retry_on_exception=log_before_retry)
def collect_and_download_files(logger: Logger, ssh_alias: str, timeout: int, remote_directory: str,
                               local_directory: str, collect_directory: str, time_window: list[datetime]) -> None:
    if len(time_window) == 2:
        start_time = min(time_window)
        end_time = max(time_window)
        time_window = [start_time + timedelta(minutes=i) for i in
                       range(int((end_time - start_time).total_seconds() // 60) + 1)]

    with Connection(ssh_alias, connect_timeout=timeout) as connection:
        source_dirs = [(Path(remote_directory)
                        / f"{time.year}-{time.month:02d}-{time.day:02d}"
                        / f"{time.hour:02d}"
                        / f"{time.minute:02d}_p200") for time in time_window]

        connection.run(f"sudo mkdir -p {collect_directory}")

        for source_dir in source_dirs:
            logger.info(f"ℹ️  Collecting files from {source_dir} to {collect_directory}")
            connection.run(f"cd {source_dir} && find . -type f -print0 | xargs -0 sudo ln -f -t {collect_directory}")

    os.makedirs(local_directory, exist_ok=True)

    logger.info(f"ℹ️  Downloading files from {collect_directory} to {local_directory}")
    command = ["rsync", "-avz", f"{ssh_alias}:{collect_directory}/", local_directory]
    subprocess.run(command, check=True)

    connection.run(f"sudo rm -rf {collect_directory}")
    logger.info(f"✅  Downloaded files from {remote_directory} to {local_directory} "
                f"between {time_window[0]} and {time_window[-1]}")


@retrying.retry(stop_max_attempt_number=3, wait_fixed=5000, retry_on_exception=log_before_retry)
def download_and_archive_files(logger: Logger, ssh_alias: str, timeout: int,
                               remote_directory: str, local_directory: str,
                               timezone: tzinfo, since_days: Optional[int] = None) -> Optional[Path]:
    """
    Connects to a remote host using an SSH alias, downloads files matching the current date pattern,
    and archives them into a tar file locally.
    :param logger: Logger instance for logging messages.
    :param ssh_alias: SSH config alias for the remote host.
    :param remote_directory: Directory on the remote server to search for files.
    :param local_directory: Name of the local directory.
    :param timezone: Timezone to use for date formatting.
    :param timeout: Timeout for SSH connection in seconds.
    :param since_days: Number of days to look back for files.
    """
    # Get the current date in the format %Y-%m-%d
    start = datetime.now(timezone)
    # TODO parametrize the since
    current_date = (start if since_days is None else (start - timedelta(days=since_days))).strftime("%Y-%m-%d")
    dest_date = start.strftime("%Y-%m-%d_%H-%M-%S")
    pattern = f"{current_date}T"

    # Establish an SSH connection using Fabric
    with Connection(ssh_alias, connect_timeout=timeout) as conn:

        # List files in the remote directory
        result = conn.run(f"find {remote_directory} -name {pattern}\\* -type f", hide=True)
        files_to_download = result.stdout.splitlines()

        if not files_to_download:
            logger.info(f"⚠️  No files found matching the pattern on host '{ssh_alias}'.")
            return None
        else:
            archive_file = f"/tmp/{ssh_alias}_{current_date}.tar"
            dest_archive_file = f"{ssh_alias}_{current_date}_{dest_date}.tar"
            conn.run(
                f"cd {remote_directory} && find ./ -name {pattern}\\* -type f -print0 | sudo tar --null "
                f"--transform='s|.*/||' -cvf {archive_file} --remove-files  --files-from=-",
                hide=True,
            )
            logger.debug(f"ℹ️  archive file is {archive_file}")
            os.makedirs(Path(local_directory) / ssh_alias, exist_ok=True)
            dest_file = Path(local_directory) / ssh_alias / dest_archive_file
            conn.get(archive_file, str(dest_file))
            logger.debug(f"✅  Downloaded archive file: {archive_file} to {str(dest_file)}")
            return dest_file


def get_parser(parser: ArgumentParser) -> ArgumentParser:
    parser.add_argument(
        "--source",
        type=str,
        nargs="+",
        help="List of SSH config aliases for the remote hosts (defined in ~/.ssh/config).",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Download and archive files from the remote host",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Collect files from the remote host and download them",
    )
    parser.add_argument(
        "--remote-directory",
        type=str,
        help="Directory on the remote server to search for files.",
        default="/var/local/scarecrow/detected/",
    )
    parser.add_argument(
        "--collect-directory",
        type=str,
        help="Temporary directory on the remote server to collect for files.",
        default="/var/local/scarecrow/collect/",
    )
    parser.add_argument(
        "--local-directory",
        type=str,
        help="Base name of the local tar archive to create. Host-specific suffixes will be added.",
        default="/var/local/er-scarecrow-upload/",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="Europe/Budapest",
        help="Timezone to use for date formatting.",
    )
    parser.add_argument("--since-days", type=int, default=None, help="Number of days to look back for files.")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout for SSH connection in seconds.")
    parser.add_argument(
        "--time-window",
        type=str,
        help="Comma separated list of timestamps to collect files from, in the format 'YYYY-MM-DDTHH:MM'.",
    )
    return parser


def main() -> None:
    # Set up argument parsing
    args, logger = init_application(
        "er-scarecrow-fetch",
        "Fetch files from remote hosts and archive them",
        get_parser,
    )
    # Iterate over all specified SSH aliases
    for ssh_alias in args.source:
        logger.info(f"ℹ️   Processing host '{ssh_alias}'")
        if args.archive:
            download_and_archive_files(
                logger,
                ssh_alias,
                args.timeout,
                args.remote_directory,
                args.local_directory,
                pytz.timezone(args.timezone)
            )
        elif args.collect:
            time_window = [datetime.fromisoformat(timestamp) for timestamp in args.time_window.split(",")]
            collect_and_download_files(
                logger,
                ssh_alias,
                args.timeout,
                args.remote_directory,
                args.local_directory,
                args.collect_directory,
                time_window)


if __name__ == "__main__":
    main()
