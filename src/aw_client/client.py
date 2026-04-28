import functools
import json
import logging
import os
import socket
import threading
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import persistqueue

from aw_core.dirs import get_data_dir
from aw_core.models import Event
from aw_transform.heartbeats import heartbeat_merge

from .config import load_config, load_local_server_api_key
from .singleinstance import SingleInstance

logger = logging.getLogger(__name__)


@dataclass
class _Response:
    status_code: int
    text: str

    def json(self):
        return json.loads(self.text or "null")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestError(self)


class RequestError(Exception):
    def __init__(self, response: Optional[_Response] = None, message: str = ""):
        self.response = response
        super().__init__(message or (f"HTTP {response.status_code}" if response else "request failed"))


class ConnectTimeout(RequestError):
    pass


def _log_request_exception(e: RequestError):
    logger.warning(str(e))
    try:
        d = e.response.json() if e.response else None
        logger.warning(f"Error message received: {d}")
    except Exception:
        pass


def _dt_is_tzaware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def always_raise_for_request_errors(f: Callable[..., _Response]):
    @functools.wraps(f)
    def g(*args, **kwargs):
        r = f(*args, **kwargs)
        try:
            r.raise_for_status()
        except RequestError as e:
            _log_request_exception(e)
            raise e
        return r

    return g


QueuedRequest = namedtuple("QueuedRequest", ["endpoint", "data"])
Bucket = namedtuple("Bucket", ["id", "type"])


class ActivityWatchClient:
    def __init__(
        self,
        client_name: str = "unknown",
        testing: bool = False,
        host=None,
        port=None,
        protocol="http",
    ) -> None:
        self.testing = testing
        self.client_name = client_name
        self.client_hostname = socket.gethostname()

        _config = load_config()
        server_config = _config["server" if not testing else "server-testing"]
        client_config = _config["client" if not testing else "client-testing"]

        server_host = host or server_config["hostname"]
        server_port = port or server_config["port"]
        self.server_api_key = load_local_server_api_key(str(server_host), server_port)
        self.server_address = f"{protocol}://{server_host}:{server_port}"

        self.instance = SingleInstance(
            f"{self.client_name}-at-{server_host}-on-{server_port}"
        )

        self.commit_interval = client_config["commit_interval"]
        self.request_queue = RequestQueue(self)
        self.last_heartbeat = {}

    def _url(self, endpoint: str):
        return f"{self.server_address}/api/0/{endpoint}"

    def _headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        request_headers = dict(headers or {})
        if self.server_api_key:
            request_headers.setdefault("Authorization", f"Bearer {self.server_api_key}")
        return request_headers

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Union[bytes, str]] = None,
        params: Optional[dict] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> _Response:
        url = self._url(endpoint)
        if params:
            query = urlencode(params)
            url = f"{url}?{query}"

        body: Optional[bytes]
        if data is None:
            body = None
        elif isinstance(data, bytes):
            body = data
        else:
            body = data.encode("utf-8")

        request = Request(url, data=body, method=method)
        for key, value in self._headers(headers).items():
            request.add_header(key, value)

        try:
            with urlopen(request, timeout=5) as response:
                text = response.read().decode("utf-8")
                return _Response(status_code=getattr(response, "status", 200), text=text)
        except HTTPError as e:
            text = e.read().decode("utf-8") if hasattr(e, "read") else ""
            return _Response(status_code=e.code, text=text)
        except URLError as e:
            raise ConnectTimeout(message=str(e)) from e

    @always_raise_for_request_errors
    def _get(self, endpoint: str, params: Optional[dict] = None) -> _Response:
        return self._request("GET", endpoint, params=params)

    @always_raise_for_request_errors
    def _post(
        self,
        endpoint: str,
        data: Union[List[Any], Dict[str, Any]],
        params: Optional[dict] = None,
    ) -> _Response:
        headers = self._headers({"Content-type": "application/json", "charset": "utf-8"})
        return self._request("POST", endpoint, data=json.dumps(data), params=params, headers=headers)

    @always_raise_for_request_errors
    def _delete(self, endpoint: str, data: Any = None) -> _Response:
        if data is None:
            data = {}
        headers = self._headers({"Content-type": "application/json"})
        return self._request("DELETE", endpoint, data=json.dumps(data), headers=headers)

    def get_info(self):
        return self._get("info").json()

    def get_setting(self, key: Optional[str] = None) -> dict:
        if key:
            return self._get(f"settings/{key}").json()
        else:
            return self._get("settings").json()

    def set_setting(self, key: str, value: str) -> None:
        self._post(f"settings/{key}", value)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def connect(self):
        if not self.request_queue.is_alive():
            self.request_queue.start()

    def disconnect(self):
        self.request_queue.stop()
        self.request_queue.join()
        self.request_queue = RequestQueue(self)

    def wait_for_start(self, timeout: int = 10) -> None:
        start_time = datetime.now()
        sleep_time = 0.1
        while (datetime.now() - start_time).seconds < timeout:
            try:
                self.get_info()
                break
            except RequestError:
                sleep(sleep_time)
                sleep_time *= 2
        else:
            raise Exception(f"Server at {self.server_address} did not start in time")

    def get_event(self, bucket_id: str, event_id: int) -> Optional[Event]:
        endpoint = f"buckets/{bucket_id}/events/{event_id}"
        try:
            event = self._get(endpoint).json()
            return Event(**event)
        except RequestError as e:
            if e.response and e.response.status_code == 404:
                return None
            else:
                raise

    def get_events(
        self,
        bucket_id: str,
        limit: int = -1,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Event]:
        endpoint = f"buckets/{bucket_id}/events"

        params = dict()
        if limit is not None:
            params["limit"] = str(limit)
        if start is not None:
            params["start"] = start.isoformat()
        if end is not None:
            params["end"] = end.isoformat()

        events = self._get(endpoint, params=params).json()
        return [Event(**event) for event in events]

    def insert_event(self, bucket_id: str, event: Event) -> None:
        endpoint = f"buckets/{bucket_id}/events"
        data = [event.to_json_dict()]
        self._post(endpoint, data)

    def insert_events(self, bucket_id: str, events: List[Event]) -> None:
        endpoint = f"buckets/{bucket_id}/events"
        data = [event.to_json_dict() for event in events]
        self._post(endpoint, data)

    def delete_event(self, bucket_id: str, event_id: int) -> None:
        endpoint = f"buckets/{bucket_id}/events/{event_id}"
        self._delete(endpoint)

    def get_eventcount(
        self,
        bucket_id: str,
        limit: int = -1,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> int:
        endpoint = f"buckets/{bucket_id}/events/count"

        params = dict()
        if start is not None:
            params["start"] = start.isoformat()
        if end is not None:
            params["end"] = end.isoformat()

        response = self._get(endpoint, params=params)
        return int(response.text)

    def heartbeat(
        self,
        bucket_id: str,
        event: Event,
        pulsetime: float,
        queued: bool = False,
        commit_interval: Optional[float] = None,
    ) -> None:
        endpoint = f"buckets/{bucket_id}/heartbeat?pulsetime={pulsetime}"
        _commit_interval = commit_interval or self.commit_interval

        if queued:
            if bucket_id not in self.last_heartbeat:
                self.last_heartbeat[bucket_id] = event
                return None

            last_heartbeat = self.last_heartbeat[bucket_id]
            merge = heartbeat_merge(last_heartbeat, event, pulsetime)

            if merge:
                diff = (last_heartbeat.duration).total_seconds()
                if diff >= _commit_interval:
                    data = merge.to_json_dict()
                    self.request_queue.add_request(endpoint, data)
                    self.last_heartbeat[bucket_id] = event
                else:
                    self.last_heartbeat[bucket_id] = merge
            else:
                data = last_heartbeat.to_json_dict()
                self.request_queue.add_request(endpoint, data)
                self.last_heartbeat[bucket_id] = event
        else:
            self._post(endpoint, event.to_json_dict())

    def get_buckets(self) -> dict:
        return self._get("buckets/").json()

    def create_bucket(self, bucket_id: str, event_type: str, queued=False):
        if queued:
            self.request_queue.register_bucket(bucket_id, event_type)
        else:
            endpoint = f"buckets/{bucket_id}"
            data = {
                "client": self.client_name,
                "hostname": self.client_hostname,
                "type": event_type,
            }
            self._post(endpoint, data)

    def delete_bucket(self, bucket_id: str, force: bool = False):
        self._delete(f"buckets/{bucket_id}" + ("?force=1" if force else ""))

    def setup_bucket(self, bucket_id: str, event_type: str):
        self.create_bucket(bucket_id, event_type, queued=True)

    def export_all(self) -> dict:
        return self._get("export").json()

    def export_bucket(self, bucket_id) -> dict:
        return self._get(f"buckets/{bucket_id}/export").json()

    def import_bucket(self, bucket: dict) -> None:
        endpoint = "import"
        self._post(endpoint, {"buckets": {bucket["id"]: bucket}})

    def query(
        self,
        query: str,
        timeperiods: List[Tuple[datetime, datetime]],
        name: Optional[str] = None,
        cache: bool = False,
    ) -> List[Any]:
        endpoint = "query/"
        params = {}
        if cache:
            if not name:
                raise Exception("You are not allowed to do caching without a query name")
            params["name"] = name
            params["cache"] = int(cache)

        for start, stop in timeperiods:
            try:
                assert _dt_is_tzaware(start)
                assert _dt_is_tzaware(stop)
            except AssertionError:
                raise ValueError("start/stop needs to have a timezone set") from None

        data = {
            "timeperiods": ["/".join([start.isoformat(), end.isoformat()]) for start, end in timeperiods],
            "query": query.split("\n"),
        }
        response = self._post(endpoint, data, params=params)
        return response.json()


class RequestQueue(threading.Thread):
    VERSION = 1

    def __init__(self, client: ActivityWatchClient) -> None:
        threading.Thread.__init__(self, daemon=True)

        self.client = client
        self.connected = False
        self._stop_event = threading.Event()
        self._registered_buckets = []
        self._attempt_reconnect_interval = 10

        data_dir = get_data_dir("aw-client")
        queued_dir = os.path.join(data_dir, "queued")
        if not os.path.exists(queued_dir):
            os.makedirs(queued_dir)

        persistqueue_path = os.path.join(
            queued_dir,
            "{}{}.v{}.persistqueue".format(
                self.client.client_name,
                "-testing" if client.testing else "",
                self.VERSION,
            ),
        )

        logger.debug(f"queue path '{persistqueue_path}'")

        self._persistqueue = persistqueue.FIFOSQLiteQueue(
            persistqueue_path, multithreading=True, auto_commit=False
        )
        self._current = None

    def _get_next(self) -> Optional[QueuedRequest]:
        if not self._current:
            try:
                self._current = self._persistqueue.get(block=False)
            except persistqueue.exceptions.Empty:
                return None
        return self._current

    def _task_done(self) -> None:
        self._current = None
        self._persistqueue.task_done()

    def _create_buckets(self) -> None:
        for bucket in self._registered_buckets:
            self.client.create_bucket(bucket.id, bucket.type)

    def _try_connect(self) -> bool:
        try:
            self._create_buckets()
            self.connected = True
            logger.info(f"Connection to aw-server established by {self.client.client_name}")
        except RequestError:
            self.connected = False
        return self.connected

    def wait(self, seconds) -> bool:
        return self._stop_event.wait(seconds)

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def _dispatch_request(self) -> None:
        request = self._get_next()
        if not request:
            self.wait(0.2)
            return

        try:
            self.client._post(request.endpoint, request.data)
        except ConnectTimeout:
            self.connected = False
            logger.warning(
                "Connection refused or timeout, will queue requests until connection is available."
            )
            sleep(0.5)
            return
        except RequestError as e:
            if e.response and e.response.status_code == 400:
                logger.error(f"Bad request, not retrying: {request.data}")
            elif e.response and e.response.status_code == 500:
                logger.error(f"Internal server error, retrying: {request.data}")
                sleep(0.5)
                return
            else:
                logger.exception(f"Unknown error, not retrying: {request.data}")
        except Exception:
            logger.exception(f"Unknown error, not retrying: {request.data}")

        self._task_done()

    def run(self) -> None:
        self._stop_event.clear()
        while not self.should_stop():
            while not self._try_connect():
                logger.warning(
                    f"Not connected to server, {self._persistqueue.qsize()} requests in queue"
                )
                if self.wait(self._attempt_reconnect_interval):
                    break

            while self.connected and not self.should_stop():
                self._dispatch_request()

    def stop(self) -> None:
        self._stop_event.set()

    def add_request(self, endpoint: str, data: dict) -> None:
        assert "/heartbeat" in endpoint
        assert isinstance(data, dict)
        self._persistqueue.put(QueuedRequest(endpoint, data))

    def register_bucket(self, bucket_id: str, event_type: str) -> None:
        self._registered_buckets.append(Bucket(bucket_id, event_type))
