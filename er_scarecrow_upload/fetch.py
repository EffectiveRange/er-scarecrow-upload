import os
import re
import shlex
import subprocess
from argparse import ArgumentParser
from datetime import datetime, timedelta, tzinfo, timezone
from logging import Logger
from pathlib import Path
from typing import Any, Optional

import pytz
import retrying
from context_logger import get_logger
from fabric import Connection

from er_scarecrow_upload.common import init_application

APPLICATION = "er-scarecrow-fetch"

FILENAME_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"T(?P<hour>\d{2})_(?P<minute>\d{2})_(?P<second>\d{2})"
    r"\.(?P<microsecond>\d+)"
    r"(?P<tz>[pm]\d{4})"
)


def log_before_retry(exc: Any) -> bool:
    # log the exception with traceback
    get_logger(APPLICATION).warning("⚠️  Operation failed, retrying…")
    # returning True means “yes, please retry”
    return True


def _timezone_postfix_from_datetime(time_value: datetime) -> str:
    offset = time_value.utcoffset()
    if offset is None:
        raise ValueError("Timezone-aware datetime values are required for --collect time ranges")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "p" if total_minutes >= 0 else "m"
    abs_minutes = abs(total_minutes)
    return f"{sign}{abs_minutes // 60:02d}{abs_minutes % 60:02d}"


def _parse_filename_timestamp(filename: str) -> Optional[datetime]:
    match = FILENAME_TIMESTAMP_PATTERN.match(filename)
    if not match:
        return None

    tz_chunk = match.group("tz")
    sign = 1 if tz_chunk[0] == "p" else -1
    hours = int(tz_chunk[1:3])
    minutes = int(tz_chunk[3:5])
    file_timezone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    microsecond = int(match.group("microsecond")[:6].ljust(6, "0"))

    return datetime(
        year=int(match.group("date")[0:4]),
        month=int(match.group("date")[5:7]),
        day=int(match.group("date")[8:10]),
        hour=int(match.group("hour")),
        minute=int(match.group("minute")),
        second=int(match.group("second")),
        microsecond=microsecond,
        tzinfo=file_timezone,
    )


@retrying.retry(stop_max_attempt_number=3, wait_fixed=10000, retry_on_exception=log_before_retry)
def collect_event_files(logger: Logger, ssh_alias: str, event_id: str, timeout: int,
                        remote_directory: str, local_directory: str) -> None:
    with Connection(ssh_alias, connect_timeout=timeout) as connection:
        pattern = f"*{event_id}*"
        result = connection.run(
            f"find {shlex.quote(remote_directory)} -maxdepth 1 -type f -name {shlex.quote(pattern)}",
            hide=True,
            warn=True,
        )

        if not result or not result.ok or not result.stdout.strip():
            logger.warning(f"⚠️  No files found for event ID '{event_id}' on host '{ssh_alias}'")
            return

        remote_file_paths = result.stdout.splitlines()
        logger.info(f"ℹ️  Found {len(remote_file_paths)} file(s) for event ID '{event_id}' on host '{ssh_alias}'")

        os.makedirs(local_directory, exist_ok=True)

        for remote_file_path in remote_file_paths:
            local_path = Path(local_directory) / Path(remote_file_path).name
            logger.info(f"ℹ️  Downloading file '{remote_file_path}' from host '{ssh_alias}' to '{local_path}'...")

            command = [
                "rsync",
                "-avz",
                "--partial",
                "--append-verify",
                f"{ssh_alias}:{remote_file_path}",
                str(local_path),
            ]
            subprocess.run(command, check=True)

            logger.info(f"✅  Downloaded file {remote_file_path} from host '{ssh_alias}' to {local_path}")
            connection.run(f"sudo rm -- {shlex.quote(remote_file_path)}", warn=True)


@retrying.retry(stop_max_attempt_number=3, wait_fixed=5000, retry_on_exception=log_before_retry)
def collect_files(logger: Logger, ssh_alias: str, timeout: int, remote_directory: str, local_directory: str,
                  collect_directory: str, start_time: datetime, end_time: datetime) -> None:
    if start_time.tzinfo is None or start_time.utcoffset() is None:
        raise ValueError("start_time must include timezone information")
    if end_time.tzinfo is None or end_time.utcoffset() is None:
        raise ValueError("end_time must include timezone information")

    range_start, range_end = sorted((start_time, end_time))
    tz_postfix = _timezone_postfix_from_datetime(range_start)
    if _timezone_postfix_from_datetime(range_end) != tz_postfix:
        raise ValueError("start_time and end_time must have the same UTC offset for directory matching")

    range_start_utc = range_start.astimezone(timezone.utc)
    range_end_utc = range_end.astimezone(timezone.utc)

    start_minute = range_start.replace(second=0, microsecond=0)
    end_minute = range_end.replace(second=0, microsecond=0)
    minute_count = int((end_minute - start_minute).total_seconds() // 60) + 1
    minute_window = [start_minute + timedelta(minutes=i) for i in range(minute_count)]

    with Connection(ssh_alias, connect_timeout=timeout) as connection:
        connection.run(f"sudo mkdir -p {shlex.quote(collect_directory)}")
        try:
            collected_count = 0
            for minute in minute_window:
                source_dir = (
                        Path(remote_directory)
                        / f"{minute.year}-{minute.month:02d}-{minute.day:02d}"
                        / f"{minute.hour:02d}"
                        / f"{minute.minute:02d}_{tz_postfix}"
                )
                list_result = connection.run(
                    f"cd {shlex.quote(str(source_dir))} && find . -maxdepth 1 -mindepth 1 -type f -printf '%f\\n'",
                    hide=True,
                    warn=True,
                )
                if not list_result.ok:
                    logger.debug(f"ℹ️  Skipping missing directory {source_dir}")
                    continue

                selected_files: list[str] = []
                for filename in list_result.stdout.splitlines():
                    file_time = _parse_filename_timestamp(filename)
                    if file_time is None:
                        continue
                    file_time_utc = file_time.astimezone(timezone.utc)
                    if range_start_utc <= file_time_utc <= range_end_utc:
                        selected_files.append(filename)

                if not selected_files:
                    continue

                logger.info(f"ℹ️  Collecting {len(selected_files)} files from {source_dir} to {collect_directory}")
                quoted_files = " ".join(shlex.quote(file_name) for file_name in selected_files)
                connection.run(
                    f"cd {shlex.quote(str(source_dir))} && sudo cp -t {shlex.quote(collect_directory)} {quoted_files}"
                )
                collected_count += len(selected_files)

            os.makedirs(local_directory, exist_ok=True)
            logger.info(f"ℹ️  Downloading files from {collect_directory} to {local_directory}")
            command = ["rsync", "-avz", f"{ssh_alias}:{collect_directory}/", local_directory]
            subprocess.run(command, check=True)
        finally:
            connection.run(f"sudo rm -rf {shlex.quote(collect_directory)}", warn=True)

    logger.info(
        f"✅  Downloaded {collected_count} files from {remote_directory} to {local_directory} "
        f"between {range_start.isoformat()} and {range_end.isoformat()}"
    )


@retrying.retry(stop_max_attempt_number=3, wait_fixed=5000, retry_on_exception=log_before_retry)
def collect_and_download_files(logger: Logger, ssh_alias: str, timeout: int, remote_directory: str,
                               local_directory: str, collect_directory: str, time_window: list[datetime],
                               tz_offset: int = 0) -> None:
    if len(time_window) == 2:
        start_time = min(time_window)
        end_time = max(time_window)
        time_window = [start_time + timedelta(minutes=i) for i in
                       range(int((end_time - start_time).total_seconds() // 60) + 1)]

    with Connection(ssh_alias, connect_timeout=timeout) as connection:
        tz_postfix = f"{'p' if tz_offset >= 0 else 'm'}{abs(tz_offset):02d}00"
        source_dirs = [
            (Path(remote_directory)
             / f"{offset_time.year}-{offset_time.month:02d}-{offset_time.day:02d}"
             / f"{offset_time.hour:02d}"
             / f"{offset_time.minute:02d}_{tz_postfix}")
            for time in time_window
            for offset_time in [time.astimezone(timezone(timedelta(hours=tz_offset)))]
        ]

        connection.run(f"sudo mkdir -p {collect_directory}")

        for source_dir in source_dirs:
            logger.info(f"ℹ️  Collecting files from {source_dir} to {collect_directory}")
            connection.run(f"cd {source_dir} && sudo find . -type f -exec cp -t {collect_directory} {{}} +")

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
        "--event-id",
        type=str,
        help="Download file(s) matching this event ID from the top level of --remote-directory.",
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
        help=(
            "Comma separated start,end timestamps for --collect. "
            "Use timezone-aware ISO values, e.g. 'YYYY-MM-DDTHH:MM+01:00,YYYY-MM-DDTHH:MM+01:00'."
        ),
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
        if args.event_id:
            collect_event_files(
                logger,
                ssh_alias,
                args.event_id,
                args.timeout,
                args.remote_directory,
                args.local_directory,
            )
        elif args.archive:
            download_and_archive_files(
                logger,
                ssh_alias,
                args.timeout,
                args.remote_directory,
                args.local_directory,
                pytz.timezone(args.timezone),
                args.since_days
            )
        elif args.collect:
            if not args.time_window:
                raise ValueError("--time-window is required when using --collect")
            time_window = [datetime.fromisoformat(timestamp) for timestamp in args.time_window.split(",")]
            if len(time_window) < 2:
                raise ValueError("--time-window for --collect must include start and end timestamps")
            if any(time_value.tzinfo is None or time_value.utcoffset() is None for time_value in time_window):
                raise ValueError("--time-window timestamps must include timezone offsets, e.g. +01:00")
            collect_files(
                logger,
                ssh_alias,
                args.timeout,
                args.remote_directory,
                args.local_directory,
                args.collect_directory,
                min(time_window),
                max(time_window),
            )
        else:
            raise ValueError("Specify one mode: --event-id, --archive, or --collect")


if __name__ == "__main__":
    main()
