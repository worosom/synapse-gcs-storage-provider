from setuptools import setup

__version__ = "1.0.1"

with open("README.md") as f:
    long_description = f.read()

setup(
    name="synapse-gcs-storage-provider",
    version=__version__,
    zip_safe=False,
    author="matrix.org team and contributors",
    description="A storage provider which can fetch and store media in Google Cloud Storage.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/worosom/synapse-gcs-storage-provider",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
    ],
    py_modules=["gcs_storage_provider"],
    scripts=["scripts/gcs_media_upload"],
    install_requires=[
        "google-cloud-storage>=2.14.0,<3.0",
        "humanize>=4.0,<5.0",
        "psycopg2-binary>=2.7.5,<3.0",
        "PyYAML>=5.4,<7.0",
        "tqdm>=4.26.0,<5.0",
        "Twisted",
    ],
)
