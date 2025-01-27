# -*- coding: utf-8 -*-
# Copyright 2024 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import threading

from google.cloud import storage
from google.api_core import exceptions as gcs_exceptions

from twisted.internet import defer, reactor, threads
from twisted.python.failure import Failure
from twisted.python.threadpool import ThreadPool

from synapse.logging.context import LoggingContext, make_deferred_yieldable
from synapse.rest.media.v1._base import Responder
from synapse.rest.media.v1.storage_provider import StorageProvider

# Synapse 1.13.0 moved current_context to a module-level function.
try:
    from synapse.logging.context import current_context
except ImportError:
    current_context = LoggingContext.current_context

logger = logging.getLogger("synapse.gcs")

# The list of valid GCS storage class names
_VALID_STORAGE_CLASSES = (
    "STANDARD",
    "NEARLINE",
    "COLDLINE",
    "ARCHIVE",
)

# Chunk size to use when reading from GCS connection in bytes
READ_CHUNK_SIZE = 16 * 1024


class GCSStorageProviderBackend(StorageProvider):
    """
    Args:
        hs (HomeServer)
        config: The config returned by `parse_config`
    """

    def __init__(self, hs, config):
        self.cache_directory = hs.config.media.media_store_path
        self.bucket_name = config["bucket"]
        self.prefix = config["prefix"]
        self.storage_class = config["storage_class"]
        self.project = config.get("project")
        self.credentials = config.get("credentials")

        self._gcs_client = None
        self._gcs_client_lock = threading.Lock()

        threadpool_size = config.get("threadpool_size", 40)
        self._gcs_pool = ThreadPool(name="gcs-pool", maxthreads=threadpool_size)
        self._gcs_pool.start()

        # Manually stop the thread pool on shutdown to avoid ~30s delay
        reactor.addSystemEventTrigger(
            "during", "shutdown", self._gcs_pool.stop,
        )

    def _get_gcs_client(self):
        # Thread-safe singleton pattern for GCS client
        if self._gcs_client:
            return self._gcs_client

        with self._gcs_client_lock:
            if not self._gcs_client:
                client_args = {}
                if self.project:
                    client_args["project"] = self.project
                if self.credentials:
                    client_args["credentials"] = self.credentials

                self._gcs_client = storage.Client(**client_args)
            return self._gcs_client

    def store_file(self, path, file_info):
        """See StorageProvider.store_file"""

        parent_logcontext = current_context()

        def _store_file():
            with LoggingContext(parent_context=parent_logcontext):
                client = self._get_gcs_client()
                bucket = client.bucket(self.bucket_name)
                blob = bucket.blob(self.prefix + path)
                blob.storage_class = self.storage_class

                blob.upload_from_filename(os.path.join(self.cache_directory, path))

        return make_deferred_yieldable(
            threads.deferToThreadPool(reactor, self._gcs_pool, _store_file)
        )

    def fetch(self, path, file_info):
        """See StorageProvider.fetch"""
        logcontext = current_context()

        d = defer.Deferred()

        def _get_file():
            gcs_download_task(
                self._get_gcs_client(), self.bucket_name, self.prefix + path, d, logcontext
            )

        self._gcs_pool.callInThread(_get_file)
        return make_deferred_yieldable(d)

    @staticmethod
    def parse_config(config):
        """Called on startup to parse config supplied. This should parse
        the config and raise if there is a problem.

        The returned value is passed into the constructor.

        In this case we return a dict with fields: bucket, prefix, storage_class,
        and optionally project and credentials.
        """
        bucket = config["bucket"]
        prefix = config.get("prefix", "")
        storage_class = config.get("storage_class", "STANDARD")

        assert isinstance(bucket, str)
        assert storage_class in _VALID_STORAGE_CLASSES

        result = {
            "bucket": bucket,
            "prefix": prefix,
            "storage_class": storage_class,
        }

        if "project" in config:
            result["project"] = config["project"]

        if "credentials" in config:
            result["credentials"] = config["credentials"]

        if "threadpool_size" in config:
            result["threadpool_size"] = config["threadpool_size"]

        return result


def gcs_download_task(gcs_client, bucket_name, key, deferred, parent_logcontext):
    """Attempts to download a file from GCS.

    Args:
        gcs_client: google.cloud.storage.Client instance
        bucket_name (str): The GCS bucket which may have the file
        key (str): The key of the file
        deferred (Deferred[_GCSResponder|None]): If file exists
            resolved with an _GCSResponder instance, if it doesn't
            exist then resolves with None.
        parent_logcontext (LoggingContext): the logcontext to report logs and metrics
            against.
    """
    with LoggingContext(parent_context=parent_logcontext):
        logger.info("Fetching %s from GCS", key)

        try:
            bucket = gcs_client.bucket(bucket_name)
            blob = bucket.get_blob(key)

            if not blob:
                logger.info("Media %s not found in GCS", key)
                reactor.callFromThread(deferred.callback, None)
                return

            producer = _GCSResponder()
            reactor.callFromThread(deferred.callback, producer)

            # Download in chunks
            stream = blob.download_as_bytes(chunk_size=READ_CHUNK_SIZE)
            _stream_to_producer(reactor, producer, stream, timeout=90.0)

        except gcs_exceptions.NotFound:
            logger.info("Media %s not found in GCS", key)
            reactor.callFromThread(deferred.callback, None)
            return
        except Exception:
            reactor.callFromThread(deferred.errback, Failure())
            return


def _stream_to_producer(reactor, producer, stream, status=None, timeout=None):
    """Streams a file like object to the producer.

    Correctly handles producer being paused/resumed/stopped.

    Args:
        reactor
        producer (_GCSResponder): Producer object to stream results to
        stream: The GCS download stream to read from
        status (_ProducerStatus|None): Used to track whether we're currently
            paused or not. Used for testing
        timeout (float|None): Timeout in seconds to wait for consume to resume
            after being paused
    """

    # Set when we should be producing, cleared when we are paused
    wakeup_event = producer.wakeup_event

    # Set if we should stop producing forever
    stop_event = producer.stop_event

    if not status:
        status = _ProducerStatus()

    try:
        for chunk in stream:
            if stop_event.is_set():
                return

            reactor.callFromThread(producer._write, chunk)

            # After writing the chunk, check if we need to pause
            # We wait for the producer to signal that the consumer wants
            # more data (or we should abort)
            if not wakeup_event.is_set():
                status.set_paused(True)
                ret = wakeup_event.wait(timeout)
                if not ret:
                    raise Exception("Timed out waiting to resume")
                status.set_paused(False)

            # Check if we were woken up so that we should abort the download
            if stop_event.is_set():
                return

    except Exception:
        reactor.callFromThread(producer._error, Failure())
    finally:
        reactor.callFromThread(producer._finish)


class _GCSResponder(Responder):
    """A Responder for GCS. Created by gcs_download_task
    """

    def __init__(self):
        # Triggered by responder when more data has been requested (or
        # stop_event has been triggered)
        self.wakeup_event = threading.Event()
        # Triggered by responder when we should abort the download.
        self.stop_event = threading.Event()

        # The consumer we're registered to
        self.consumer = None

        # The deferred returned by write_to_consumer, which should resolve when
        # all the data has been written (or there has been a fatal error).
        self.deferred = defer.Deferred()

    def write_to_consumer(self, consumer):
        """See Responder.write_to_consumer
        """
        self.consumer = consumer
        # We are a IPushProducer, so we start producing immediately until we
        # get a pauseProducing or stopProducing
        consumer.registerProducer(self, True)
        self.wakeup_event.set()
        return make_deferred_yieldable(self.deferred)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        self.wakeup_event.set()

    def resumeProducing(self):
        """See IPushProducer.resumeProducing
        """
        # The consumer is asking for more data, signal gcs_download_task
        self.wakeup_event.set()

    def pauseProducing(self):
        """See IPushProducer.stopProducing
        """
        self.wakeup_event.clear()

    def stopProducing(self):
        """See IPushProducer.stopProducing
        """
        # The consumer wants no more data ever, signal gcs_download_task
        self.stop_event.set()
        self.wakeup_event.set()
        if not self.deferred.called:
            self.deferred.errback(Exception("Consumer ask to stop producing"))

    def _write(self, chunk):
        """Writes the chunk of data to consumer. Called by gcs_download_task.
        """
        if self.consumer and not self.stop_event.is_set():
            self.consumer.write(chunk)

    def _error(self, failure):
        """Called when a fatal error occurred while getting data. Called by
        gcs_download_task.
        """
        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.errback(failure)

    def _finish(self):
        """Called when there is no more data to write. Called by gcs_download_task.
        """
        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.callback(None)


class _ProducerStatus(object):
    """Used to track whether the GCS download thread is currently paused
    waiting for consumer to resume. Used for testing.
    """

    def __init__(self):
        self.is_paused = threading.Event()
        self.is_paused.clear()

    def wait_until_paused(self, timeout=None):
        is_paused = self.is_paused.wait(timeout)
        if not is_paused:
            raise Exception("Timed out waiting")

    def set_paused(self, paused):
        if paused:
            self.is_paused.set()
        else:
            self.is_paused.clear()