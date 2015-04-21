from __future__ import absolute_import

import atexit
import logging
import time

try:
    from queue import Empty, Full, Queue
except ImportError:
    from Queue import Empty, Full, Queue
from collections import defaultdict

from threading import Thread, Event

import six

from kafka.common import (
    ProduceRequest, TopicAndPartition, RetryOptions,
    UnsupportedCodecError, FailedPayloadsError,
    RequestTimedOutError, AsyncProducerQueueFull
)
from kafka.common import (
    RETRY_ERROR_TYPES, RETRY_BACKOFF_ERROR_TYPES, RETRY_REFRESH_ERROR_TYPES)

from kafka.protocol import CODEC_NONE, ALL_CODECS, create_message_set
from kafka.util import kafka_bytestring

log = logging.getLogger("kafka")

BATCH_SEND_DEFAULT_INTERVAL = 20
BATCH_SEND_MSG_COUNT = 20
BATCH_RETRY_OPTIONS = RetryOptions(
    limit=0, backoff_ms=300, retry_on_timeouts=False)

# unlimited
ASYNC_QUEUE_MAXSIZE = 0
STOP_ASYNC_PRODUCER = -1


def _send_upstream(queue, client, codec, batch_time, batch_size,
                   req_acks, ack_timeout, retry_options, stop_event):
    """
    Listen on the queue for a specified number of messages or till
    a specified timeout and send them upstream to the brokers in one
    request
    """
    reqs = []
    client.reinit()

    while not stop_event.is_set():
        timeout = batch_time

        # it's a simplification: we're comparing message sets and
        # messages: each set can contain [1..batch_size] messages
        count = batch_size - len(reqs)
        send_at = time.time() + timeout
        msgset = defaultdict(list)

        # Keep fetching till we gather enough messages or a
        # timeout is reached
        while count > 0 and timeout >= 0:
            try:
                topic_partition, msg, key = queue.get(timeout=timeout)
            except Empty:
                break

            # Check if the controller has requested us to stop
            if topic_partition == STOP_ASYNC_PRODUCER:
                stop_event.set()
                break

            # Adjust the timeout to match the remaining period
            count -= 1
            timeout = send_at - time.time()
            msgset[topic_partition].append((msg, key))

        # Send collected requests upstream
        for topic_partition, msg in msgset.items():
            messages = create_message_set(msg, codec, key)
            req = ProduceRequest(topic_partition.topic,
                                 topic_partition.partition,
                                 messages)
            reqs.append(req)

        if not reqs:
            continue

        reqs_to_retry = []
        try:
            client.send_produce_request(reqs,
                                        acks=req_acks,
                                        timeout=ack_timeout)

        except tuple(RETRY_ERROR_TYPES) as ex:

            # by default, retry all sent messages
            reqs_to_retry = reqs

            if type(ex) == FailedPayloadsError:
                reqs_to_retry = ex.failed_payloads

            elif (type(ex) == RequestTimedOutError and
                    not retry_options.retry_on_timeouts):
                reqs_to_retry = []

            # filter reqs_to_retry if there's a retry limit
            if retry_options.limit and retry_options.limit > 0:
                reqs_to_retry = [req._replace(retries=req.retries+1)
                    for req in reqs_to_retry
                    if req.retries < retry_options.limit]

            # doing backoff before next retry
            if (reqs_to_retry and type(ex) in RETRY_BACKOFF_ERROR_TYPES
                    and retry_options.backoff_ms):
                log.warning("Doing backoff for %s(ms)." % retry_options.backoff_ms)
                time.sleep(float(retry_options.backoff_ms) / 1000)

            # refresh topic metadata before next retry
            if reqs_to_retry and type(ex) in RETRY_REFRESH_ERROR_TYPES:
                client.load_metadata_for_topics()

        except Exception as ex:
            log.exception("Unable to send message: %s" % type(ex))

        finally:
            reqs = []

        if reqs_to_retry:
            reqs = reqs_to_retry


class Producer(object):
    """
    Base class to be used by producers

    Arguments:
        client: The Kafka client instance to use
        async: If set to true, the messages are sent asynchronously via another
            thread (process). We will not wait for a response to these
            WARNING!!! current implementation of async producer does not
            guarantee message delivery.  Use at your own risk! Or help us
            improve with a PR!
        req_acks: A value indicating the acknowledgements that the server must
            receive before responding to the request
        ack_timeout: Value (in milliseconds) indicating a timeout for waiting
            for an acknowledgement
        batch_send: If True, messages are send in batches
        batch_send_every_n: If set, messages are send in batches of this size
        batch_send_every_t: If set, messages are send after this timeout
    """

    ACK_NOT_REQUIRED = 0            # No ack is required
    ACK_AFTER_LOCAL_WRITE = 1       # Send response after it is written to log
    ACK_AFTER_CLUSTER_COMMIT = -1   # Send response after data is committed

    DEFAULT_ACK_TIMEOUT = 1000

    def __init__(self, client, async=False,
                 req_acks=ACK_AFTER_LOCAL_WRITE,
                 ack_timeout=DEFAULT_ACK_TIMEOUT,
                 codec=None,
                 batch_send=False,
                 batch_send_every_n=BATCH_SEND_MSG_COUNT,
                 batch_send_every_t=BATCH_SEND_DEFAULT_INTERVAL,
                 batch_retry_options=BATCH_RETRY_OPTIONS,
                 async_queue_maxsize=ASYNC_QUEUE_MAXSIZE):

        if batch_send:
            async = True
            assert batch_send_every_n > 0
            assert batch_send_every_t > 0
            assert async_queue_maxsize >= 0
        else:
            batch_send_every_n = 1
            batch_send_every_t = 3600

        self.client = client
        self.async = async
        self.req_acks = req_acks
        self.ack_timeout = ack_timeout
        self.stopped = False

        if codec is None:
            codec = CODEC_NONE
        elif codec not in ALL_CODECS:
            raise UnsupportedCodecError("Codec 0x%02x unsupported" % codec)

        self.codec = codec

        if self.async:
            # Messages are sent through this queue
            self.queue = Queue(async_queue_maxsize)
            self.thread_stop_event = Event()
            self.thread = Thread(target=_send_upstream,
                                 args=(self.queue,
                                       self.client.copy(),
                                       self.codec,
                                       batch_send_every_t,
                                       batch_send_every_n,
                                       self.req_acks,
                                       self.ack_timeout,
                                       batch_retry_options,
                                       self.thread_stop_event))

            # Thread will die if main thread exits
            self.thread.daemon = True
            self.thread.start()

            def cleanup(obj):
                if obj.stopped:
                    obj.stop()
            self._cleanup_func = cleanup
            atexit.register(cleanup, self)

    def send_messages(self, topic, partition, *msg):
        """
        Helper method to send produce requests
        @param: topic, name of topic for produce request -- type str
        @param: partition, partition number for produce request -- type int
        @param: *msg, one or more message payloads -- type bytes
        @returns: ResponseRequest returned by server
        raises on error

        Note that msg type *must* be encoded to bytes by user.
        Passing unicode message will not work, for example
        you should encode before calling send_messages via
        something like `unicode_message.encode('utf-8')`

        All messages produced via this method will set the message 'key' to Null
        """
        topic = kafka_bytestring(topic)
        return self._send_messages(topic, partition, *msg)

    def _send_messages(self, topic, partition, *msg, **kwargs):
        key = kwargs.pop('key', None)

        # Guarantee that msg is actually a list or tuple (should always be true)
        if not isinstance(msg, (list, tuple)):
            raise TypeError("msg is not a list or tuple!")

        # Raise TypeError if any message is not encoded as bytes
        if any(not isinstance(m, six.binary_type) for m in msg):
            raise TypeError("all produce message payloads must be type bytes")

        # Raise TypeError if topic is not encoded as bytes
        if not isinstance(topic, six.binary_type):
            raise TypeError("the topic must be type bytes")

        # Raise TypeError if the key is not encoded as bytes
        if key is not None and not isinstance(key, six.binary_type):
            raise TypeError("the key must be type bytes")

        if self.async:
            for m in msg:
                try:
                    item = (TopicAndPartition(topic, partition), m, key)
                    self.queue.put_nowait(item)
                except Full:
                    raise AsyncProducerQueueFull(
                        'Producer async queue overfilled. '
                        'Current queue size %d.' % self.queue.qsize())
            resp = []
        else:
            messages = create_message_set([(m, key) for m in msg], self.codec, key)
            req = ProduceRequest(topic, partition, messages)
            try:
                resp = self.client.send_produce_request([req], acks=self.req_acks,
                                                        timeout=self.ack_timeout)
            except Exception:
                log.exception("Unable to send messages")
                raise
        return resp

    def stop(self, timeout=1):
        """
        Stop the producer. Optionally wait for the specified timeout before
        forcefully cleaning up.
        """
        if self.async:
            self.queue.put((STOP_ASYNC_PRODUCER, None, None))
            self.thread.join(timeout)

            if self.thread.is_alive():
                self.thread_stop_event.set()

        if hasattr(self, '_cleanup_func'):
            # Remove cleanup handler now that we've stopped

            # py3 supports unregistering
            if hasattr(atexit, 'unregister'):
                atexit.unregister(self._cleanup_func) # pylint: disable=no-member

            # py2 requires removing from private attribute...
            else:

                # ValueError on list.remove() if the exithandler no longer exists
                # but that is fine here
                try:
                    atexit._exithandlers.remove((self._cleanup_func, (self,), {}))
                except ValueError:
                    pass

            del self._cleanup_func

        self.stopped = True

    def __del__(self):
        if not self.stopped:
            self.stop()
