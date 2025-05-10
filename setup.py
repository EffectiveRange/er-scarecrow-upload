from setuptools import setup, find_packages

setup(
    name="er-scarecrow-upload",
    description="Service for uploading files to Google Drive using a service account",
    long_description="Service for uploading files to Google Drive using a service account",
    author="Ferenc Nandor Janky & Attila Gombos",
    author_email="info@effective-range.com",
    maintainer="Ferenc Nandor Janky & Attila Gombos",
    maintainer_email="info@effective-range.com",
    packages=find_packages(exclude=["tests"]),
    entry_points={
        "console_scripts": [
            "er-scarecrow-upload=er_scarecrow_upload.upload:main",
            "er-scarecrow-fetch=er_scarecrow_upload.fetch:main",
            "er-scarecrow-fetch-upload=er_scarecrow_upload.fetch_upload:main",
        ],
    },
    use_scm_version=True,
    setup_requires=["setuptools_scm"],
    install_requires=[
        "google-api-python-client>=2.90.0",
        "google-auth>=1.5.1",
        "google-auth-httplib2>=0.1.0",
        "google-auth-oauthlib>=0.4.2",
        "fabric",
        "pytz",
        "retrying",
        "tenacity",
        "python-context-logger@git+https://github.com/EffectiveRange/python-context-logger.git@latest",
    ]
)
