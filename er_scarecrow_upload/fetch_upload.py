from er_scarecrow_upload.fetch import get_parser as fetch_get_parser
from er_scarecrow_upload.upload import get_parser as upload_get_parser
from er_scarecrow_upload.common import init_application


def main() -> None:
    init_application(
        "er-scarecrow-fetch-upload",
        "Fetch files from remote hosts and archive them and upload them to Google Drive",
        fetch_get_parser,
        upload_get_parser,
    )


if __name__ == "__main__":
    main()
