import asyncio
import logging
import traceback
from sys import exc_info
from contextlib import suppress

from ..core.helpers.misc import get_size
from ..core.helpers.async_helpers import TaskCounter
from ..core.errors import ValidationError, WordlistError


class BaseModule:
    # Event types to watch
    watched_events = []
    # Event types to produce
    produced_events = []
    # Module description, etc.
    meta = {"auth_required": False, "description": "Base module"}
    # Flags, must include either "passive" or "active"
    flags = []

    # python dependencies (pip install ____)
    deps_pip = []
    # apt dependencies (apt install ____)
    deps_apt = []
    # other dependences as shell commands
    # uses ansible.builtin.shell (https://docs.ansible.com/ansible/latest/collections/ansible/builtin/shell_module.html)
    deps_shell = []
    # list of ansible tasks for when other dependency installation methods aren't enough
    deps_ansible = []
    # Whether to accept incoming duplicate events
    accept_dupes = False
    # Whether to block outgoing duplicate events
    suppress_dupes = True

    # Scope distance modifier - accept/deny events based on scope distance
    # None == accept all events
    # 2 == accept events up to and including the scan's configured search distance plus two
    # 1 == accept events up to and including the scan's configured search distance plus one
    # 0 == (DEFAULT) accept events up to and including the scan's configured search distance
    # -1 == accept events up to and including the scan's configured search distance minus one
    # -2 == accept events up to and including the scan's configured search distance minus two
    scope_distance_modifier = 0
    # Only accept the initial target event(s)
    target_only = False
    # Only accept explicitly in-scope events (scope distance == 0)
    # Use this options if your module is aggressive or if you don't want it to scale with
    #   the scan's search distance
    in_scope_only = False

    # Options, e.g. {"api_key": ""}
    options = {}
    # Options description, e.g. {"api_key": "API Key"}
    options_desc = {}
    # Maximum concurrent instances of handle_event() or handle_batch()
    max_event_handlers = 1
    # Batch size
    # If batch size > 1, override handle_batch() instead of handle_event()
    batch_size = 1
    # Seconds to wait before force-submitting batch
    batch_wait = 10
    # Use in conjunction with .request_with_fail_count() to set_error_state() after this many failed HTTP requests
    failed_request_abort_threshold = 5
    # When set to false, prevents events generated by this module from being automatically marked as in-scope
    # Useful for low-confidence modules like speculate and ipneighbor
    _scope_shepherding = True
    # Exclude from scan statistics
    _stats_exclude = False
    # outgoing queue size (0 == infinite)
    _qsize = 0
    # Priority of events raised by this module, 1-5, lower numbers == higher priority
    _priority = 3
    # Name, overridden automatically
    _name = "base"
    # Type, for differentiating between normal modules and output modules, etc.
    _type = "scan"

    def __init__(self, scan):
        self.scan = scan
        self.errored = False
        self._log = None
        self._incoming_event_queue = None
        # seconds since we've submitted a batch
        self._outgoing_event_queue = None
        # seconds since we've submitted a batch
        self._last_submitted_batch = None
        # additional callbacks to be executed alongside self.cleanup()
        self.cleanup_callbacks = []
        self._cleanedup = False
        self._watched_events = None

        self._task_counter = TaskCounter()

        # string constant
        self._custom_filter_criteria_msg = "it did not meet custom filter criteria"

        # track number of failures (for .request_with_fail_count())
        self._request_failures = 0

        self._tasks = []
        self._event_received = asyncio.Condition()
        self._event_queued = asyncio.Condition()
        self._event_dequeued = asyncio.Condition()

    async def setup(self):
        """
        Perform setup functions at the beginning of the scan.
        Optionally override this method.

        Must return True or False based on whether the setup was successful
        """
        return True

    async def handle_event(self, event):
        """
        Override this method if batch_size == 1.
        """
        pass

    def handle_batch(self, *events):
        """
        Override this method if batch_size > 1.
        """
        pass

    async def filter_event(self, event):
        """
        Accept/reject events based on custom criteria

        Override this method if you need more granular control
        over which events are distributed to your module
        """
        return True

    async def finish(self):
        """
        Perform final functions when scan is nearing completion

        For example,  if your module relies on the word cloud, you may choose to wait until
        the scan is finished (and the word cloud is most complete) before running an operation.

        Note that this method may be called multiple times, because it may raise events.
        Optionally override this method.
        """
        return

    async def report(self):
        """
        Perform a final task when the scan is finished, but before cleanup happens

        This is useful for modules that aggregate data and raise summary events at the end of a scan
        """
        return

    async def cleanup(self):
        """
        Perform final cleanup after the scan has finished
        This method is called only once, and may not raise events.
        Optionally override this method.
        """
        return

    async def require_api_key(self):
        """
        Use in setup() to ensure the module is configured with an API key
        """
        self.api_key = self.config.get("api_key", "")
        if self.auth_secret:
            try:
                await self.ping()
                self.hugesuccess(f"API is ready")
                return True
            except Exception as e:
                return None, f"Error with API ({str(e).strip()})"
        else:
            return None, "No API key set"

    async def ping(self):
        """
        Used in conjuction with require_api_key to ensure an API is up and responding

        Requires the use of an assert statement.

        E.g. if your API has a "/ping" endpoint, you can use it like this:
            def ping(self):
                r = self.request_with_fail_count(f"{self.base_url}/ping")
                resp_content = getattr(r, "text", "")
                assert getattr(r, "status_code", 0) == 200, resp_content
        """
        return

    @property
    def auth_secret(self):
        """
        Use this to indicate whether the module has everything it needs for authentication
        """
        return getattr(self, "api_key", "")

    def get_watched_events(self):
        """
        Override if you need your watched_events to be dynamic
        """
        if self._watched_events is None:
            self._watched_events = set(self.watched_events)
        return self._watched_events

    async def _handle_batch(self):
        submitted = False
        if self.batch_size <= 1:
            return
        if self.num_incoming_events > 0:
            events, finish, report = await self.events_waiting()
            if not self.errored:
                self.debug(f"Handling batch of {len(events):,} events")
                if events:
                    submitted = True
                    async with self.scan.acatch(context=f"{self.name}.handle_batch"):
                        with self._task_counter:
                            await self.handle_batch(*events)
                if finish:
                    async with self.scan.acatch(context=f"{self.name}.finish"):
                        await self.finish()
                elif report:
                    async with self.scan.acatch(context=f"{self.name}.report"):
                        await self.report()
        return submitted

    def make_event(self, *args, **kwargs):
        raise_error = kwargs.pop("raise_error", False)
        try:
            event = self.scan.make_event(*args, **kwargs)
        except ValidationError as e:
            if raise_error:
                raise
            self.warning(f"{e}")
            return
        if not event.module:
            event.module = self
        return event

    def emit_event(self, *args, **kwargs):
        event_kwargs = dict(kwargs)
        emit_kwargs = {}
        for o in ("on_success_callback", "abort_if", "quick"):
            v = event_kwargs.pop(o, None)
            if v is not None:
                emit_kwargs[o] = v
        event = self.make_event(*args, **event_kwargs)
        self.queue_outgoing_event(event, **emit_kwargs)

    async def events_waiting(self):
        """
        yields all events in queue, up to maximum batch size
        """
        events = []
        finish = False
        report = False
        while self.incoming_event_queue:
            if len(events) > self.batch_size:
                break
            try:
                event = self.incoming_event_queue.get_nowait()
                self.debug(f"Got {event} from {getattr(event, 'module', 'unknown_module')}")
                acceptable, reason = await self._event_postcheck(event)
                if acceptable:
                    if event.type == "FINISHED":
                        finish = True
                    else:
                        events.append(event)
                        self.scan.stats.event_consumed(event, self)
                elif reason:
                    self.debug(f"Not accepting {event} because {reason}")
            except asyncio.queues.QueueEmpty:
                break
        return events, finish, report

    @property
    def num_incoming_events(self):
        ret = 0
        if self.incoming_event_queue:
            ret = self.incoming_event_queue.qsize()
        return ret

    def start(self):
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.max_event_handlers)]

    async def _setup(self):
        status_codes = {False: "hard-fail", None: "soft-fail", True: "success"}

        status = False
        self.debug(f"Setting up module {self.name}")
        try:
            result = await self.setup()
            if type(result) == tuple and len(result) == 2:
                status, msg = result
            else:
                status = result
                msg = status_codes[status]
            self.debug(f"Finished setting up module {self.name}")
        except Exception as e:
            self.set_error_state()
            # soft-fail if it's only a wordlist error
            if isinstance(e, WordlistError):
                status = None
            msg = f"{e}"
            self.trace()
        return self.name, status, str(msg)

    async def _worker(self):
        async with self.scan.acatch(context=self._worker):
            while not self.scan.stopping:
                # hold the reigns if our outgoing queue is full
                if self._qsize > 0 and self.outgoing_event_queue.qsize() >= self._qsize:
                    async with self._event_dequeued:
                        await self._event_dequeued.wait()

                if self.batch_size > 1:
                    submitted = await self._handle_batch()
                    if not submitted:
                        async with self._event_received:
                            await self._event_received.wait()

                else:
                    try:
                        if self.incoming_event_queue:
                            event = await self.incoming_event_queue.get()
                        else:
                            self.debug(f"Event queue is in bad state")
                            return
                    except asyncio.queues.QueueEmpty:
                        continue
                    self.debug(f"Got {event} from {getattr(event, 'module', 'unknown_module')}")
                    acceptable, reason = await self._event_postcheck(event)
                    if not acceptable:
                        self.debug(f"Not accepting {event} because {reason}")
                    if acceptable:
                        if event.type == "FINISHED":
                            async with self.scan.acatch(context=f"{self.name}.finish"):
                                with self._task_counter:
                                    await self.finish()
                        else:
                            self.scan.stats.event_consumed(event, self)
                            async with self.scan.acatch(context=f"{self.name}.handle_event"):
                                with self._task_counter:
                                    await self.handle_event(event)

    @property
    def max_scope_distance(self):
        if self.in_scope_only or self.target_only:
            return 0
        return max(0, self.scan.scope_search_distance + self.scope_distance_modifier)

    def _event_precheck(self, event):
        """
        Check if an event should be accepted by the module
        Used when putting an event INTO the modules' queue
        """
        # special signal event types
        if event.type in ("FINISHED",):
            return True, ""
        if self.errored:
            return False, f"module is in error state"
        # exclude non-watched types
        if not any(t in self.get_watched_events() for t in ("*", event.type)):
            return False, "its type is not in watched_events"
        if self.target_only:
            if "target" not in event.tags:
                return False, "it did not meet target_only filter criteria"
        # exclude certain URLs (e.g. javascript):
        if event.type.startswith("URL") and self.name != "httpx" and "httpx-only" in event.tags:
            return False, "its extension was listed in url_extension_httpx_only"
        # if event is an IP address that was speculated from a CIDR
        source_is_range = getattr(event.source, "type", "") == "IP_RANGE"
        if (
            source_is_range
            and event.type == "IP_ADDRESS"
            and str(event.module) == "speculate"
            and self.name != "speculate"
        ):
            # and the current module listens for both ranges and CIDRs
            if all([x in self.watched_events for x in ("IP_RANGE", "IP_ADDRESS")]):
                # then skip the event.
                # this helps avoid double-portscanning both an individual IP and its parent CIDR.
                return False, "module consumes IP ranges directly"
        return True, ""

    async def _event_postcheck(self, event):
        """
        Check if an event should be accepted by the module
        Used when taking an event FROM the module's queue (immediately before it's handled)
        """
        # special exception for "FINISHED" event
        if event.type in ("FINISHED",):
            return True, ""

        # reject out-of-scope events for active modules
        # TODO: reconsider this
        if "active" in self.flags and "target" in event.tags and event not in self.scan.whitelist:
            return False, "it is not in whitelist and module has active flag"

        # check scope distance
        if self._type != "output":
            if self.in_scope_only:
                if event.scope_distance > 0:
                    return False, "it did not meet in_scope_only filter criteria"
            if self.scope_distance_modifier is not None:
                if event.scope_distance < 0:
                    return False, f"its scope_distance ({event.scope_distance}) is invalid."
                elif event.scope_distance > self.max_scope_distance:
                    return (
                        False,
                        f"its scope_distance ({event.scope_distance}) exceeds the maximum allowed by the scan ({self.scan.scope_search_distance}) + the module ({self.scope_distance_modifier}) == {self.max_scope_distance}",
                    )

        # custom filtering
        async with self.scan.acatch(context=self.filter_event):
            filter_result = await self.filter_event(event)
            msg = str(self._custom_filter_criteria_msg)
            with suppress(ValueError, TypeError):
                filter_result, reason = filter_result
                msg += f": {reason}"
            if not filter_result:
                return False, msg

        if self._type == "output" and not event._stats_recorded:
            event._stats_recorded = True
            self.scan.stats.event_produced(event)

        self.debug(f"{event} passed post-check")
        return True, ""

    async def _cleanup(self):
        if not self._cleanedup:
            self._cleanedup = True
            for callback in [self.cleanup] + self.cleanup_callbacks:
                if callable(callback):
                    async with self.scan.acatch(context=self.name):
                        with self._task_counter:
                            await self.helpers.execute_sync_or_async(callback)

    async def queue_event(self, event):
        """
        Queue (incoming) event with module
        """
        if self.incoming_event_queue in (None, False):
            self.debug(f"Not in an acceptable state to queue incoming event")
            return
        acceptable, reason = self._event_precheck(event)
        if not acceptable:
            if reason and reason != "its type is not in watched_events":
                self.debug(f"Not accepting {event} because {reason}")
            return
        try:
            self.incoming_event_queue.put_nowait(event)
            async with self._event_received:
                self._event_received.notify()
        except AttributeError:
            self.debug(f"Not in an acceptable state to queue incoming event")

    def queue_outgoing_event(self, event, **kwargs):
        """
        Queue (outgoing) event with module
        """
        try:
            self.outgoing_event_queue.put_nowait((event, kwargs))
        except AttributeError:
            self.debug(f"Not in an acceptable state to queue outgoing event")

    async def dequeue_outgoing_event(self):
        await self.outgoing_event_queue.get()
        with self._event_dequeued:
            self._event_dequeued.notify()

    def set_error_state(self, message=None):
        if not self.errored:
            if message is not None:
                self.warning(str(message))
            self.debug(f"Setting error state for module {self.name}")
            self.errored = True
            # clear incoming queue
            if self.incoming_event_queue:
                self.debug(f"Emptying event_queue")
                with suppress(asyncio.queues.QueueEmpty):
                    while 1:
                        self.incoming_event_queue.get_nowait()
                # set queue to None to prevent its use
                # if there are leftover objects in the queue, the scan will hang.
                self._incoming_event_queue = False

    @property
    def name(self):
        return str(self._name)

    @property
    def helpers(self):
        return self.scan.helpers

    @property
    def status(self):
        status = {
            "events": {"incoming": self.num_incoming_events, "outgoing": self.outgoing_event_queue.qsize()},
            "tasks": self._task_counter.value,
            "errored": self.errored,
        }
        status["running"] = self.running
        return status

    @property
    def running(self):
        """
        Indicates whether the module is currently processing data.
        """
        return self._task_counter.value > 0

    @property
    def finished(self):
        """
        Indicates whether the module is finished (not running and nothing in queues)
        """
        return not self.running and self.num_incoming_events <= 0 and self.outgoing_event_queue.qsize() <= 0

    async def request_with_fail_count(self, *args, **kwargs):
        r = await self.helpers.request(*args, **kwargs)
        if r is None:
            self._request_failures += 1
        else:
            self._request_failures = 0
        if self._request_failures >= self.failed_request_abort_threshold:
            self.set_error_state(f"Setting error state due to {self._request_failures:,} failed HTTP requests")
        return r

    def is_spider_danger(self, source_event, url):
        url_depth = self.helpers.url_depth(url)
        web_spider_depth = self.scan.config.get("web_spider_depth", 1)
        spider_distance = getattr(source_event, "web_spider_distance", 0) + 1
        web_spider_distance = self.scan.config.get("web_spider_distance", 0)
        if (url_depth > web_spider_depth) or (spider_distance > web_spider_distance):
            return True
        return False

    @property
    def config(self):
        config = self.scan.config.get("modules", {}).get(self.name, {})
        if config is None:
            config = {}
        return config

    @property
    def incoming_event_queue(self):
        if self._incoming_event_queue is None:
            self._incoming_event_queue = asyncio.PriorityQueue()
        return self._incoming_event_queue

    @property
    def outgoing_event_queue(self):
        if self._outgoing_event_queue is None:
            self._outgoing_event_queue = asyncio.PriorityQueue()
        return self._outgoing_event_queue

    @property
    def priority(self):
        return int(max(1, min(5, self._priority)))

    @property
    def auth_required(self):
        return self.meta.get("auth_required", False)

    @property
    def log(self):
        if getattr(self, "_log", None) is None:
            self._log = logging.getLogger(f"bbot.modules.{self.name}")
        return self._log

    @property
    def memory_usage(self):
        """
        Return how much memory the module is currently using in bytes
        """
        seen = {self.scan, self.helpers, self.log}
        return get_size(self, max_depth=3, seen=seen)

    def __str__(self):
        return self.name

    def log_table(self, *args, **kwargs):
        table_name = kwargs.pop("table_name", None)
        table = self.helpers.make_table(*args, **kwargs)
        for line in table.splitlines():
            self.info(line)
        if table_name is not None:
            date = self.helpers.make_date()
            filename = self.scan.home / f"{self.helpers.tagify(table_name)}-table-{date}.txt"
            with open(filename, "w") as f:
                f.write(table)
            self.verbose(f"Wrote {table_name} to {filename}")
        return table

    def stdout(self, *args, **kwargs):
        self.log.stdout(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def debug(self, *args, **kwargs):
        self.log.debug(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def verbose(self, *args, **kwargs):
        self.log.verbose(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugeverbose(self, *args, **kwargs):
        self.log.hugeverbose(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def info(self, *args, **kwargs):
        self.log.info(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugeinfo(self, *args, **kwargs):
        self.log.hugeinfo(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def success(self, *args, **kwargs):
        self.log.success(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def hugesuccess(self, *args, **kwargs):
        self.log.hugesuccess(*args, extra={"scan_id": self.scan.id}, **kwargs)

    def warning(self, *args, **kwargs):
        self.log.warning(*args, extra={"scan_id": self.scan.id}, **kwargs)
        self.trace()

    def hugewarning(self, *args, **kwargs):
        self.log.hugewarning(*args, extra={"scan_id": self.scan.id}, **kwargs)
        self.trace()

    def error(self, *args, **kwargs):
        self.log.error(*args, extra={"scan_id": self.scan.id}, **kwargs)
        self.trace()

    def trace(self):
        e_type, e_val, e_traceback = exc_info()
        if e_type is not None:
            self.log.trace(traceback.format_exc())

    def critical(self, *args, **kwargs):
        self.log.critical(*args, extra={"scan_id": self.scan.id}, **kwargs)
        self.trace()
