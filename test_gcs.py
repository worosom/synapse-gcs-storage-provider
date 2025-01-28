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

from twisted.internet import defer
from twisted.python.failure import Failure
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial import unittest

import sys
import os
import tempfile

is_py2 = sys.version[0] == "2"
if is_py2:
    from Queue import Queue
else:
    from queue import Queue

from threading import Event, Thread

from mock import Mock, patch
from contextlib import contextmanager

from gcs_storage_provider import (
    _stream_to_producer,
    _GCSResponder,
    _ProducerStatus,
    GCSStorageProviderBackend,
)

# Mock LoggingContext for testing
@contextmanager
def LoggingContextMock(*args, **kwargs):
    yield


class StreamingProducerTestCase(unittest.TestCase):
    def setUp(self):
        self.reactor = ThreadedMemoryReactorClock()

        self.body = Channel()
        self.consumer = Mock()
        self.written = ""

        def write(data):
            self.written += data

        self.consumer.write.side_effect = write

        self.producer_status = _ProducerStatus()
        self.producer = _GCSResponder()
        self.thread = Thread(
            target=_stream_to_producer,
            args=(self.reactor, self.producer, self.body),
            kwargs={"status": self.producer_status, "timeout": 1.0},
        )
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        # Really ensure that we've stopped the thread
        self.producer.stopProducing()

    def test_simple_produce(self):
        deferred = self.producer.write_to_consumer(self.consumer)

        self.body.write("test")
        self.wait_for_thread()
        self.assertEqual("test", self.written)

        self.body.write(" string")
        self.wait_for_thread()
        self.assertEqual("test string", self.written)

        self.body.finish()
        self.wait_for_thread()

        self.assertTrue(deferred.called)
        self.assertEqual(deferred.result, None)

    def test_pause_produce(self):
        deferred = self.producer.write_to_consumer(self.consumer)

        self.body.write("test")
        self.wait_for_thread()
        self.assertEqual("test", self.written)

        # We pause producing, but the thread will currently be blocked waiting
        # to read data, so we wake it up by writing before asserting that
        # it actually pauses.
        self.producer.pauseProducing()
        self.body.write(" string")
        self.wait_for_thread()
        self.producer_status.wait_until_paused(10.0)
        self.assertEqual("test string", self.written)

        # If we write again we remain paused and nothing gets written
        self.body.write(" second")
        self.producer_status.wait_until_paused(10.0)
        self.assertEqual("test string", self.written)

        # If we call resumeProducing the buffered data gets read and written.
        self.producer.resumeProducing()
        self.wait_for_thread()
        self.assertEqual("test string second", self.written)

        # We can continue writing as normal now
        self.body.write(" third")
        self.wait_for_thread()
        self.assertEqual("test string second third", self.written)

        self.body.finish()
        self.wait_for_thread()

        self.assertTrue(deferred.called)
        self.assertEqual(deferred.result, None)

    def test_error(self):
        deferred = self.producer.write_to_consumer(self.consumer)

        self.body.write("test")
        self.wait_for_thread()
        self.assertEqual("test", self.written)

        excp = Exception("Test Exception")
        self.body.error(excp)
        self.wait_for_thread()

        self.failureResultOf(deferred, Exception)

    def wait_for_thread(self):
        """Wait for something to call `callFromThread` and advance reactor
        """
        self.reactor.thread_event.wait(1)
        self.reactor.thread_event.clear()
        self.reactor.advance(0)


class ThreadedMemoryReactorClock(MemoryReactorClock):
    """
    A MemoryReactorClock that supports callFromThread.
    """

    def __init__(self):
        super(ThreadedMemoryReactorClock, self).__init__()
        self.thread_event = Event()

    def callFromThread(self, callback, *args, **kwargs):
        """
        Make the callback fire in the next reactor iteration.
        """
        d = defer.Deferred()
        d.addCallback(lambda x: callback(*args, **kwargs))
        self.callLater(0, d.callback, True)

        self.thread_event.set()

        return d

    def callInThreadWithCallback(self, onResult, f, *args, **kw):
        """
        Call a function in a thread and call onResult with the result.
        """
        def wrapped():
            try:
                result = f(*args, **kw)
                success = True
            except:
                result = Failure()
                success = False
            
            self.callFromThread(onResult, success, result)
            
        wrapped()


class Channel(object):
    """Simple channel to mimic a thread safe file like object
    """

    def __init__(self):
        self._queue = Queue()

    def __iter__(self):
        while True:
            val = self._queue.get()
            if val is None:
                break
            if isinstance(val, Exception):
                raise val
            yield val

    def write(self, val):
        self._queue.put(val)

    def error(self, err):
        self._queue.put(err)

    def finish(self):
        self._queue.put(None)


class GCSStorageProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.reactor = ThreadedMemoryReactorClock()
        
        # Mock the homeserver config
        self.hs = Mock()
        self.hs.config.media.media_store_path = "/test/media/path"
        
        # Basic GCS config
        self.config = {
            "bucket": "test-bucket",
            "prefix": "test-prefix/",
            "storage_class": "STANDARD",
        }
        
        self.provider = GCSStorageProviderBackend(self.hs, self.config)
        
        # Replace the thread pool with our test reactor
        self.provider._gcs_pool = self.reactor
    
    @patch("google.cloud.storage.Client")
    @patch("gcs_storage_provider.LoggingContext", new=LoggingContextMock)
    def test_store_file_mime_types(self, mock_storage_client):
        """Test that files are uploaded with correct MIME types"""
        # Set up mock GCS client and bucket
        mock_bucket = Mock()
        mock_blob = Mock()
        mock_client = mock_storage_client.return_value
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        
        # Set the mock client in the provider
        self.provider._gcs_client = mock_client
        
        test_cases = [
            # (file_path, expected_mime_type)
            ("test.jpg", "image/jpeg"),
            ("test.png", "image/png"),
            ("test.pdf", "application/pdf"),
            ("test.txt", "text/plain"),
            ("test.mp4", "video/mp4"),
            ("test_no_extension", "application/octet-stream"),
            ("test.", "application/octet-stream"),
        ]
        
        # Create a temporary directory for test files
        with tempfile.TemporaryDirectory() as temp_dir:
            self.provider.cache_directory = temp_dir
            
            for file_path, expected_mime_type in test_cases:
                # Reset mock for each test case
                mock_blob.reset_mock()
                
                # Create a temporary file
                full_path = os.path.join(temp_dir, file_path)
                with open(full_path, 'wb') as f:
                    f.write(b'test content')
                
                # Call store_file
                self.provider.store_file(file_path, {})
                
                # Advance reactor to process the deferred
                self.reactor.advance(0)
                
                # Verify the upload was called with correct content type
                mock_blob.upload_from_filename.assert_called_once()
                call_kwargs = mock_blob.upload_from_filename.call_args[1]
                self.assertEqual(
                    call_kwargs.get("content_type"),
                    expected_mime_type,
                    f"Wrong mime type for {file_path}. Expected {expected_mime_type}, got {call_kwargs.get('content_type')}"
                )