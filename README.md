Synapse GCS Storage Provider
===========================

This module can be used by synapse as a storage provider, allowing it to fetch
and store media in Google Cloud Storage (GCS).


Usage
-----

The `gcs_storage_provider.py` should be on the PYTHONPATH when starting
synapse.

Example of entry in synapse config:

```yaml
media_storage_providers:
- module: gcs_storage_provider.GCSStorageProviderBackend
  store_local: True
  store_remote: True
  store_synchronous: True
  config:
    bucket: <GCS_BUCKET_NAME>
    # All of the below options are optional. If not provided, the provider
    # will use Application Default Credentials.
    project: <GCP_PROJECT_ID>
    credentials: /path/to/service-account.json

    # The storage class used when uploading files to the bucket.
    # Default is STANDARD.
    # Valid options: STANDARD, NEARLINE, COLDLINE, ARCHIVE
    #storage_class: "NEARLINE"

    # Prefix for all media in bucket, can't be changed once media has been uploaded
    # Useful if sharing the bucket between Synapses
    # Blank if not provided
    #prefix: "prefix/to/files/in/bucket"

    # The maximum number of concurrent threads which will be used to connect
    # to GCS. Each thread manages a single connection. Default is 40.
    #threadpool_size: 20
```

This module uses `google-cloud-storage`, and so the credentials can be provided in several ways:
1. Application Default Credentials (recommended in GCP environments)
2. Service account JSON file specified in the config
3. Environment variables (GOOGLE_APPLICATION_CREDENTIALS)

For more details, see the [Google Cloud authentication documentation](https://cloud.google.com/docs/authentication).

Regular cleanup job
-------------------

There is additionally a script at `scripts/gcs_media_upload` which can be used
in a regular job to upload content to GCS, then delete that from local disk.
This script can be used in combination with configuration for the storage
provider to pull media from GCS, but upload it asynchronously.

Once the package is installed, the script should be run somewhat like the
following. We suggest using `tmux` or `screen` as these can take a long time
on larger servers.

`database.yaml` should contain the keys that would be passed to psycopg2 to
connect to your database. They can be found in the contents of the
`database`.`args` parameter in your homeserver.yaml.

More options are available in the command help.

```bash
# cache.db will be created if absent. database.yaml is required to
# contain database credentials
> ls
cache.db database.yaml

# Update cache from /path/to/media/store looking for files not used
# within 2 months
> gcs_media_upload update-db 2m
Syncing files that haven't been accessed since: 2024-01-18 11:06:21.520602
Synced 0 new rows
100%|█████████████████████████████████████████████████████████████| 1074/1074 [00:33<00:00, 25.97files/s]
Updated 0 as deleted

# Upload to GCS with service account credentials
> gcs_media_upload upload /path/to/media/store \
    --bucket matrix-media-bucket \
    --storage-class NEARLINE \
    --project my-project \
    --credentials /path/to/service-account.json \
    --delete

# Or upload using Application Default Credentials
> gcs_media_upload upload /path/to/media/store \
    --bucket matrix-media-bucket \
    --storage-class NEARLINE \
    --delete

# prepare to wait a long time
```

Packaging and release
---------

For maintainers:

1. Update the `__version__` in setup.py. Commit. Push.
2. Create a release on GitHub for this version.
3. When published, a [GitHub action workflow](https://github.com/matrix-org/synapse-s3-storage-provider/actions/workflows/release.yml) will build the package and upload to [PyPI](https://pypi.org/project/synapse-s3-storage-provider/).
