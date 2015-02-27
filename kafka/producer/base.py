from __future__ import absolute_import

import logging
import time

try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue
from collections import defaultdict

from threading import Thread, Event

import six

from kafka.common import (
    ProduceRequest, TopicAndPartition,
    UnsupportedCodecError, FailedPayloadsError
)
from kafka.protocol import CODEC_NONE, ALL_CODECS, create_message_set

log = logging.getLogger("kafka")

BATCH_SEND_DEFAULT_INTERVAL = 20
BATCH_SEND_MSG_COUNT = 20
BATCH_RETRY_BACKOFF_MS = 300
BATCH_RETRIES_LIMIT = 0

STOP_ASYNC_PRODUCER = -1


def _send_upstream(queue, client, codec, batch_time, batch_size,
                   req_acks, ack_timeout, retry_backoff, retries_limit, stop_event):
    """
    Listen on the queue for a specified number of messages or till
    a specified timeout and send them upstream to the brokers in one
    request
    """
    stop = False
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
        except FailedPayloadsError as ex:
            failed_reqs = ex.args[0]
            log.exception("Failed payloads count %s" % len(failed_reqs))

            # if no limit, retry all failed messages until success
            if retries_limit is None:
                reqs_to_retry = failed_reqs
            # makes sense to check failed reqs only if we have a limit > 0
            elif retries_limit > 0:
                for req in failed_reqs:
                    if retries_limit and req.retries < retries_limit:
                        updated_req = req._replace(retries=req.retries+1)
                        reqs_to_retry.append(updated_req)
        except Exception as ex:
            log.exception("Unable to send message: %s" % type(ex))
        finally:
            reqs = []

        if reqs_to_retry and retry_backoff:
            reqs = reqs_to_retry
            log.warning("%s requests will be retried next call." % len(reqs))
            time.sleep(float(retry_backoff) / 1000)


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
                 batch_retry_backoff_ms=BATCH_RETRY_BACKOFF_MS,
                 batch_retries_limit=BATCH_RETRIES_LIMIT):

        if batch_send:
            async = True
            assert batch_send_every_n > 0
            assert batch_send_every_t > 0
        else:
            batch_send_every_n = 1
            batch_send_every_t = 3600

        self.client = client
        self.async = async
        self.req_acks = req_acks
        self.ack_timeout = ack_timeout

        if codec is None:
            codec = CODEC_NONE
        elif codec not in ALL_CODECS:
            raise UnsupportedCodecError("Codec 0x%02x unsupported" % codec)

        self.codec = codec

        if self.async:
            log.warning("async producer does not guarantee message delivery!")
            log.warning("Current implementation does not retry Failed messages")
            log.warning("Use at your own risk! (or help improve with a PR!)")
            self.queue = Queue()  # Messages are sent through this queue
            self.thread_stop_event = Event()
            self.thread = Thread(target=_send_upstream,
                                 args=(self.queue,
                                       self.client.copy(),
                                       self.codec,
                                       batch_send_every_t,
                                       batch_send_every_n,
                                       self.req_acks,
                                       self.ack_timeout,
                                       batch_retry_backoff_ms,
                                       batch_retries_limit,
                                       self.thread_stop_event))

            # Thread will die if main thread exits
            self.thread.daemon = True
            self.thread.start()

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
        return self._send_messages(topic, partition, *msg)

    def _send_messages(self, topic, partition, *msg, **kwargs):
        key = kwargs.pop('key', None)

        # Guarantee that msg is actually a list or tuple (should always be true)
        if not isinstance(msg, (list, tuple)):
            raise TypeError("msg is not a list or tuple!")

        # Raise TypeError if any message is not encoded as bytes
        if any(not isinstance(m, six.binary_type) for m in msg):
            raise TypeError("all produce message payloads must be type bytes")

        # Raise TypeError if the key is not encoded as bytes
        if key is not None and not isinstance(key, six.binary_type):
            raise TypeError("the key must be type bytes")

        if self.async:
            for m in msg:
                self.queue.put((TopicAndPartition(topic, partition), m, key))
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
