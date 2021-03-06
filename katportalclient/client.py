###############################################################################
# SKA South Africa (http://ska.ac.za/)                                        #
# Author: cam@ska.ac.za                                                       #
# Copyright @ 2013 SKA SA. All rights reserved.                               #
#                                                                             #
# THIS SOFTWARE MAY NOT BE COPIED OR DISTRIBUTED IN ANY FORM WITHOUT THE      #
# WRITTEN PERMISSION OF SKA SA.                                               #
###############################################################################
"""
Websocket client and HTTP module for access to katportal webservers.
"""


import base64
import hashlib
import hmac
import logging
import uuid
import time
from urllib import urlencode
from datetime import timedelta
from collections import namedtuple

import tornado.gen
import tornado.ioloop
import tornado.httpclient
import tornado.locks
import omnijson as json
from tornado.websocket import websocket_connect
from tornado.httputil import url_concat, HTTPHeaders
from tornado.httpclient import HTTPRequest
from tornado.ioloop import PeriodicCallback

from request import JSONRPCRequest


# Limit for sensor history queries, in order to preserve memory on katportal.
MAX_SAMPLES_PER_HISTORY_QUERY = 1000000
# Pick a reasonable chunk size for sample downloads.  The samples are
# published in blocks, so many at a time.
# 43200 = 12 hour chunks if 1 sample every second
SAMPLE_HISTORY_CHUNK_SIZE = 43200
# Request sample times  in milliseconds for better precision
SAMPLE_HISTORY_REQUEST_TIME_TYPE = 'ms'
SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC = 1000.0

# Websocket connect and reconnect timeouts
WS_CONNECT_TIMEOUT = 10
WS_RECONNECT_INTERVAL = 15
WS_HEART_BEAT_INTERVAL = 20000  # in milliseconds

module_logger = logging.getLogger('kat.katportalclient')


def create_jwt_login_token(email, password):
    """Creates a JWT login token. See http://jwt.io for the industry standard
    specifications.

    Parameters
    ----------
    email: str
        The email address of the user to include in the token. This email
        address needs to exist in the kaportal user database to be able to
        authenticate.
    password: str
        The password for the user specified in the email address to include
        in the JWT login token.

    Returns
    -------
    jwt_auth_token: str
        The authentication token to include in the HTTP Authorization header
        when verifying a user's credentials on katportal.
    """
    jwt_header_alg = base64.standard_b64encode(u'{"alg":"HS256","typ":"JWT"}')
    jwt_header_email = base64.standard_b64encode(
        u'{"email":"%s"}' % email).strip('=')
    jwt_header = '.'.join([jwt_header_alg, jwt_header_email])

    password_sha = hashlib.sha256(password).hexdigest()
    dig = hmac.new(password_sha, msg=jwt_header,
                   digestmod=hashlib.sha256).digest()
    password_encrypted = base64.b64encode(dig).decode()
    jwt_auth_token = '.'.join([jwt_header, password_encrypted])

    return jwt_auth_token


class SensorSample(namedtuple('SensorSample', 'timestamp, value, status')):
    """Class to represent all sensor samples.

    Fields:
        - timestamp:  float
            The timestamp (UNIX epoch) the sample was received by CAM.
            timestamp value is reported with millisecond precision.
        - value:  str
            The value of the sensor when sampled.  The units depend on the
            sensor, see :meth:`.sensor_detail`.
        - status:  str
            The status of the sensor when the sample was taken. As defined
            by the KATCP protocol. Examples: 'nominal', 'warn', 'failure', 'error',
            'critical', 'unreachable', 'unknown', etc.
    """

    def csv(self):
        """Returns sample in comma separated values format."""
        return '{},{},{}'.format(self.timestamp, self.value, self.status)


class SensorSampleValueTs(namedtuple(
        'SensorSampleValueTs', 'timestamp, value_timestamp, value, status')):
    """Class to represent sensor samples, including the value_timestamp.

    Fields:
        - timestamp:  float
            The timestamp (UNIX epoch) the sample was received by CAM.
            Timestamp value is reported with millisecond precision.
        - value_timestamp:  float
            The timestamp (UNIX epoch) the sample was read at the lowest level sensor.
            value_timestamp value is reported with millisecond precision.
        - value:  str
            The value of the sensor when sampled.  The units depend on the
            sensor, see :meth:`.sensor_detail`.
        - status:  str
            The status of the sensor when the sample was taken. As defined
            by the KATCP protocol. Examples: 'nominal', 'warn', 'failure', 'error',
            'critical', 'unreachable', 'unknown', etc.
    """

    def csv(self):
        """Returns sample in comma separated values format."""
        return '{},{},{},{}'.format(
            self.timestamp, self.value_timestamp, self.value, self.status)


class KATPortalClient(object):
    """
    Client providing simple access to katportal.

    Wraps functions available on katportal webservers via the Pub/Sub capability,
    and HTTP requests.

    Parameters
    ----------
    url: str
        |  Client sitemap URL: ``http://<portal server>/api/client/<subarray #>``.
        |  E.g. for subarray 2:  ``http://1.2.3.4/api/client/2``
        |  (**Deprecated**:  use a websocket URL, e.g. ``ws://...``)
    on_update_callback: function
        Callback that should be invoked every time a Pub/Sub update message
        is received. Signature has to include a single argument for the
        message, e.g. `def on_update(message)`.
    io_loop: tornado.ioloop.IOLoop
        Optional IOLoop instance (default=None).
    logger: logging.Logger
        Optional logger instance (default=None).
    """

    def __init__(self, url, on_update_callback, io_loop=None, logger=None):
        self._logger = logger or module_logger
        self._url = url
        self._ws = None
        self._ws_connecting_lock = tornado.locks.Lock()
        self._io_loop = io_loop or tornado.ioloop.IOLoop.current()
        self._on_update = on_update_callback
        self._pending_requests = {}
        self._http_client = tornado.httpclient.AsyncHTTPClient()
        self._sitemap = None
        self._sensor_history_states = {}
        self._reference_observer_config = None
        self._disconnect_issued = False
        self._ws_jsonrpc_cache = []
        self._heart_beat_timer = PeriodicCallback(
            self._send_heart_beat, WS_HEART_BEAT_INTERVAL)
        self._current_user_id = None

    @tornado.gen.coroutine
    def logout(self):
        """ Logs user out of katportal. Katportal then deletes the cached
        session_id for this client. In order to call HTTP requests that
        requires authentication, the user will need to login again.
        """
        try:
            if self._session_id is not None:
                url = self.sitemap['authorization'] + '/user/logout'
                response = yield self.authorized_fetch(
                    url=url, auth_token=self._session_id, method='POST', body='{}')
                self._logger.info("Logout result: %s", response.body)
        finally:
            # Clear the local session_id, no matter what katportal says
            self._session_id = None
            self._current_user_id = None

    @tornado.gen.coroutine
    def login(self, username, password, role='read_only'):
        """
        Logs the specified user into katportal and caches the session_id
        created by katportal in this instance of KatportalClient.

        Parameters
        ----------
        username: str
            The registered username that exists on katportal. This is an
            email address, like abc@ska.ac.za.

        password: str
            The password for the specified username as saved in the katportal
            users database.

        """
        login_token = create_jwt_login_token(username, password)
        url = self.sitemap['authorization'] + '/user/verify/' + role
        response = yield self.authorized_fetch(url=url, auth_token=login_token)

        try:
            response_json = json.loads(response.body)
            if not response_json.get('logged_in', False) or response_json.get('session_id'):
                self._session_id = response_json.get('session_id')
                self._current_user_id = response_json.get('user_id')

                login_url = self.sitemap['authorization'] + '/user/login'
                response = yield self.authorized_fetch(
                    url=login_url, auth_token=self._session_id,
                    method='POST', body='')

                self._logger.info('Succesfully logged in as %s',
                                  response_json.get('email'))
            else:
                self._session_id = None
                self._current_user_id = None
                self._logger.error('Error in logging see response %s',
                                   response)
        except Exception:
            self._session_id = None
            self._current_user_id = None
            self._logger.exception('Error in response')

    @tornado.gen.coroutine
    def authorized_fetch(self, url, auth_token, **kwargs):
        """
        Wraps tornado.fetch to add the Authorization headers with
        the locally cached session_id.
        """
        login_header = HTTPHeaders({
            "Authorization": "CustomJWT {}".format(auth_token)})
        request = HTTPRequest(
            url, headers=login_header, **kwargs)
        response = yield self._http_client.fetch(request)
        raise tornado.gen.Return(response)

    def _get_sitemap(self, url):
        """
        Fetches the sitemap from the specified URL.

        See :meth:`.sitemap` for details, including the return value.

        Parameters
        ----------
        url: str
            URL to query for the sitemap, if it is an HTTP(S) address.  Otherwise
            it is assumed to be a websocket URL (this is for backwards
            compatibility).  In the latter case, the other endpoints will not be
            valid in the return value.

        Returns
        -------
        dict:
            Sitemap endpoints - see :meth:`.sitemap`.
        """
        result = {
            'authorization': '',
            'websocket': '',
            'historic_sensor_values': '',
            'schedule_blocks': '',
            'sub_nr': '',
            'subarray_sensor_values': '',
            'target_descriptions': ''
        }
        if (url.lower().startswith('http://') or
                url.lower().startswith('https://')):
            http_client = tornado.httpclient.HTTPClient()
            try:
                try:
                    response = http_client.fetch(url)
                    response = json.loads(response.body)
                    result.update(response['client'])
                except tornado.httpclient.HTTPError:
                    self._logger.exception("Failed to get sitemap!")
                except json.JSONError:
                    self._logger.exception("Failed to parse sitemap!")
                except KeyError:
                    self._logger.exception("Failed to parse sitemap!")
            finally:
                http_client.close()
        else:
            result['websocket'] = url
        return result

    @property
    def sitemap(self):
        """
        Returns the sitemap using the URL specified during instantiation.

        The portal webserver provides a sitemap with a number of URLs.  The
        endpoints could change over time, but the keys to access them will not.
        The websever is only queried once, the first time the property is
        accessed.  Typically users will not need to access the sitemap
        directly - the class's methods make use of it.

        Returns
        -------
        dict:
            Sitemap endpoints, will include at least the following::

                { 'websocket': str,
                  'historic_sensor_values': str,
                  'schedule_blocks': str,
                  'sub_nr': str,
                  ... }

                websocket: str
                    Websocket URL for Pub/Sub access.
                historic_sensor_values: str
                    URL for requesting sensor value history.
                schedule_blocks: str
                    URL for requesting observation schedule block information.
                sub_nr: str
                    Subarray number to access (e.g. '1', '2', '3', or '4').
                subarray_sensor_values: str
                    URL for requesting once off current sensor values.
                target_descriptions: str
                    URL for requesting target pointing descriptions for a
                    specified schedule block

        """
        if not self._sitemap:
            self._sitemap = self._get_sitemap(self._url)
            self._logger.debug("Sitemap: %s.", self._sitemap)
        return self._sitemap

    @property
    def is_connected(self):
        """Return True if websocket is connected."""
        return self._ws is not None

    @tornado.gen.coroutine
    def _connect(self, reconnecting=False):
        """
        Connect the websocket connection specified during instantiation.
        When the connection drops, katportalclient will periodically attempt
        to reconnect by calling this method until a disconnect() is called.

        Params
        ------
        reconnecting: bool
            Must be True if this method was called to reconnect a previously connected
            websocket connection. If this is True the websocket connection will attempt
            to resend the subscriptions and sampling strategies that was sent while the
            websocket connection was open. If the websocket connection cannot reconnect,
            it will try again periodically. If this is false and the websocket cannot be
            connected, no further attempts will be made to connect.
        """
        # The lock is used to ensure only a single connection can be made
        with (yield self._ws_connecting_lock.acquire()):
            self._disconnect_issued = False
            if not self.is_connected:
                self._logger.debug(
                    "Connecting to websocket %s", self.sitemap['websocket'])
                try:
                    if self._heart_beat_timer.is_running():
                        self._heart_beat_timer.stop()
                    self._ws = yield websocket_connect(
                        self.sitemap['websocket'],
                        on_message_callback=self._websocket_message,
                        connect_timeout=WS_CONNECT_TIMEOUT)
                    if reconnecting:
                        yield self._resend_subscriptions_and_strategies()
                        self._logger.info("Reconnected :)")
                    self._heart_beat_timer.start()
                except Exception:
                    self._logger.exception(
                        'Could not connect websocket to %s',
                        self.sitemap['websocket'])
                    if reconnecting:
                        self._logger.info(
                            'Retrying connection in %s seconds...', WS_RECONNECT_INTERVAL)
                        self._io_loop.call_later(
                            WS_RECONNECT_INTERVAL, self._connect, True)
                if not self.is_connected and not reconnecting:
                    self._logger.error("Failed to connect!")

    @tornado.gen.coroutine
    def connect(self):
        """Connect to the websocket server specified during instantiation."""
        yield self._connect(reconnecting=False)

    @tornado.gen.coroutine
    def _send_heart_beat(self):
        """
        Sends a PING message to katportal to test if the websocket connection is still
        alive. If there is an error sending this message, tornado will call the
        _websocket_message callback function with None as the message, where we realise
        that the websocket connection has failed.
        """
        if self._ws is not None:
            self._ws.write_message('PING')
        else:
            self._logger.debug('Attempting to send a PING over a closed websocket!')

    def disconnect(self):
        """Disconnect from the connected websocket server."""
        if self._heart_beat_timer.is_running():
            self._heart_beat_timer.stop()

        self._disconnect_issued = True
        self._ws_jsonrpc_cache = []
        self._logger.debug("Cleared JSONRPCRequests cache.")

        if self.is_connected:
            self._ws.close()
            self._ws = None
            self._logger.debug("Disconnected client websocket.")

    def _cache_jsonrpc_request(self, jsonrpc_request):
        """
        If the websocket is connected, cache all the the jsonrpc requests.
        When the websocket connection closes unexpectedly, we will attempt
        to reconnect. When the reconnection was successful, we will resend
        all of the jsonrpc applicable requests to ensure that we set the same
        subscriptions and sampling strategies that was set while the
        websocket was connected.

        .. note::

        When an unsubscribe or set_sampling_strategy and set_sampling_strategies
        with a none value is cached, we remove the matching converse call for
        that pattern. For example, if we cache a subscribe message for a
        namespace, then later cache an unsubscribe message for that same
        namespace, we will remove the subscribe message from the cache and not
        add the unsubscribe message to the cache. If we cache a
        set_sampling_strategy for a sensor, then later cache a call to
        set_sampling_strategy for the same sensor with none (clearing the
        strategy on the sensor), we remove the set_sampling_strategy from the
        cache and do not add the set_sampling_strategy to the cache that had
        a strategy of none. The same counts for set_sampling_strategies,
        except that we match on the sensor name pattern.
        We do not cache unsubscribe because creating a new websocket
        connection has no subscriptions on katportal. Also when we receive a
        'redis-reconnect' message, we do not have any subscriptions on
        katportal.

        JSONRPCRequests with identical methods and params will not be cached more than
        once.
        """
        requests_to_remove = []
        if jsonrpc_request.method == 'unsubscribe':
            for req in self._ws_jsonrpc_cache:
                # match namespace and subscription string for subscribes
                if (req.method == 'subscribe' and
                        req.params == jsonrpc_request.params):
                    requests_to_remove.append(req)
        elif (jsonrpc_request.method.startswith('set_sampling_strat') and
              jsonrpc_request.params[2] == 'none'):
            # index 2 of params is always the sampling strategy
            for req in self._ws_jsonrpc_cache:
                # match the namespace and sensor/filter combination
                # namespace is always at index 0 of params and sensor/filter
                # is always at index 1
                if (req.method == jsonrpc_request.method and
                        req.params[0] == jsonrpc_request.params[0] and
                        req.params[1] == jsonrpc_request.params[1]):
                    requests_to_remove.append(req)
        else:
            duplicate_found = False
            for req in self._ws_jsonrpc_cache:
                # check if there is a difference between the items in the dict of existing
                # JSONRPCRequests, if we find that we already have this JSONRPCRequest in
                # the cache, don't add it to the cache.
                duplicate_found = (req.method_and_params_hash() ==
                                   jsonrpc_request.method_and_params_hash())
                if duplicate_found:
                    break
            if not duplicate_found:
                self._ws_jsonrpc_cache.append(jsonrpc_request)

        for req in requests_to_remove:
            self._ws_jsonrpc_cache.remove(req)

    @tornado.gen.coroutine
    def _resend_subscriptions_and_strategies(self):
        """
        Resend the cached subscriptions and strategies that has been set while
        the websocket connection was connected. This cache is cleared when a
        disconnect is issued by the client. The cache is a list of
        JSONRPCRequests"""
        for req in self._ws_jsonrpc_cache:
            self._logger.info('Resending JSONRPCRequest %s', req)
            result = yield self._send(req)
            self._logger.info('Resent JSONRPCRequest, with result: %s', result)

    @tornado.gen.coroutine
    def _resend_subscriptions(self):
        """
        Resend the cached subscriptions only. This is necessary when we receive
        a redis-reconnect server message."""
        for req in self._ws_jsonrpc_cache:
            if req.method == 'subscribe':
                self._logger.info('Resending JSONRPCRequest %s', req)
                result = yield self._send(req)
                self._logger.info(
                    'Resent JSONRPCRequest, with result: %s', result)

    @tornado.gen.coroutine
    def _websocket_message(self, msg):
        """
        All websocket messages calls this method.
        If the message is None, the websocket connection was closed. When
        the websocket is closed by a disconnect that was not issued by the
        client, we need to reconnect, resubscribe and reset sampling
        strategies.

        There are different types of websocket messages that we receive:

            - json RPC message, the result for setting sampling strategies or
              subscribing/unsubscribing to namespaces
            - pub-sub message
            - redis-reconnect - when portal reconnects to redis. When this
              happens we need to resend our subscriptions
        """
        if msg is None:
            self._logger.warn("Websocket server disconnected!")
            if not self._disconnect_issued:
                if self._ws is not None:
                    self._ws.close()
                    self._ws = None
                yield self._connect(reconnecting=True)
            return
        try:
            msg = json.loads(msg)
            self._logger.debug("Message received: %s", msg)
            msg_id = str(msg['id'])
            if msg_id.startswith('redis-pubsub'):
                self._process_redis_message(msg, msg_id)
            elif msg_id.startswith('redis-reconnect'):
                # only resubscribe to namespaces, the server will still
                # publish sensor value updates to redis because the client
                # did not disconnect, katportal lost its own connection
                # to redis
                yield self._resend_subscriptions()
            else:
                self._process_json_rpc_message(msg, msg_id)
        except Exception:
            self._logger.exception(
                "Error processing websocket message! {}".format(msg))
            if self._on_update:
                self._io_loop.add_callback(self._on_update, msg)
            else:
                self._logger.warn('Ignoring message (no on_update_callback): %s',
                                  msg)

    @tornado.gen.coroutine
    def _process_redis_message(self, msg, msg_id):
        """Internal handler for Redis messages."""
        msg_result = msg['result']
        processed = False
        if msg_id == 'redis-pubsub-init':
            processed = True  # Nothing to do really.
        elif 'msg_channel' in msg_result:
            namespace = msg_result['msg_channel'].split(':', 1)[0]
            if namespace in self._sensor_history_states:
                state = self._sensor_history_states[namespace]
                msg_data = msg_result['msg_data']
                if (isinstance(msg_data, dict) and
                        'inform_type' in msg_data and
                        msg_data['inform_type'] == 'sample_history'):
                    # inform message which provides synchronisation
                    # information.
                    inform = msg_data['inform_data']
                    num_new_samples = inform['num_samples_to_be_published']
                    state['num_samples_pending'] += num_new_samples
                    if inform['done']:
                        state['done_event'].set()
                elif isinstance(msg_data, list):
                    num_received = 0
                    for sample in msg_data:
                        if len(sample) == 6:
                            # assume sample data message, extract fields of interest
                            # (timestamp returned in milliseconds, so scale to seconds)
                            # example:  [1476164224429L, 1476164223640L,
                            #            1476164224429354L, u'5.07571614843',
                            #            u'anc_mean_wind_speed', u'nominal']
                            if state['include_value_ts']:
                                # Requesting value_timestamp in addition to
                                # sample timestamp
                                sensor_sample = SensorSampleValueTs(
                                    timestamp=sample[
                                        0] / SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC,
                                    value_timestamp=sample[
                                        1] / SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC,
                                    value=sample[3],
                                    status=sample[5])
                            else:
                                # Only sample timestamp
                                sensor_sample = SensorSample(
                                    timestamp=sample[
                                        0] / SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC,
                                    value=sample[3],
                                    status=sample[5])
                            state['samples'].append(sensor_sample)
                            num_received += 1
                    state['num_samples_pending'] -= num_received
                else:
                    self._logger.warn(
                        'Ignoring unexpected message: %s', msg_result)
                processed = True
        if not processed:
            if self._on_update:
                self._io_loop.add_callback(self._on_update, msg_result)
            else:
                self._logger.warn('Ignoring message (no on_update_callback): %s',
                                  msg_result)

    @tornado.gen.coroutine
    def _process_json_rpc_message(self, msg, msg_id):
        """Internal handler for JSON RPC response messages."""
        future = self._pending_requests.get(msg_id, None)
        if future:
            error = msg.get('error', None)
            result = msg.get('result', None)
            if error:
                future.set_result(error)
            else:
                future.set_result(result)
        else:
            self._logger.error(
                "Message received without a matching pending request! '{}'".format(msg))

    def _send(self, req):
        future = tornado.gen.Future()
        if self.is_connected:
            req_id = str(req.id)
            self._pending_requests[req_id] = future
            self._ws.write_message(req())
            return future
        else:
            err_msg = "Failed to send request! Not connected."
            self._logger.error(err_msg)
            future.set_exception(Exception(err_msg))
            return future

    @tornado.gen.coroutine
    def add(self, x, y):
        """Simple method useful for testing."""
        req = JSONRPCRequest('add', [x, y])
        result = yield self._send(req)
        raise tornado.gen.Return(result)

    @tornado.gen.coroutine
    def subscribe(self, namespace, sub_strings=None):
        r"""Subscribe to the specified string identifiers in a namespace.

        A namespace provides grouping and consist of channels that can be
        subscribed to, e.g.

            namespace_1
                channel_A
                channel_B
            namespace_2
                channel_A
                channel_Z

        Messages are then published to namespace channels and delivered to all
        subscribers.

        This method supports both exact string identifiers and redis glob-style
        pattern string identifiers. Example of glob-style redis patterns:

        - h?llo subscribes to hello, hallo and hxllo
        - h*llo subscribes to hllo and heeeello
        - h[ae]llo subscribes to hello and hallo, but not hillo

        Use \ to escape special characters if you want to match them verbatim.

        Examples of subscriptions:
        --------------------------
            - Subscribe to 'data_1' channel in the 'alarms' namespace

                subscribe('alarms', 'data_1')

            - Subscribe to all channels in the 'alarms' namespace

                subscribe('alarms')

            - Subscribe to all 'componentA' channels in namespace 'elementX'

                subscribe('elementX', 'componentA*')

            - Subscribe to a list of subscription identifiers with mixed
              patterns

                subscribe('my_namespace', ['data_5', 'data_[abc]', 'data_1*'])


        Examples of KATCP sensor subscription strings:
        ----------------------------------------------
            Here the channels are the normalised KATCP sensor names
            (i.e. underscores Python identifiers).

            - Single sensor in the general namespace

                subscribe('', 'm063_ap_mode')

            - List of specific sensors in the 'antennas' namespace

                subscribe('antennas',
                          ['m063_ap_mode',
                           'm062_ap_mode',
                           'mon:m063_inhibited'])

            - List of sensor pattern strings in the 'antennas' namespace

                subscribe('antennas',
                          ['m063_ap_mode',
                           'm062_ap_actual*',
                           'm063_rsc_rx[lsxu]*'])

        Parameters
        ----------
        namespace: str
            Namespace to subscribe to. If an empty string '', the general
            namespace will be used automatically.
        sub_strings: str or list of str
            The exact and pattern string identifiers to subscribe to.
            Format = [namespace:]channel. Optional (default='*')

        Returns
        -------
        int
            Number of strings identifiers subscribed to.
        """
        req = JSONRPCRequest('subscribe', [namespace, sub_strings])
        result = yield self._send(req)
        self._cache_jsonrpc_request(req)
        raise tornado.gen.Return(result)

    @tornado.gen.coroutine
    def unsubscribe(self, namespace, unsub_strings=None):
        """Unsubscribe from the specified string identifiers in a namespace.

        Method supports both exact string identifiers and redis glob-style
        pattern string identifiers. For more information refer to the docstring
        of the `subscribe` method.

        .. note::

            Redis requires that the unsubscribe names and patterns must match
            the original subscribed names and patterns (including any
            namespaces).

        Parameters
        ----------
        namespace: str
            Namespace to unsubscribe. If an empty string '', the general
            namespace will be used automatically.
        unsub_strings: str or list of str
            The exact and pattern string identifiers to unsubscribe from.
            Optional (default='*').

        Returns
        -------
        int
            Number of strings identifiers unsubscribed from.
        """
        req = JSONRPCRequest('unsubscribe', [namespace, unsub_strings])
        result = yield self._send(req)
        self._cache_jsonrpc_request(req)
        raise tornado.gen.Return(result)

    @tornado.gen.coroutine
    def set_sampling_strategy(self, namespace, sensor_name,
                              strategy_and_params, persist_to_redis=False):
        """Set up a specified sensor strategy for a specific single sensor.

        Parameters
        ----------
        namespace: str
            Namespace with the relevant sensor subscriptions. If empty string
            '', the general namespace will be used.
        sensor_name: str
            The exact sensor name for which the sensor strategy should be set.
            Sensor name has to be the fully normalised sensor name (i.e. python
            identifier of sensor with all underscores) including the resource
            the sensor belongs to e.g. 'm063_ap_connected'
        strategy_and_params: str
            A string with the strategy and its optional parameters specified in
            space-separated form according the KATCP specification e.g.
            '<strat_name> <strat_parm1> <strat_parm2>'
            Examples:
                'event'
                'period 0.5'
                'event-rate 1.0 5.0'
        persist_to_redis: bool
            Whether to persist the sensor updates to redis or not, if persisted
            to redis, the last updated values can be  retrieved from redis
            without having to wait for the next KATCP sensor update.
            (default=False)

        Returns
        -------
        dict
            Dictionary with sensor name as key and result as value
        """
        req = JSONRPCRequest(
            'set_sampling_strategy',
            [namespace, sensor_name, strategy_and_params, persist_to_redis]
        )
        result = yield self._send(req)
        self._cache_jsonrpc_request(req)
        raise tornado.gen.Return(result)

    @tornado.gen.coroutine
    def set_sampling_strategies(self, namespace, filters,
                                strategy_and_params, persist_to_redis=False):
        """
        Set up a specified sensor strategy for a filtered list of sensors.

        Parameters
        ----------
        namespace: str
            Namespace with the relevant sensor subscriptions. If empty string
            '', the general namespace will be used.
        filters: str or list of str
            The regular expression filters to use to select the sensors to
            which to apply the specified strategy. Use "" to match all
            sensors. Is matched using KATCP method `list_sensors`.  Can be a
            single string or a list of strings.
            For example:
                1 filter  = 'm063_rsc_rxl'
                3 filters = ['m063_sensors_ok', 'ap_connected', 'sensors_ok']
        strategy_and_params : str
            A string with the strategy and its optional parameters specified in
            space-separated form according the KATCP specification e.g.
            '<strat_name> <strat_parm1> <strat_parm2>'
            Examples:
                'event'
                'period 0.5'
                'event-rate 1.0 5.0'
        persist_to_redis: bool
            Whether to persist the sensor updates to redis or not, if persisted
            to redis, the last updated values can be  retrieved from redis
            without having to wait for the next KATCP sensor update.
            (default=False)

        Returns
        -------
        dict
            Dictionary with matching sensor names as keys and the
            :meth:`.set_sampling_strategy` result as value::

                { <matching_sensor1_name>:
                    { success: bool,
                    info: string },
                ...
                <matching_sensorN_name>:
                    { success: bool,
                    info: string },
                }

                success: bool
                    True if setting succeeded for this sensor, else False.
                info: string
                    Normalised sensor strategy and parameters as string if
                    success == True else, string with the error that occured.
        """
        req = JSONRPCRequest(
            'set_sampling_strategies',
            [namespace, filters, strategy_and_params, persist_to_redis]
        )
        result = yield self._send(req)
        self._cache_jsonrpc_request(req)
        raise tornado.gen.Return(result)

    def _extract_schedule_blocks(self, json_text, subarray_number):
        """Extract and return list of schedule block IDs from a JSON response."""
        data = json.loads(json_text)
        results = []
        if data['result']:
            schedule_blocks = json.loads(data['result'])
            for schedule_block in schedule_blocks:
                if (schedule_block['sub_nr'] == subarray_number and
                        schedule_block['type'] == 'OBSERVATION'):
                    results.append(schedule_block['id_code'])
        return results

    @tornado.gen.coroutine
    def schedule_blocks_assigned(self):
        """Return list of assigned observation schedule blocks.

        The schedule blocks have already been verified and assigned to
        a single subarray.  The subarray queried is determined by
        the URL used during instantiation.  For detail about
        a schedule block, use :meth:`.schedule_block_detail`.

        Alternatively, subscribe to a sensor like ``sched_observation_schedule_3``
        for updates on the list assigned to subarray number 3 - see :meth:`.subscribe`.

        .. note::

            The websocket is not used for this request - it does not need
            to be connected.

        Returns
        -------
        list:
            List of scheduled block ID strings.  Ordered according to
            priority of the schedule blocks (first has hightest priority).

        """
        url = self.sitemap['schedule_blocks'] + '/scheduled'
        response = yield self._http_client.fetch(url)
        results = self._extract_schedule_blocks(response.body,
                                                int(self.sitemap['sub_nr']))
        raise tornado.gen.Return(results)

    @tornado.gen.coroutine
    def future_targets(self, id_code):
        """
        Return a list of future targets as determined by the dry run of the
        schedule block.

        The schedule block will only have future targets (in the targets
        attribute) if the schedule block has been through a dry run and
        has the verification_state of VERIFIED. The future targets are
        only applicable to schedule blocks of the OBSERVATION type.

        Parameters
        ----------
        id_code: str
            Schedule block identifier. For example: ``20160908-0010``.

        Returns
        -------
        list:
            Ordered list of future targets that was determined by the
            verification dry run.
            Example:
            [
                {
                    "track_start_offset":39.8941187859,
                    "target":"PKS 0023-26 | J0025-2602 | OB-238, radec, "
                             "0:25:49.16, -26:02:12.6, "
                             "(1410.0 8400.0 -1.694 2.107 -0.4043)",
                    "track_duration":20.0
                },
                {
                    "track_start_offset":72.5947952271,
                    "target":"PKS 0043-42 | J0046-4207, radec, "
                             "0:46:17.75, -42:07:51.5, "
                             "(400.0 2000.0 3.12 -0.7)",
                    "track_duration":20.0
                },
                {
                    "track_start_offset":114.597304821,
                    "target":"PKS 0408-65 | J0408-6545, radec, "
                             "4:08:20.38, -65:45:09.1, "
                             "(1410.0 8400.0 -3.708 3.807 -0.7202)",
                    "track_duration":20.0
                }
            ]
        Raises
        ------
        ScheduleBlockTargetsParsingError:
            If there is an error parsing the schedule block's targets string.
        ScheduleBlockNotFoundError:
            If no information was available for the requested schedule block.
        """
        sb = yield self.schedule_block_detail(id_code)
        targets_list = []
        sb_targets = sb.get('targets')
        if sb_targets is not None:
            try:
                targets_list = json.loads(sb_targets)
            except Exception:
                raise ScheduleBlockTargetsParsingError(
                    'There was an error parsing the schedule block (%s) '
                    'targets attribute: %s', id_code, sb_targets)
        raise tornado.gen.Return(targets_list)

    @tornado.gen.coroutine
    def schedule_block_detail(self, id_code):
        """Return detailed information about an observation schedule block.

        For a list of schedule block IDs, see :meth:`.schedule_blocks_assigned`.

        .. note::

            The websocket is not used for this request - it does not need
            to be connected.

        Parameters
        ----------
        id_code: str
            Schedule block identifier.  For example: ``20160908-0010``.

        Returns
        -------
        dict:
            Detailed information about the schedule block.  Some of the
            more useful fields are indicated::

                { 'description': str,
                  'scheduled_time': str,
                  'desired_start_time': str,
                  'actual_start_time': str,
                  'actual_end_time': str,
                  'expected_duration_seconds': int,
                  'state': str,
                  'sub_nr': int,
                  ... }

                description: str
                    Free text description of the observation.
                scheduled_time: str
                     Time (UTC) at which the Schedule Block went SCHEDULED.
                desired_start_time: str
                     Time (UTC) at which user would like the Schedule Block to start.
                actual_start_time: str
                     Time (UTC) at which the Schedule Block went ACTIVE.
                actual_end_time: str
                     Time (UTC) at which the Schedule Block went to COMPLETED
                     or INTERRUPTED.
                expected_duration_seconds: int
                     Length of time (seconds) the observation is expected to take
                     in total.
                state: str
                    'DRAFT': created, in process of being defined, but not yet
                             ready for scheduling.
                    'SCHEDULED': observation is scheduled for later execution, once
                                 resources (receptors, correlator, etc.) become available.
                    'ACTIVE':  observation is currently being executed.
                    'COMPLETED': observation completed naturally (may have been
                                 successful, or failed).
                    'INTERRUPTED': observation was stopped or cancelled by a user or
                                   the system.
                sub_nr: int
                    The number of the subarray the observation is scheduled on.

        Raises
        -------
        ScheduleBlockNotFoundError:
            If no information was available for the requested schedule block.
        """
        url = self.sitemap['schedule_blocks'] + '/' + id_code
        response = yield self._http_client.fetch(url)
        response = json.loads(response.body)
        schedule_block = response['result']
        if not schedule_block:
            raise ScheduleBlockNotFoundError(
                "Invalid schedule block ID: " + id_code)
        raise tornado.gen.Return(schedule_block)

    def _extract_sensors_details(self, json_text):
        """Extract and return list of sensor names from a JSON response."""
        sensors = json.loads(json_text)
        results = []
        # Errors are returned in dict, while valid data is returned in a list.
        if isinstance(sensors, dict):
            if 'error' in sensors:
                raise SensorNotFoundError(
                    "Invalid sensor request: " + sensors['error'])
        else:
            for sensor in sensors:
                sensor_info = {}
                sensor_info['name'] = sensor[0]
                sensor_info['component'] = sensor[1]
                sensor_info.update(sensor[2])
                results.append(sensor_info)
        return results

    @tornado.gen.coroutine
    def sensor_names(self, filters):
        """Return list of matching sensor names.

        Provides the list of available sensors in the system that match the
        specified pattern.  For detail about a sensor's attributes,
        use :meth:`.sensor_detail`.

        .. note::

            The websocket is not used for this request - it does not need
            to be connected.

        Parameters
        ----------
        filters: str or list of str
            List of regular expression patterns to match.
            See :meth:`.set_sampling_strategies` for more detail.

        Returns
        -------
        list:
            List of sensor name strings.

        Raises
        -------
        SensorNotFoundError:
            - If any of the filters were invalid regular expression patterns.
        """
        url = self.sitemap['historic_sensor_values'] + '/sensors'
        if isinstance(filters, str):
            filters = [filters]
        results = set()
        for filt in filters:
            response = yield self._http_client.fetch("{}?sensors={}".format(url, filt))
            new_sensors = self._extract_sensors_details(response.body)
            # only add sensors once, to ensure a unique list
            for sensor in new_sensors:
                results.add(sensor['name'])
        raise tornado.gen.Return(list(results))

    @tornado.gen.coroutine
    def sensor_detail(self, sensor_name):
        """Return detailed attribute information for a sensor.

        For a list of sensor names, see :meth:`.sensors_list`.

        .. note::

            The websocket is not used for this request - it does not need
            to be connected.

        Parameters
        ----------
        sensor_name: str
            Exact sensor name - see description in :meth:`.set_sampling_strategy`.

        Returns
        -------
        dict:
            Detailed attribute information for the sensor.  Some of the
            more useful fields are indicated::

                { 'name': str,
                  'description': str,
                  'params': str,
                  'units': str,
                  'type': str,
                  'resource': str,
                  'katcp_name': str,
                  ... }

                name: str
                    Normalised sensor name, as requested in input parameters.
                description: str
                    Free text description of the sensor.
                params: str
                     Limits or possible states for the sensor value.
                units: str
                     Measurement units for sensor value, e.g. 'm/s'.
                type: str
                     Sensor type, e.g. 'float', 'discrete', 'boolean'
                resource: str
                     Name of resource that provides the sensor.
                katcp_name: str
                     Internal KATCP messaging name.

        Raises
        -------
        SensorNotFoundError:
            - If no information was available for the requested sensor name.
            - If the sensor name was not a unique match for a single sensor.
        """
        url = self.sitemap['historic_sensor_values'] + '/sensors'
        response = yield self._http_client.fetch("{}?sensors={}".format(url, sensor_name))
        results = self._extract_sensors_details(response.body)
        if len(results) == 0:
            raise SensorNotFoundError("Sensor name not found: " + sensor_name)
        elif len(results) > 1:
            # check for exact match, before giving up
            for result in results:
                if result['name'] == sensor_name:
                    raise tornado.gen.Return(result)
            raise SensorNotFoundError(
                "Multiple sensors ({}) found - specify a single sensor "
                "name not a pattern like: '{}'.  (Some matches: {})."
                .format(len(results),
                        sensor_name,
                        [result['name'] for result in results[0:5]]))
        else:
            raise tornado.gen.Return(results[0])

    @tornado.gen.coroutine
    def sensor_history(self, sensor_name, start_time_sec, end_time_sec,
                       include_value_ts=False, timeout_sec=300):
        """Return time history of sample measurements for a sensor.

        For a list of sensor names, see :meth:`.sensors_list`.

        Parameters
        ----------
        sensor_name: str
            Exact sensor name - see description in :meth:`.set_sampling_strategy`.
        start_time_sec: float
            Start time for sample history query, in seconds since the UNIX epoch
            (1970-01-01 UTC).
        end_time_sec: float
            End time for sample history query, in seconds since the UNIX epoch.
        include_value_ts: bool
            Flag to also include value timestamp in addition to time series
            sample timestamp in the result.
            Default: False.
        timeout_sec: float
            Maximum time (in sec) to wait for the history to be retrieved.
            An exception will be raised if the request times out. (default:300)

        Returns
        -------
        list:
            List of :class:`.SensorSample` namedtuples (one per sample, with fields
            timestamp, value and status) or, if include_value_ts was set, then
            list of :class:`.SensorSampleValueTs` namedtuples (one per sample, with fields
            timestamp, value_timestamp, value and status).
            See :class:`.SensorSample` and :class:`.SensorSampleValueTs` for details.
            If the sensor named never existed, or is otherwise invalid, the
            list will be empty - no exception is raised.

        Raises
        -------
        SensorHistoryRequestError:
            - If there was an error submitting the request.
            - If the request timed out
        """
        # create new namespace and state variables per query, to allow multiple
        # request simultaneously
        state = {
            'sensor': sensor_name,
            'done_event': tornado.locks.Event(),
            'num_samples_pending': 0,
            'include_value_ts': include_value_ts,
            'samples': []
        }
        namespace = str(uuid.uuid4())
        self._sensor_history_states[namespace] = state
        # ensure connected, and subscribed before sending request
        yield self.connect()
        yield self.subscribe(namespace, ['*'])

        params = {
            'sensor': sensor_name,
            'time_type': SAMPLE_HISTORY_REQUEST_TIME_TYPE,
            'start': start_time_sec * SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC,
            'end': end_time_sec * SAMPLE_HISTORY_REQUEST_MULTIPLIER_TO_SEC,
            'namespace': namespace,
            'request_in_chunks': 1,
            'chunk_size': SAMPLE_HISTORY_CHUNK_SIZE,
            'limit': MAX_SAMPLES_PER_HISTORY_QUERY
        }
        url = url_concat(
            self.sitemap['historic_sensor_values'] + '/samples', params)
        self._logger.debug("Sensor history request: %s", url)
        response = yield self._http_client.fetch(url)
        data = json.loads(response.body)
        if isinstance(data, dict) and data['result'] == 'success':
            download_start_sec = time.time()
            # Query accepted by portal - data will be returned via websocket, but
            # we need to wait until it has arrived.  For synchronisation, we wait
            # for a 'done_event'. This event is updated in
            # _process_redis_message().
            try:
                timeout_delta = timedelta(seconds=timeout_sec)
                yield state['done_event'].wait(timeout=timeout_delta)

                self._logger.debug('Done in %d seconds, fetched %s samples.' % (
                    time.time() - download_start_sec,
                    len(state['samples'])))
            except tornado.gen.TimeoutError:
                raise SensorHistoryRequestError(
                    "Sensor history request timed out")

        else:
            raise SensorHistoryRequestError("Error requesting sensor history: {}"
                                            .format(response.body))

        def sort_by_timestamp(sample):
            return sample.timestamp
        # return a sorted copy, as data may have arrived out of order
        result = sorted(state['samples'], key=sort_by_timestamp)

        if len(result) >= MAX_SAMPLES_PER_HISTORY_QUERY:
            self._logger.warn(
                'Maximum sample limit (%d) hit - there may be more data available.',
                MAX_SAMPLES_PER_HISTORY_QUERY)

        # Free the state variables that were only required for the duration of
        # the download.  Do not disconnect - there may be websocket activity
        # initiated by another call.
        yield self.unsubscribe(namespace, ['*'])
        del self._sensor_history_states[namespace]

        raise tornado.gen.Return(result)

    @tornado.gen.coroutine
    def sensors_histories(self, filters, start_time_sec, end_time_sec,
                          include_value_ts=False, timeout_sec=300):
        """Return time histories of sample measurements for multiple sensors.

        Finds the list of available sensors in the system that match the
        specified pattern, and then requests the sample history for each one.

        If only a single sensor's data is required, use :meth:`.sensor_history`.

        Parameters
        ----------
        filters: str or list of str
            List of regular expression patterns to match.
            See :meth:`.set_sampling_strategies` for more detail.
        start_time_sec: float
            Start time for sample history query, in seconds since the UNIX epoch
            (1970-01-01 UTC).
        end_time_sec: float
            End time for sample history query, in seconds since the UNIX epoch.
        include_value_ts: bool
            Flag to also include value timestamp in addition to time series
            sample timestamp in the result.
            Default: False.
        timeout_sec: float
            Maximum time to wait for all sensors' histories to be retrieved.
            An exception will be raised if the request times out.

        Returns
        -------
        dict:
            Dictionary of lists.  The keys are the full sensor names.
            The values are lists of :class:`.SensorSample` namedtuples,
            (one per sample, with fields timestamp, value and status)
            or, if include_value_ts was set, then
            list of :class:`.SensorSampleValueTs` namedtuples (one per sample,
            with fields timestamp, value_timestamp, value and status).
            See :class:`.SensorSample` and :class:`.SensorSampleValueTs` for details.

        Raises
        -------
        SensorHistoryRequestError:
            - If there was an error submitting the request.
            - If the request timed out
        SensorNotFoundError:
            - If any of the filters were invalid regular expression patterns.
        """
        request_start_sec = time.time()
        sensors = yield self.sensor_names(filters)
        histories = {}
        for sensor in sensors:
            elapsed_time_sec = time.time() - request_start_sec
            timeout_left_sec = timeout_sec - elapsed_time_sec
            histories[sensor] = yield self.sensor_history(
                sensor, start_time_sec, end_time_sec, timeout_left_sec)
        raise tornado.gen.Return(histories)

    @tornado.gen.coroutine
    def userlog_tags(self):
        """Return all userlog tags in the database.

        Returns
        -------
        list:
            List of userlog tags in the database. Example:

            [{
                'activated': True,
                'slug': '',
                'name': 'm047',
                'id': 1
            },
            {
                'activated': True,
                'slug': '',
                'name': 'm046',
                'id': 2
            },
            {
                'activated': True,
                'slug': '',
                'name': 'm045',
                'id': 3},
            {..}]

        """
        url = self.sitemap['userlogs'] + '/tags'
        response = yield self._http_client.fetch(url)
        raise tornado.gen.Return(json.loads(response.body))

    @tornado.gen.coroutine
    def userlogs(self, start_time=None, end_time=None):
        """
        Return a list of userlogs in the database that has an start_time
        and end_time combination that intersects with the given start_time
        and end_time. For example of an userlog has a start_time before the
        given start_time and an end time after the given end_time, the time
        window of that userlog intersects with the time window of the given
        start_time and end_time.

        If an userlog has no end_time, an end_time of infinity is assumed.
        For example, if the given end_time is after the userlog's start time,
        there is an intersection of the two time windows.

        Here are some visual representations of the time window intersections:

                                Start       End
        Userlog:                  [----------]
        Search params:                 [-----------------]
                                      Start              End

                                              Start       End
        Userlog:                                [----------]
        Search params:              [-----------------]
                                  Start              End

                                 Start                End
        Userlog:                  [--------------------]
        Search params:                 [---------]
                                     Start      End

                                 Start     End
        Userlog:                  [---------]
        Search params:     [-------------------------]
                         Start                      End

                                              Start
        Userlog:                                [-------------------*
        Search params:              [-----------------]
                                  Start              End

                                                    End
        Userlog:             *-----------------------]
        Search params:                      [-----------------]
                                          Start              End

        Userlog:            *--------------------------------------*
        Search params:              [-----------------]
                                  Start              End
        Parameters
        ----------
        start_time: str
            A formatted UTC datetime string used as the start of the time window
            to query. Format: %Y-%m-%d %H:%M:%S.
            Default: Today at %Y-%m-%d 00:00:00 (The day of year is selected from local
                     time but the time portion is in UTC. Example if you are at SAST, and
                     you call this method at 2017-01-01 01:00:00 AM SAST, the date portion
                     of start_time will be selected from local time: 2017-01-01.
                     The start_time is, however, saved as UTC, so this default will be
                     2017-01-01 00:00:00 AM UTC and NOT 2016-12-31 00:00:00 AM UTC)
        end_time: str
            A formatted UTC datetime string used as the end of the time window
            to query. Format: %Y-%m-%d %H:%M:%S.
            Default: Today at %Y-%m-%d 23:59:59 (The day of year is selected from local
                     time but the time portion is in UTC. Example if you are at SAST, and
                     you call this method at 2017-01-01 01:00:00 AM SAST, the date portion
                     of end_time will be selected from local time: 2017-01-01.
                     The end_time is, however, saved as UTC, so this default will be
                     2017-01-01 23:59:59 UTC and NOT 2016-12-31 23:59:59 UTC)

        Returns
        -------
        list:
            List of userlog that intersects with the give start_time and
            end_time. Example:

            [{
                'other_metadata': [],
                'user_id': 1,
                'attachments': [],
                'tags': '[]',
                'timestamp': '2017-02-07 08:47:22',
                'start_time': '2017-02-07 00:00:00',
                'modified': '',
                'content': 'katportalclient userlog creation content!',
                'parent_id': '',
                'user': {'email': 'cam@ska.ac.za', 'id': 1, 'name': 'CAM'},
                'attachment_count': 0,
                'id': 40,
                'end_time': '2017-02-07 23:59:59'
             }, {..}]
        """
        url = self.sitemap['userlogs'] + '/query?'
        if start_time is None:
            start_time = time.strftime('%Y-%m-%d 00:00:00')
        if end_time is None:
            end_time = time.strftime('%Y-%m-%d 23:59:59')
        request_params = {
            'start_time': start_time,
            'end_time': end_time
        }
        query_string = urlencode(request_params)
        response = yield self.authorized_fetch(
            url='{}{}'.format(url, query_string), auth_token=self._session_id)
        raise tornado.gen.Return(json.loads(response.body))

    @tornado.gen.coroutine
    def create_userlog(self, content, tag_ids=None, start_time=None,
                       end_time=None):
        """
        Create a userlog with specified linked tags and content, start_time
        and end_time.

        Parameters
        ----------
        content: str
            The content of the userlog, could be any text. Required.

        tag_ids: list
            A list of tag id's to link to this userlog.
            Example: [1, 2, 3, ..]
            Default: None

        start_time: str
            A formatted datetime string used as the start time of the userlog in UTC.
            Format: %Y-%m-%d %H:%M:%S.
            Default: None

        end_time: str
            A formatted datetime string used as the end time of the userlog in UTC.
            Format: %Y-%m-%d %H:%M:%S.
            Default: None

        Returns
        -------
        userlog: dict
            The userlog that was created. Example:
            {
                'other_metadata': [],
                'user_id': 1,
                'attachments': [],
                'tags': '[]',
                'timestamp': '2017-02-07 08:47:22',
                'start_time': '2017-02-07 00:00:00',
                'modified': '',
                'content': 'katportalclient userlog creation content!',
                'parent_id': '',
                'user': {'email': 'cam@ska.ac.za', 'id': 1, 'name': 'CAM'},
                'attachment_count': 0,
                'id': 40,
                'end_time': '2017-02-07 23:59:59'
             }
        """
        url = self.sitemap['userlogs']
        new_userlog = {
            'user': self._current_user_id,
            'content': content
        }
        if start_time is not None:
            new_userlog['start_time'] = start_time
        if end_time is not None:
            new_userlog['end_time'] = end_time
        if tag_ids is not None:
            new_userlog['tag_ids'] = tag_ids

        response = yield self.authorized_fetch(
            url=url, auth_token=self._session_id,
            method='POST', body=json.dumps(new_userlog))
        raise tornado.gen.Return(json.loads(response.body))

    @tornado.gen.coroutine
    def modify_userlog(self, userlog, tag_ids=None):
        """
        Modify an existing userlog using the dictionary provided as the
        modified attributes of the userlog.

        Parameters
        ----------
        userlog: dict
            The userlog with the new values to be modified.

        tag_ids: list
            A list of tag id's to link to this userlog. Optional, if this is
            not specified, the tags attribute of the given userlog will be
            used.
            Example: [1, 2, 3, ..]

        Returns
        -------
        userlog: dict
            The userlog that was modified. Example:
            {
                'other_metadata': [],
                'user_id': 1,
                'attachments': [],
                'tags': '[]',
                'timestamp': '2017-02-07 08:47:22',
                'start_time': '2017-02-07 00:00:00',
                'modified': '',
                'content': 'katportalclient userlog modified content!',
                'parent_id': '',
                'user': {'email': 'cam@ska.ac.za', 'id': 1, 'name': 'CAM'},
                'attachment_count': 0,
                'id': 40,
                'end_time': '2017-02-07 23:59:59'
             }
        """
        if tag_ids is None and 'tags' in userlog:
            try:
                userlog['tag_ids'] = [
                    tag_id for tag_id in json.loads(userlog['tags'])]
            except Exception:
                self._logger.exception(
                    'Could not parse the tags field of the userlog: %s', userlog)
                raise
        else:
            userlog['tag_ids'] = tag_ids
        url = '{}/{}'.format(self.sitemap['userlogs'], userlog['id'])
        response = yield self.authorized_fetch(
            url=url, auth_token=self._session_id,
            method='POST', body=json.dumps(userlog))
        raise tornado.gen.Return(json.loads(response.body))

    @tornado.gen.coroutine
    def sensor_subarray_lookup(self, component, sensor, return_katcp_name=False,
                               sub_nr=None):
        """Return the full sensor name based on a generic component and sensor
        name, for the given subarray.

        This method gets the full sensor name based on a generic component and
        sensor name, for a given subarray. This method will return a failed
        katcp response if the given subarray is not in the 'active' or
        'initialising' state.


        .. note::

            The websocket is not used for this request - it does not need
            to be connected.

        Parameters
        ----------

        component: str
            The component that has the sensor to look up.

        sensor: str
            The generic sensor to look up.

        katcp_name: bool (optional)
            True to return the katcp name, False to return the fully qualified
            Python sensor name. Default is False.

        sub_nr: int
            The sub_nr on which to do the sensor lookup. The given component
            must be assigned to this subarray for a successful lookup.

        Returns
        -------
        str:
            The full sensor name based on the given component and subarray.

        """
        if sub_nr == None:
            sub_nr = int(self.sitemap['sub_nr'])
        if not sub_nr:
            raise SubarrayNumberUnknown()
        url = "{base_url}/{sub_nr}/{component}/{sensor}/{katcp_name}"
        response = yield self._http_client.fetch(url.format(
            base_url=self.sitemap['sensor_lookup'],
            sub_nr=sub_nr, component=component, sensor=sensor,
            return_katcp_name=1 if return_katcp_name else 0))
        # 1 or 0 because katportal expects that instead of a boolean value
        raise tornado.gen.Return(response.body)


class ScheduleBlockNotFoundError(Exception):
    """Raise if requested schedule block is not found."""


class SensorNotFoundError(Exception):
    """Raise if requested sensor is not found."""


class SensorHistoryRequestError(Exception):
    """Raise if error requesting sensor sample history."""


class ScheduleBlockTargetsParsingError(Exception):
    """Raise if there was an error parsing the targets attribute of the
    ScheduleBlock"""

class SubarrayNumberUnknown(Exception):
    """Raised when subarray number is unknown"""

    def __init__(self, method_name):
        _message = ("Unknown subarray number when calling method {}"
                    .format(method_name))
        super(SelectBandError, self).__init__(_message)

