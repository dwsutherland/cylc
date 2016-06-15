#!/usr/bin/env python

# THIS FILE IS PART OF THE CYLC SUITE ENGINE.
# Copyright (C) 2008-2016 NIWA
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Provide a class to represent a task proxy in a running suite."""

from collections import namedtuple
from copy import copy
from logging import (
    getLevelName, getLogger, CRITICAL, ERROR, WARNING, INFO, DEBUG)
import os
from pipes import quote
import Queue
from random import randrange
import re
import shlex
from shutil import rmtree
import time
import traceback

from isodatetime.timezone import get_local_time_zone

from cylc.mkdir_p import mkdir_p
from cylc.cfgspec.globalcfg import GLOBAL_CFG
import cylc.cycling.iso8601
from cylc.envvar import expandvars
import cylc.flags as flags
from cylc.wallclock import (
    get_current_time_string,
    get_time_string_from_unix_time,
    get_seconds_as_interval_string,
    RE_DATE_TIME_FORMAT_EXTENDED
)
from cylc.network.task_msgqueue import TaskMessageServer
from cylc.host_select import get_task_host
from cylc.job_file import JOB_FILE
from cylc.job_host import RemoteJobHostManager
from cylc.batch_sys_manager import BATCH_SYS_MANAGER
from cylc.owner import is_remote_user, USER
from cylc.poll_timer import PollTimer
from cylc.suite_host import is_remote_host, get_suite_host
from parsec.util import pdeepcopy, poverride
from parsec.OrderedDict import OrderedDictWithDefaults
from cylc.mp_pool import SuiteProcPool, SuiteProcContext
from cylc.rundb import CylcSuiteDAO
from cylc.task_id import TaskID
from cylc.task_message import TaskMessage
from parsec.util import pdeepcopy, poverride
from parsec.config import ItemNotFoundError
from cylc.task_state import (
    TaskState, TASK_STATUSES_ACTIVE, TASK_STATUS_WAITING, TASK_STATUS_EXPIRED,
    TASK_STATUS_READY, TASK_STATUS_SUBMITTED, TASK_STATUS_SUBMIT_FAILED,
    TASK_STATUS_RUNNING, TASK_STATUS_SUCCEEDED, TASK_STATUS_FAILED)
from cylc.task_outputs import (
    TASK_OUTPUT_STARTED, TASK_OUTPUT_SUCCEEDED, TASK_OUTPUT_FAILED)


CustomTaskEventHandlerContext = namedtuple(
    "CustomTaskEventHandlerContext",
    ["key", "ctx_type", "cmd"])


TaskEventMailContext = namedtuple(
    "TaskEventMailContext",
    ["key", "ctx_type", "event", "mail_from", "mail_to", "mail_smtp"])


TaskJobLogsRegisterContext = namedtuple(
    "TaskJobLogsRegisterContext",
    ["key", "ctx_type"])


TaskJobLogsRetrieveContext = namedtuple(
    "TaskJobLogsRetrieveContext",
    ["key", "ctx_type", "user_at_host", "max_size"])


class TryState(object):
    """Represent the current state of a (re)try."""

    # Memory optimization - constrain possible attributes to this list.
    __slots__ = ["ctx", "delays", "num", "delay", "timeout", "is_waiting"]

    def __init__(self, ctx=None, delays=None):
        self.ctx = ctx
        if delays:
            self.delays = list(delays)
        else:
            self.delays = [0]
        self.num = 0
        self.delay = None
        self.timeout = None
        self.is_waiting = False

    def delay_as_seconds(self):
        """Return the delay as PTnS, where n is number of seconds."""
        return get_seconds_as_interval_string(self.delay)

    def is_delay_done(self, now=None):
        """Is timeout done?"""
        if self.timeout is None:
            return False
        if now is None:
            now = time.time()
        return now > self.timeout

    def is_timeout_set(self):
        """Return True if timeout is set."""
        return self.timeout is not None

    def next(self):
        """Return the next retry delay if there is one, or None otherwise."""
        try:
            self.delay = self.delays[self.num]
        except IndexError:
            return None
        else:
            self.timeout = time.time() + self.delay
            self.num += 1
            return self.delay

    def set_waiting(self):
        """Set waiting flag, while waiting for action to complete."""
        self.delay = None
        self.is_waiting = True
        self.timeout = None

    def unset_waiting(self):
        """Unset waiting flag after an action has completed."""
        self.is_waiting = False

    def timeout_as_str(self):
        """Return the timeout as an ISO8601 date-time string."""
        return get_time_string_from_unix_time(self.timeout)


class TaskProxySequenceBoundsError(ValueError):
    """Error on TaskProxy.__init__ with out of sequence bounds start point."""

    def __str__(self):
        return "Not loading %s (out of sequence bounds)" % self.args[0]


class TaskProxy(object):
    """The task proxy."""

    # RETRY LOGIC:
    #  1) ABSOLUTE SUBMIT NUMBER increments every time a task is
    #  submitted, manually or automatically by (submission or execution)
    # retries; whether or not the task actually begins executing, and is
    # appended to the task log root filename.
    #  2) SUBMISSION TRY NUMBER increments when task job submission
    # fails, if submission retries are configured, but resets to 1 if
    # the task begins executing; and is used for accounting purposes.
    #  3) EXECUTION TRY NUMBER increments only when task execution fails,
    # if execution retries are configured; and is passed to task
    # environments to allow changed behaviour after previous failures.

    # Format string for single line output
    JOB_LOG_FMT_1 = "%(timestamp)s [%(cmd_key)s %(attr)s] %(mesg)s"
    # Format string for multi-line output
    JOB_LOG_FMT_M = "%(timestamp)s [%(cmd_key)s %(attr)s]\n\n%(mesg)s\n"

    CUSTOM_EVENT_HANDLER = "event-handler"
    EVENT_MAIL = "event-mail"
    JOB_KILL = "job-kill"
    JOB_LOGS_REGISTER = "job-logs-register"
    JOB_LOGS_RETRIEVE = "job-logs-retrieve"
    JOB_POLL = "job-poll"
    JOB_SUBMIT = "job-submit"
    MANAGE_JOB_LOGS_TRY_DELAYS = (0, 30, 180)  # PT0S, PT30S, PT3M
    MESSAGE_SUFFIX_RE = re.compile(
        ' at (' + RE_DATE_TIME_FORMAT_EXTENDED + '|unknown-time)$')

    LOGGING_LVL_OF = {
        "INFO": INFO,
        "NORMAL": INFO,
        "WARNING": WARNING,
        "ERROR": ERROR,
        "CRITICAL": CRITICAL,
        "DEBUG": DEBUG,
    }

    TABLE_TASK_JOBS = CylcSuiteDAO.TABLE_TASK_JOBS
    TABLE_TASK_JOB_LOGS = CylcSuiteDAO.TABLE_TASK_JOB_LOGS
    TABLE_TASK_EVENTS = CylcSuiteDAO.TABLE_TASK_EVENTS
    TABLE_TASK_STATES = CylcSuiteDAO.TABLE_TASK_STATES

    POLLED_INDICATOR = "(polled)"

    event_handler_env = {}
    stop_sim_mode_job_submission = False

    @classmethod
    def get_job_log_dir(
            cls, task_name, task_point, submit_num="NN", suite=None):
        """Return the latest job log path on the suite host."""
        try:
            submit_num = "%02d" % submit_num
        except TypeError:
            pass
        if suite:
            return os.path.join(
                GLOBAL_CFG.get_derived_host_item(
                    suite, "suite job log directory"),
                str(task_point), task_name, submit_num)
        else:
            return os.path.join(str(task_point), task_name, submit_num)

    def __init__(
            self, tdef, start_point, status=TASK_STATUS_WAITING,
            has_spawned=False, stop_point=None, is_startup=False,
            validate_mode=False, submit_num=0, is_reload_or_restart=False,
            pre_reload_inst=None, message_queue=None):
        self.tdef = tdef
        if submit_num is None:
            self.submit_num = 0
        else:
            self.submit_num = submit_num
        self.validate_mode = validate_mode
        self.message_queue = message_queue

        if is_startup:
            # adjust up to the first on-sequence cycle point
            adjusted = []
            for seq in self.tdef.sequences:
                adj = seq.get_first_point(start_point)
                if adj:
                    # may be None if out of sequence bounds
                    adjusted.append(adj)
            if not adjusted:
                # This task is out of sequence bounds
                raise TaskProxySequenceBoundsError(self.tdef.name)
            self.point = min(adjusted)
            self.cleanup_cutoff = self.tdef.get_cleanup_cutoff_point(
                self.point, self.tdef.intercycle_offsets)
            self.identity = TaskID.get(self.tdef.name, self.point)
        else:
            self.point = start_point
            self.cleanup_cutoff = self.tdef.get_cleanup_cutoff_point(
                self.point, self.tdef.intercycle_offsets)
            self.identity = TaskID.get(self.tdef.name, self.point)

        self.has_spawned = has_spawned
        self.point_as_seconds = None

        # Manually inserted tasks may have a final cycle point set.
        self.stop_point = stop_point

        self.job_conf = None
        self.manual_trigger = False
        self.is_manual_submit = False

        self.submitted_time = None
        self.started_time = None
        self.finished_time = None

        self.summary = {
            'latest_message': "",
            'submitted_time': None,
            'submitted_time_string': None,
            'submit_num': self.submit_num,
            'started_time': None,
            'started_time_string': None,
            'finished_time': None,
            'finished_time_string': None,
            'name': self.tdef.name,
            'description': self.tdef.rtconfig['description'],
            'title': self.tdef.rtconfig['title'],
            'label': str(self.point),
            'logfiles': [],
            'job_hosts': {},
        }
        for lfile in self.tdef.rtconfig['extra log files']:
            self.summary['logfiles'].append(expandvars(lfile))

        self.job_file_written = False

        self.retries_configured = False

        self.run_try_state = TryState()
        self.sub_try_state = TryState()
        self.event_handler_try_states = {}

        self.db_inserts_map = {
            self.TABLE_TASK_JOBS: [],
            self.TABLE_TASK_JOB_LOGS: [],
            self.TABLE_TASK_STATES: [],
            self.TABLE_TASK_EVENTS: [],
        }
        self.db_updates_map = {
            self.TABLE_TASK_JOBS: [],
            self.TABLE_TASK_STATES: [],
        }

        # TODO - should take suite name from config!
        self.suite_name = os.environ['CYLC_SUITE_NAME']

        # In case task owner and host are needed by db_events_insert()
        # for pre-submission events, set their initial values as if
        # local (we can't know the correct host prior to this because
        # dynamic host selection could be used).
        self.task_host = 'localhost'
        self.task_owner = None
        self.user_at_host = self.task_host

        self.submit_method_id = None
        self.batch_sys_name = None
        self.job_vacated = False

        self.submission_poll_timer = None
        self.execution_poll_timer = None

        self.logger = getLogger("main")

        # An initial db state entry is created at task proxy init. On reloading
        # or restarting the suite, the task proxies already have this db entry.
        if (not self.validate_mode and not is_reload_or_restart and
                self.submit_num == 0):
            self.db_inserts_map[self.TABLE_TASK_STATES].append({
                "time_created": get_current_time_string(),
                "time_updated": get_current_time_string(),
                "try_num": self.run_try_state.num + 1,
                "status": status})

        if not self.validate_mode and self.submit_num > 0:
            self.db_updates_map[self.TABLE_TASK_STATES].append({
                "time_updated": get_current_time_string(),
                "status": status})

        self.event_hooks = None
        self.sim_mode_run_length = None
        self.set_from_rtconfig()
        self.delayed_start_str = None
        self.delayed_start = None
        self.expire_time_str = None
        self.expire_time = None

        self.state = TaskState(status, self.point, self.identity, tdef,
                               self.db_events_insert, self.db_update_status,
                               self.log)

        if tdef.sequential:
            # Adjust clean-up cutoff.
            p_next = None
            adjusted = []
            for seq in tdef.sequences:
                nxt = seq.get_next_point(self.point)
                if nxt:
                    # may be None if beyond the sequence bounds
                    adjusted.append(nxt)
            if adjusted:
                p_next = min(adjusted)
                if (self.cleanup_cutoff is not None and
                        self.cleanup_cutoff < p_next):
                    self.cleanup_cutoff = p_next

        if is_reload_or_restart and pre_reload_inst is not None:
            self.log(INFO, 'reloaded task definition')
            if pre_reload_inst.state.status in TASK_STATUSES_ACTIVE:
                self.log(WARNING, "job is active with pre-reload settings")
            # Retain some state from my pre suite-reload predecessor.
            self.has_spawned = pre_reload_inst.has_spawned
            self.summary = pre_reload_inst.summary
            self.started_time = pre_reload_inst.started_time
            self.submitted_time = pre_reload_inst.submitted_time
            self.finished_time = pre_reload_inst.finished_time
            self.run_try_state = pre_reload_inst.run_try_state
            self.sub_try_state = pre_reload_inst.sub_try_state
            self.submit_num = pre_reload_inst.submit_num
            self.db_inserts_map = pre_reload_inst.db_inserts_map
            self.db_updates_map = pre_reload_inst.db_updates_map
            # Retain status of outputs.
            for msg, oid in pre_reload_inst.state.outputs.completed.items():
                self.state.outputs.completed[msg] = oid
                try:
                    del self.state.outputs.not_completed[msg]
                except:
                    pass

    def _get_events_conf(self, key, default=None):
        """Return an events setting from suite then global configuration."""
        for getter in (
                self.tdef.rtconfig["events"],
                self.event_hooks,
                GLOBAL_CFG.get()["task events"]):
            try:
                value = getter.get(key)
                if value is not None:
                    return value
            except (ItemNotFoundError, KeyError):
                pass
        return default

    def _get_host_conf(self, key, default=None):
        """Return a host setting from suite then global configuration."""
        if self.tdef.rtconfig["remote"].get(key) is not None:
            return self.tdef.rtconfig["remote"][key]
        else:
            try:
                return GLOBAL_CFG.get_host_item(
                    key, self.task_host, self.task_owner)
            except ItemNotFoundError:
                pass
        return default

    def log(self, lvl=INFO, msg=""):
        """Log a message of this task proxy."""
        msg = "[%s] -%s" % (self.identity, msg)
        self.logger.log(lvl, msg)

    def command_log(self, ctx):
        """Log an activity for a job of this task proxy."""
        ctx_str = str(ctx)
        if not ctx_str:
            return
        submit_num = "NN"
        if isinstance(ctx.cmd_key, tuple):  # An event handler
            submit_num = ctx.cmd_key[-1]
        job_log_dir = self.get_job_log_dir(
            self.tdef.name, self.point, submit_num, self.suite_name)
        job_activity_log = os.path.join(job_log_dir, "job-activity.log")
        try:
            with open(job_activity_log, "ab") as handle:
                handle.write(ctx_str)
        except IOError as exc:
            self.log(WARNING, "%s: write failed\n%s" % (job_activity_log, exc))
        if ctx.cmd and ctx.ret_code:
            self.log(ERROR, ctx_str)
        elif ctx.cmd:
            self.log(DEBUG, ctx_str)

    def db_events_insert(self, event="", message=""):
        """Record an event to the DB."""
        self.db_inserts_map[self.TABLE_TASK_EVENTS].append({
            "time": get_current_time_string(),
            "event": event,
            "message": message,
            "misc": self.user_at_host})

    def db_update_status(self):
        self.db_updates_map[self.TABLE_TASK_STATES].append({
            "time_updated": get_current_time_string(),
            "submit_num": self.submit_num,
            "try_num": self.run_try_state.num + 1,
            "status": self.state.status})

    def retry_delay_done(self):
        """Is retry delay done? Can I retry now?"""
        now = time.time()
        return (self.run_try_state.is_delay_done(now) or
                self.sub_try_state.is_delay_done(now))

    def ready_to_run(self):
        """Am I in a pre-run state but ready to run?

        Queued tasks are not counted as they've already been deemed ready.

        """
        ready = self.state.is_ready_to_run(self.retry_delay_done(),
                                           self.start_time_reached())
        if ready and self._has_expired():
            self.log(WARNING, 'Task expired (skipping job).')
            self.setup_event_handlers(
                "expired", 'Task expired (skipping job).')
            self.state.set_expired()
            return False
        return ready

    def get_point_as_seconds(self):
        """Compute and store my cycle point as seconds."""
        if self.point_as_seconds is None:
            iso_timepoint = cylc.cycling.iso8601.point_parse(str(self.point))
            self.point_as_seconds = int(iso_timepoint.get(
                "seconds_since_unix_epoch"))
            if iso_timepoint.time_zone.unknown:
                utc_offset_hours, utc_offset_minutes = (
                    get_local_time_zone())
                utc_offset_in_seconds = (
                    3600 * utc_offset_hours + 60 * utc_offset_minutes)
                self.point_as_seconds += utc_offset_in_seconds
        return self.point_as_seconds

    def get_offset_as_seconds(self, offset):
        """Return an ISO interval as seconds."""
        iso_offset = cylc.cycling.iso8601.interval_parse(str(offset))
        return int(iso_offset.get_seconds())

    def start_time_reached(self):
        """Has this task reached its clock trigger time?"""
        if self.tdef.clocktrigger_offset is None:
            return True
        if self.delayed_start is None:
            self.delayed_start = (
                self.get_point_as_seconds() +
                self.get_offset_as_seconds(self.tdef.clocktrigger_offset))
            self.delayed_start_str = get_time_string_from_unix_time(
                self.delayed_start)
        return time.time() > self.delayed_start

    def _has_expired(self):
        """Is this task past its use-by date?"""
        if self.tdef.expiration_offset is None:
            return False
        if self.expire_time is None:
            self.expire_time = (
                self.get_point_as_seconds() +
                self.get_offset_as_seconds(self.tdef.expiration_offset))
            self.expire_time_str = get_time_string_from_unix_time(
                self.expire_time)
        return time.time() > self.expire_time

    def job_submission_callback(self, result):
        """Callback on job submission."""
        if result.out is not None:
            out = ""
            for line in result.out.splitlines(True):
                if line.startswith(
                        BATCH_SYS_MANAGER.CYLC_BATCH_SYS_JOB_ID + "="):
                    self.submit_method_id = line.strip().replace(
                        BATCH_SYS_MANAGER.CYLC_BATCH_SYS_JOB_ID + "=", "")
                else:
                    out += line
            result.out = out
        self.command_log(result)

        if result.ret_code == SuiteProcPool.JOB_SKIPPED_FLAG:
            return

        if self.submit_method_id and result.ret_code == 0:
            self.job_submission_succeeded()
        else:
            self.job_submission_failed()

    def job_poll_callback(self, cmd_ctx, line):
        """Callback on job poll."""
        ctx = SuiteProcContext(self.JOB_POLL, None)
        ctx.out = line
        ctx.ret_code = 0

        items = line.split("|")
        # See cylc.batch_sys_manager.JobPollContext
        try:
            (
                batch_sys_exit_polled, run_status, run_signal, _, time_run
            ) = items[4:9]
        except IndexError:
            self.summary['latest_message'] = 'poll failed'
            flags.iflag = True
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
            return
        finally:
            self.command_log(ctx)
        if run_status == "1" and run_signal in ["ERR", "EXIT"]:
            # Failed normally
            self._process_poll_message(INFO, TASK_OUTPUT_FAILED)
        elif run_status == "1" and batch_sys_exit_polled == "1":
            # Failed by a signal, and no longer in batch system
            self._process_poll_message(INFO, TASK_OUTPUT_FAILED)
            self._process_poll_message(
                INFO, TaskMessage.FAIL_MESSAGE_PREFIX + run_signal)
        elif run_status == "1":
            # The job has terminated, but is still managed by batch system.
            # Some batch system may restart a job in this state, so don't
            # mark as failed yet.
            self._process_poll_message(INFO, TASK_OUTPUT_STARTED)
        elif run_status == "0":
            # The job succeeded
            self._process_poll_message(INFO, TASK_OUTPUT_SUCCEEDED)
        elif time_run and batch_sys_exit_polled == "1":
            # The job has terminated without executing the error trap
            self._process_poll_message(INFO, TASK_OUTPUT_FAILED)
        elif time_run:
            # The job has started, and is still managed by batch system
            self._process_poll_message(INFO, TASK_OUTPUT_STARTED)
        elif batch_sys_exit_polled == "1":
            # The job never ran, and no longer in batch system
            self._process_poll_message(INFO, "submission failed")
        else:
            # The job never ran, and is in batch system
            self._process_poll_message(INFO, TASK_STATUS_SUBMITTED)

    def _process_poll_message(self, priority, message):
        """Wraps self.process_incoming_message for poll messages."""
        self.process_incoming_message((priority, message), msg_was_polled=True)

    def job_poll_message_callback(self, cmd_ctx, line):
        """Callback on job poll message."""
        ctx = SuiteProcContext(self.JOB_POLL, None)
        ctx.out = line
        try:
            priority, message = line.split("|")[3:5]
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = 0
            self.process_incoming_message(
                (priority, message), msg_was_polled=True)
        self.command_log(ctx)

    def job_kill_callback(self, cmd_ctx, line):
        """Callback on job kill."""
        ctx = SuiteProcContext(self.JOB_KILL, None)
        ctx.out = line
        try:
            ctx.timestamp, _, ctx.ret_code = line.split("|", 2)
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = int(ctx.ret_code)
            if ctx.ret_code:
                ctx.cmd = cmd_ctx.cmd  # print original command on failure
        self.command_log(ctx)
        log_lvl = INFO
        log_msg = 'killed'
        if ctx.ret_code:  # non-zero exit status
            log_lvl = WARNING
            log_msg = 'kill failed'
            self.state.kill_failed = True
        elif self.state.status == TASK_STATUS_SUBMITTED:
            self.job_submission_failed()
            flags.iflag = True
        elif self.state.status == TASK_STATUS_RUNNING:
            self.job_execution_failed()
            flags.iflag = True
        else:
            log_lvl = WARNING
            log_msg = (
                'ignoring job kill result, unexpected task state: %s' %
                self.state.status)
        self.summary['latest_message'] = log_msg
        self.log(log_lvl, "job(%02d) %s" % (self.submit_num, log_msg))

    def job_submit_callback(self, cmd_ctx, line):
        """Callback on job submit."""
        ctx = SuiteProcContext(self.JOB_SUBMIT, None)
        ctx.out = line
        items = line.split("|")
        try:
            ctx.timestamp, _, ctx.ret_code = items[0:3]
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = int(ctx.ret_code)
            if ctx.ret_code:
                ctx.cmd = cmd_ctx.cmd  # print original command on failure
        self.command_log(ctx)

        if ctx.ret_code == SuiteProcPool.JOB_SKIPPED_FLAG:
            return

        try:
            self.submit_method_id = items[3]
        except IndexError:
            self.submit_method_id = None
        self.register_job_logs(self.submit_num)
        if self.submit_method_id and ctx.ret_code == 0:
            self.job_submission_succeeded()
        else:
            self.job_submission_failed()

    def job_cmd_out_callback(self, cmd_ctx, line):
        """Callback on job command STDOUT/STDERR."""
        job_log_dir = self.get_job_log_dir(
            self.tdef.name, self.point, "NN", self.suite_name)
        job_activity_log = os.path.join(job_log_dir, "job-activity.log")
        if cmd_ctx.cmd_kwargs.get("host") and cmd_ctx.cmd_kwargs.get("user"):
            user_at_host = "(%(user)s@%(host)s) " % cmd_ctx.cmd_kwargs
        elif cmd_ctx.cmd_kwargs.get("host"):
            user_at_host = "(%(host)s) " % cmd_ctx.cmd_kwargs
        elif cmd_ctx.cmd_kwargs.get("user"):
            user_at_host = "(%(user)s@localhost) " % cmd_ctx.cmd_kwargs
        else:
            user_at_host = ""
        try:
            timestamp, _, content = line.split("|")
        except ValueError:
            pass
        else:
            line = "%s %s" % (timestamp, content)
        try:
            with open(job_activity_log, "ab") as handle:
                if not line.endswith("\n"):
                    line += "\n"
                handle.write(user_at_host + line)
        except IOError as exc:
            self.log(WARNING, "%s: write failed\n%s" % (job_activity_log, exc))

    def setup_event_handlers(
            self, event, message, db_update=True, db_event=None, db_msg=None):
        """Set up event handlers."""
        # extra args for inconsistent use between events, logging, and db
        # updates
        db_event = db_event or event
        if db_update:
            self.db_events_insert(event=db_event, message=db_msg)

        if self.tdef.run_mode != 'live':
            return

        self.setup_job_logs_retrieval(event, message)
        self.setup_event_mail(event, message)
        self.setup_custom_event_handlers(event, message)

    def setup_job_logs_retrieval(self, event, _=None):
        """Set up remote job logs retrieval."""
        # TODO - use string constants for event names.
        if event not in ['failed', 'retry', 'succeeded']:
            return
        if (self.user_at_host in [USER + '@localhost', 'localhost'] or
                not self._get_host_conf("retrieve job logs")):
            key2 = (self.JOB_LOGS_REGISTER, self.submit_num)
            if key2 in self.event_handler_try_states:
                return
            self.event_handler_try_states[key2] = TryState(
                TaskJobLogsRegisterContext(
                    # key, ctx_type
                    self.JOB_LOGS_REGISTER, self.JOB_LOGS_REGISTER,
                ),
                self._get_events_conf("register job logs retry delays", []))
        else:
            key2 = (self.JOB_LOGS_RETRIEVE, self.submit_num)
            if key2 in self.event_handler_try_states:
                return
            self.event_handler_try_states[key2] = TryState(
                TaskJobLogsRetrieveContext(
                    # key
                    self.JOB_LOGS_RETRIEVE,
                    # ctx_type
                    self.JOB_LOGS_RETRIEVE,
                    self.user_at_host,
                    # max_size
                    self._get_host_conf("retrieve job logs max size"),
                ),
                self._get_host_conf("retrieve job logs retry delays", []))

    def setup_event_mail(self, event, message):
        """Event notification, by email."""
        key1 = (self.EVENT_MAIL, event)
        if ((key1, self.submit_num) in self.event_handler_try_states or
                event not in self._get_events_conf("mail events", [])):
            return

        self.event_handler_try_states[(key1, self.submit_num)] = TryState(
            TaskEventMailContext(
                key1,
                self.EVENT_MAIL,  # ctx_type
                event,
                self._get_events_conf(  # mail_from
                    "mail from",
                    "notifications@" + get_suite_host(),
                ),
                self._get_events_conf("mail to", USER),  # mail_to
                self._get_events_conf("mail smtp"),  # mail_smtp
            ),
            self._get_events_conf("mail retry delays", []))

    def setup_custom_event_handlers(self, event, message, only_list=None):
        """Call custom event handlers."""
        handlers = []
        if self.event_hooks[event + ' handler']:
            handlers = self.event_hooks[event + ' handler']
        elif (self._get_events_conf('handlers', []) and
                event in self._get_events_conf('handler events', [])):
            handlers = self._get_events_conf('handlers', [])
        retry_delays = self._get_events_conf(
            'handler retry delays',
            self._get_host_conf("task event handler retry delays", []))
        for i, handler in enumerate(handlers):
            key1 = ("%s-%02d" % (self.CUSTOM_EVENT_HANDLER, i), event)
            if (key1, self.submit_num) in self.event_handler_try_states or (
                    only_list and i not in only_list):
                continue
            cmd = handler % {
                "event": quote(event),
                "suite": quote(self.suite_name),
                "point": quote(str(self.point)),
                "name": quote(self.tdef.name),
                "submit_num": self.submit_num,
                "id": quote(self.identity),
                "message": quote(message),
            }
            if cmd == handler:
                # Nothing substituted, assume classic interface
                cmd = "%s '%s' '%s' '%s' '%s'" % (
                    handler, event, self.suite_name, self.identity, message)
            self.log(DEBUG, "Queueing %s handler: %s" % (event, cmd))
            self.event_handler_try_states[(key1, self.submit_num)] = TryState(
                CustomTaskEventHandlerContext(
                    key1,
                    self.CUSTOM_EVENT_HANDLER,
                    cmd,
                ),
                retry_delays)

    def custom_event_handler_callback(self, result):
        """Callback when a custom event handler is done."""
        self.command_log(result)
        try:
            if result.ret_code == 0:
                del self.event_handler_try_states[result.cmd_key]
            else:
                self.event_handler_try_states[result.cmd_key].unset_waiting()
        except KeyError:
            pass

    def job_submission_failed(self):
        """Handle job submission failure."""
        self.log(ERROR, 'submission failed')
        self.db_updates_map[self.TABLE_TASK_JOBS].append({
            "time_submit_exit": get_current_time_string(),
            "submit_status": 1,
        })
        self.submit_method_id = None
        if self.sub_try_state.next() is None:
            # No submission retry lined up: definitive failure.
            flags.pflag = True
            # See github #476.
            self.setup_event_handlers(
                'submission failed', 'job submission failed')
            self.state.set_submit_failed()
        else:
            # There is a submission retry lined up.
            timeout_str = self.sub_try_state.timeout_as_str()

            delay_msg = "submit-retrying in %s" % (
                self.sub_try_state.delay_as_seconds())
            msg = "submission failed, %s (after %s)" % (delay_msg, timeout_str)
            self.log(INFO, "job(%02d) " % self.submit_num + msg)
            self.summary['latest_message'] = msg
            self.db_events_insert(
                event="submission failed", message=delay_msg)
            # TODO - is this insert redundant with setup_event_handlers?
            self.db_events_insert(
                event="submission failed",
                message="submit-retrying in " + str(self.sub_try_state.delay))
            self.setup_event_handlers(
                "submission retry", "job submission failed, " + delay_msg)
            self.state.set_submit_retry()

    def job_submission_succeeded(self):
        """Handle job submission succeeded."""
        if self.submit_method_id is not None:
            self.log(INFO, 'submit_method_id=' + self.submit_method_id)
        self.log(INFO, 'submission succeeded')
        now = get_current_time_string()
        self.db_updates_map[self.TABLE_TASK_STATES].append({
            "time_updated": now,
            "submit_method_id": self.submit_method_id})
        self.db_updates_map[self.TABLE_TASK_JOBS].append({
            "time_submit_exit": now,
            "submit_status": 0,
            "batch_sys_job_id": self.submit_method_id})

        if self.tdef.run_mode == 'simulation':
            # Simulate job execution at this point.
            if self.__class__.stop_sim_mode_job_submission:
                self.state.set_ready_to_submit()
            else:
                self.started_time = time.time()
                self.summary['started_time'] = self.started_time
                self.summary['started_time_string'] = (
                    get_time_string_from_unix_time(self.started_time))
                self.state.set_executing()
            return

        self.submitted_time = time.time()

        self.summary['started_time'] = None
        self.summary['started_time_string'] = None
        self.started_time = None
        self.summary['finished_time'] = None
        self.summary['finished_time_string'] = None
        self.finished_time = None

        self.summary['submitted_time'] = self.submitted_time
        self.summary['submitted_time_string'] = (
            get_time_string_from_unix_time(self.submitted_time))
        self.summary['submit_method_id'] = self.submit_method_id
        self.summary['latest_message'] = TASK_STATUS_SUBMITTED
        self.setup_event_handlers("submitted", 'job submitted',
                                  db_event='submission succeeded')

        if self.state.set_submit_succeeded():
            submit_timeout = self._get_events_conf('submission timeout')
            if submit_timeout:
                self.state.submission_timer_timeout = (
                    self.submitted_time + submit_timeout
                )
            else:
                self.state.submission_timer_timeout = None
            self.submission_poll_timer.set_timer()

    def job_execution_failed(self):
        """Handle a job failure."""
        self.finished_time = time.time()  # TODO: use time from message
        self.summary['finished_time'] = self.finished_time
        self.summary['finished_time_string'] = (
            get_time_string_from_unix_time(self.finished_time))
        self.db_updates_map[self.TABLE_TASK_JOBS].append({
            "run_status": 1,
            "time_run_exit": self.summary['finished_time_string'],
        })
        self.state.execution_timer_timeout = None
        if self.run_try_state.next() is None:
            # No retry lined up: definitive failure.
            # Note the TASK_STATUS_FAILED output is only added if needed.
            flags.pflag = True
            self.state.set_execution_failed()
            self.setup_event_handlers("failed", 'job failed')
        else:
            # There is a retry lined up
            timeout_str = self.run_try_state.timeout_as_str()
            delay_msg = "retrying in %s" % (
                self.run_try_state.delay_as_seconds())
            msg = "failed, %s (after %s)" % (delay_msg, timeout_str)
            self.log(INFO, "job(%02d) " % self.submit_num + msg)
            self.summary['latest_message'] = msg
            self.setup_event_handlers(
                "retry", "job failed, " + delay_msg, db_msg=delay_msg)
            self.state.set_execution_retry()

    def reset_manual_trigger(self):
        """This is called immediately after manual trigger flag used."""
        if self.manual_trigger:
            self.manual_trigger = False
            self.is_manual_submit = True
            # unset any retry delay timers
            self.run_try_state.timeout = None
            self.sub_try_state.timeout = None

    def set_from_rtconfig(self, cfg=None):
        """Populate task proxy with runtime configuration.

        Some [runtime] config requiring consistency checking on reload,
        and self variables requiring updating for the same.

        """

        if cfg:
            rtconfig = cfg
        else:
            rtconfig = self.tdef.rtconfig

        if not self.retries_configured:
            # configure retry delays before the first try
            self.retries_configured = True
            # TODO - saving the retry delay lists here is not necessary
            # (it can be handled like the polling interval lists).
            if (self.tdef.run_mode == 'live' or
                    (self.tdef.run_mode == 'simulation' and
                        not rtconfig['simulation mode']['disable retries']) or
                    (self.tdef.run_mode == 'dummy' and
                        not rtconfig['dummy mode']['disable retries'])):
                # note that a *copy* of the retry delays list is needed
                # so that all instances of the same task don't pop off
                # the same deque (but copy of rtconfig above solves this).
                self.run_try_state.delays = list(rtconfig['retry delays'])
                self.sub_try_state.delays = list(
                    rtconfig['job submission']['retry delays'])

        rrange = rtconfig['simulation mode']['run time range']
        if len(rrange) != 2:
            raise Exception("ERROR, " + self.tdef.name + ": simulation mode " +
                            "run time range should be ISO 8601-compatible")
        try:
            self.sim_mode_run_length = randrange(rrange[0], rrange[1])
        except Exception, exc:
            traceback.print_exc(exc)
            raise Exception(
                "ERROR: simulation mode task run time range must be [MIN,MAX)")

        self.event_hooks = rtconfig['event hooks']

        self.submission_poll_timer = PollTimer(
            copy(rtconfig['submission polling intervals']),
            copy(GLOBAL_CFG.get(['submission polling intervals'])),
            'submission', self.log)

        self.execution_poll_timer = PollTimer(
            copy(rtconfig['execution polling intervals']),
            copy(GLOBAL_CFG.get(['execution polling intervals'])),
            'execution', self.log)

    def register_job_logs(self, submit_num):
        """Register job logs in the runtime database.

        Return a list containing the names of the job logs.

        """
        data = []
        job_log_dir = self.get_job_log_dir(
            self.tdef.name, self.point, submit_num, self.suite_name)
        try:
            for filename in os.listdir(job_log_dir):
                try:
                    stat = os.stat(os.path.join(job_log_dir, filename))
                except OSError:
                    continue
                else:
                    data.append((stat.st_mtime, stat.st_size, filename))
        except OSError:
            pass

        rel_job_log_dir = self.get_job_log_dir(
            self.tdef.name, self.point, submit_num)
        for mtime, size, filename in data:
            self.db_inserts_map[self.TABLE_TASK_JOB_LOGS].append({
                "submit_num": submit_num,
                "filename": filename,
                "location": os.path.join(rel_job_log_dir, filename),
                "mtime": mtime,
                "size": size})

        return [datum[2] for datum in data]

    def prep_submit(self, dry_run=False, overrides=None):
        """Prepare job submission.

        Return self on a good preparation.

        """
        if self.tdef.run_mode == 'simulation' or (
                self.job_file_written and not dry_run):
            return self

        try:
            self._prep_submit_impl(overrides=overrides)
            JOB_FILE.write(self.job_conf)
            self.job_file_written = True
        except Exception, exc:
            # Could be a bad command template.
            if flags.debug:
                traceback.print_exc()
            self.command_log(SuiteProcContext(
                self.JOB_SUBMIT, '(prepare job file)', err=exc,
                ret_code=1))
            self.job_submission_failed()
            return

        if dry_run:
            # This will be shown next to submit num in gcylc:
            self.summary['latest_message'] = 'job file written for edit-run'
            self.log(WARNING, self.summary['latest_message'])

        # Return value used by "cylc submit" and "cylc jobscript":
        return self

    def _prep_submit_impl(self, overrides=None):
        """Helper for self.prep_submit."""
        self.log(DEBUG, "incrementing submit number")
        self.submit_num += 1
        self.summary['submit_num'] = self.submit_num
        self.job_file_written = False
        self.db_events_insert(event="incrementing submit number")
        self.db_inserts_map[self.TABLE_TASK_JOBS].append({
            "is_manual_submit": self.is_manual_submit,
            "try_num": self.run_try_state.num + 1,
            "time_submit": get_current_time_string(),
        })

        local_job_log_dir, common_job_log_path = self._create_job_log_path(
            new_mode=True)
        local_jobfile_path = os.path.join(
            local_job_log_dir, common_job_log_path)

        if overrides:
            rtconfig = pdeepcopy(self.tdef.rtconfig)
            poverride(rtconfig, overrides)
        else:
            rtconfig = self.tdef.rtconfig

        self.set_from_rtconfig(rtconfig)

        # construct the job_sub_method here so that a new one is used if
        # the task is re-triggered by the suite operator - so it will
        # get new stdout/stderr logfiles and not overwrite the old ones.

        # dynamic instantiation - don't know job sub method till run time.
        self.batch_sys_name = rtconfig['job submission']['method']
        self.summary['batch_sys_name'] = self.batch_sys_name

        command = rtconfig['script']
        if self.tdef.run_mode == 'dummy':
            # (dummy tasks don't detach)
            command = rtconfig['dummy mode']['script']
            if rtconfig['dummy mode']['disable pre-script']:
                precommand = None
            if rtconfig['dummy mode']['disable post-script']:
                postcommand = None
        else:
            precommand = rtconfig['pre-script']
            postcommand = rtconfig['post-script']

        if self.tdef.suite_polling_cfg:
            # generate automatic suite state polling script
            comstr = "cylc suite-state " + \
                     " --task=" + self.tdef.suite_polling_cfg['task'] + \
                     " --point=" + str(self.point) + \
                     " --status=" + self.tdef.suite_polling_cfg['status']
            if rtconfig['suite state polling']['user']:
                comstr += " --user=" + rtconfig['suite state polling']['user']
            if rtconfig['suite state polling']['host']:
                comstr += " --host=" + rtconfig['suite state polling']['host']
            if rtconfig['suite state polling']['interval']:
                comstr += " --interval=" + str(int(
                    rtconfig['suite state polling']['interval']))
            if rtconfig['suite state polling']['max-polls']:
                comstr += (
                    " --max-polls=" +
                    str(rtconfig['suite state polling']['max-polls']))
            if rtconfig['suite state polling']['run-dir']:
                comstr += (
                    " --run-dir=" +
                    str(rtconfig['suite state polling']['run-dir']))
            if rtconfig['suite state polling']['template']:
                comstr += (
                    " --template=" +
                    str(rtconfig['suite state polling']['template']))
            comstr += " " + self.tdef.suite_polling_cfg['suite']
            command = "echo " + comstr + "\n" + comstr

        # Determine task host settings now, just before job submission,
        # because dynamic host selection may be used.

        # host may be None (= run task on suite host)
        self.task_host = get_task_host(rtconfig['remote']['host'])
        if self.task_host != "localhost":
            self.log(INFO, "Task host: " + self.task_host)

        self.task_owner = rtconfig['remote']['owner']

        if self.task_owner:
            self.user_at_host = self.task_owner + "@" + self.task_host
        else:
            self.user_at_host = self.task_host
        self.summary['host'] = self.user_at_host
        self.submission_poll_timer.set_host(self.task_host)
        self.execution_poll_timer.set_host(self.task_host)

        RemoteJobHostManager.get_inst().init_suite_run_dir(
            self.suite_name, self.user_at_host)

        self.db_updates_map[self.TABLE_TASK_STATES].append({
            "time_updated": get_current_time_string(),
            "submit_method": self.batch_sys_name,
            "host": self.user_at_host,
            "submit_num": self.submit_num})
        self._populate_job_conf(
            rtconfig, local_jobfile_path, common_job_log_path)
        self.job_conf.update(
            {
                'pre-script': precommand,
                'script': command,
                'post-script': postcommand,
            }.items()
        )
        self.db_updates_map[self.TABLE_TASK_JOBS].append({
            "user_at_host": self.user_at_host,
            "batch_sys_name": self.batch_sys_name,
        })
        self.is_manual_submit = False

    def submit(self):
        """Queue my job to the multiprocessing pool."""

        self.state.set_ready_to_submit()

        # Reset flag so any re-triggering will generate a new job file.
        self.job_file_written = False

        cmd_key = self.JOB_SUBMIT
        args = [self.job_conf['job file path']]
        stdin_file_paths = [self.job_conf['local job file path']]

        cmd = ["cylc", cmd_key]
        if cylc.flags.debug:
            cmd.append("--debug")
        remote_mode = False
        for key, value, test_func in [
                ('host', self.task_host, is_remote_host),
                ('user', self.task_owner, is_remote_user)]:
            if test_func(value):
                cmd.append('--%s=%s' % (key, value))
                remote_mode = True
        if remote_mode:
            cmd.append('--remote-mode')
        cmd.append("--")
        cmd += list(args)

        self.log(INFO, "job(%02d) initiate %s" % (self.submit_num, cmd_key))
        ctx = SuiteProcContext(
            cmd_key, cmd, stdin_file_paths=stdin_file_paths)
        return SuiteProcPool.get_inst().put_command(
            ctx, self.job_submission_callback)

    def prep_manip(self):
        """A cut down version of prepare_submit().

        This provides access to job poll commands before the task is submitted,
        for polling in the submitted state or on suite restart.

        """
        if self.user_at_host:
            if "@" in self.user_at_host:
                self.task_owner, self.task_host = (
                    self.user_at_host.split('@', 1))
            else:
                self.task_host = self.user_at_host
        local_job_log_dir, common_job_log_path = self._create_job_log_path()
        local_jobfile_path = os.path.join(
            local_job_log_dir, common_job_log_path)
        rtconfig = pdeepcopy(self.tdef.rtconfig)
        self._populate_job_conf(
            rtconfig, local_jobfile_path, common_job_log_path)

    def _populate_job_conf(
            self, rtconfig, local_jobfile_path, common_job_log_path):
        """Populate the configuration for submitting or manipulating a job."""
        self.batch_sys_name = rtconfig['job submission']['method']
        self.job_conf = OrderedDictWithDefaults({
            'suite name': self.suite_name,
            'task id': self.identity,
            'batch system name': rtconfig['job submission']['method'],
            'directives': rtconfig['directives'],
            'init-script': rtconfig['init-script'],
            'env-script': rtconfig['env-script'],
            'runtime environment': rtconfig['environment'],
            'remote suite path': (
                rtconfig['remote']['suite definition directory']),
            'job script shell': rtconfig['job submission']['shell'],
            'batch submit command template': (
                rtconfig['job submission']['command template']),
            'work sub-directory': rtconfig['work sub-directory'],
            'pre-script': '',
            'script': '',
            'post-script': '',
            'namespace hierarchy': self.tdef.namespace_hierarchy,
            'submission try number': self.sub_try_state.num + 1,
            'try number': self.run_try_state.num + 1,
            'absolute submit number': self.submit_num,
            'is cold-start': self.tdef.is_coldstart,
            'owner': self.task_owner,
            'host': self.task_host,
            'common job log path': common_job_log_path,
            'local job file path': local_jobfile_path,
            'job file path': local_jobfile_path,
        }.items())

        # Locations of logfiles for gcylc
        # Locations can now be derived from self.summary['job_hosts']
        # This should reduce the number of unique strings that need to be
        # stored in memory.
        # self.summary['logfiles'] retained for backward compat
        logfiles = self.summary['logfiles']
        logfiles.append(local_jobfile_path)

        if not self.job_conf['host']:
            self.job_conf['host'] = 'localhost'

        try:
            self.job_conf['batch system conf'] = self._get_host_conf(
                'batch systems')[self.job_conf['batch system name']]
        except (TypeError, KeyError):
            self.job_conf['batch system conf'] = self.job_conf.__class__()
        if (is_remote_host(self.job_conf['host']) or
                is_remote_user(self.job_conf['owner'])):
            remote_job_log_dir = GLOBAL_CFG.get_derived_host_item(
                self.suite_name,
                'suite job log directory',
                self.task_host,
                self.task_owner)

            remote_path = os.path.join(
                remote_job_log_dir, self.job_conf['common job log path'])

            # Used in command construction:
            self.job_conf['job file path'] = remote_path

            # Record paths of remote log files for access by gui
            # N.B. Need to consider remote log files in shared file system
            #      accessible from the suite daemon, mounted under the same
            #      path or otherwise.
            prefix = self.job_conf['host'] + ':' + remote_path
            self.summary['job_hosts'][self.submit_num] = self.job_conf['host']
            if self.job_conf['owner']:
                prefix = self.job_conf['owner'] + "@" + prefix
                self.summary['job_hosts'][self.submit_num] = (
                    self.job_conf['owner'] + "@" + self.job_conf['host'])
            logfiles.append(prefix + '.out')
            logfiles.append(prefix + '.err')
        else:
            # Record paths of local logfiles for access by gui
            logfiles.append(self.job_conf['job file path'] + '.out')
            logfiles.append(self.job_conf['job file path'] + '.err')
            self.summary['job_hosts'][self.submit_num] = 'localhost'

    def handle_submission_timeout(self):
        """Handle submission timeout, only called if TASK_STATUS_SUBMITTED."""
        msg = 'job submitted %s ago, but has not started' % (
            get_seconds_as_interval_string(
                self.event_hooks['submission timeout'])
        )
        self.log(WARNING, msg)
        self.setup_event_handlers('submission timeout', msg)

    def handle_execution_timeout(self):
        """Handle execution timeout, only called if if TASK_STATUS_RUNNING."""
        if self.event_hooks['reset timer']:
            # the timer is being re-started by put messages
            msg = 'last message %s ago, but job not finished'
        else:
            msg = 'job started %s ago, but has not finished'
        msg = msg % get_seconds_as_interval_string(
            self.event_hooks['execution timeout'])
        self.log(WARNING, msg)
        self.setup_event_handlers('execution timeout', msg)

    def sim_time_check(self):
        """Check simulation time."""
        timeout = self.started_time + self.sim_mode_run_length
        if time.time() > timeout:
            if self.tdef.rtconfig['simulation mode']['simulate failure']:
                self.message_queue.put(
                    self.identity, 'NORMAL', TASK_STATUS_SUBMITTED)
                self.message_queue.put(
                    self.identity, 'CRITICAL', TASK_STATUS_FAILED)
            else:
                self.message_queue.put(
                    self.identity, 'NORMAL', TASK_STATUS_SUBMITTED)
                self.message_queue.put(
                    self.identity, 'NORMAL', TASK_STATUS_SUCCEEDED)
            return True
        else:
            return False

    def reject_if_failed(self, message):
        """Reject a message if in the failed state.

        Handle 'enable resurrection' mode.

        """
        if self.state.status == TASK_STATUS_FAILED:
            if self.tdef.rtconfig['enable resurrection']:
                self.log(
                    WARNING,
                    'message receive while failed:' +
                    ' I am returning from the dead!'
                )
                return False
            else:
                self.log(
                    WARNING,
                    'rejecting a message received while in the failed state:'
                )
                self.log(WARNING, '  ' + message)
            return True
        else:
            return False

    def process_incoming_message(
            self, (priority, msg_in), msg_was_polled=False):
        """Parse an incoming task message and update task state.

        Incoming is e.g. "succeeded at <TIME>".

        Correctly handle late (out of order) message which would otherwise set
        the state backward in the natural order of events.

        """

        # Log incoming messages with '>' to distinguish non-message log entries
        log_msg = '(current:%s)> %s' % (self.state.status, msg_in)
        if msg_was_polled:
            log_msg += ' %s' % self.POLLED_INDICATOR
        self.log(self.LOGGING_LVL_OF.get(priority, INFO), log_msg)

        # Strip the "at TIME" suffix.
        msg = self.MESSAGE_SUFFIX_RE.sub('', msg_in)

        # always update the suite state summary for latest message
        self.summary['latest_message'] = msg
        if msg_was_polled:
            self.summary['latest_message'] += " %s" % self.POLLED_INDICATOR
        flags.iflag = True

        if self.reject_if_failed(msg):
            # Failed tasks do not send messages unless declared resurrectable
            return

        # Check registered outputs.
        self.state.record_output(msg, msg_was_polled)

        if (msg_was_polled and
                self.state.status not in TASK_STATUSES_ACTIVE):
            # A poll result can come in after a task finishes.
            self.log(WARNING, "Ignoring late poll result: task is not active")
            return

        if priority == TaskMessage.WARNING:
            self.setup_event_handlers('warning', msg, db_update=False)

        if self._get_events_conf('reset timer'):
            # Reset execution timer on incoming messages
            execution_timeout = self._get_events_conf('execution timeout')
            if execution_timeout:
                self.state.execution_timer_timeout = (
                    time.time() + execution_timeout
                )

        elif (msg == TASK_OUTPUT_STARTED and
                self.state.status in [TASK_STATUS_READY, TASK_STATUS_SUBMITTED,
                                      TASK_STATUS_SUBMIT_FAILED]):
            if self.job_vacated:
                self.job_vacated = False
                self.log(WARNING, "Vacated job restarted: " + msg)
            # Received a 'task started' message
            flags.pflag = True
            self.state.set_executing()
            self.started_time = time.time()  # TODO: use time from message
            self.summary['started_time'] = self.started_time
            self.summary['started_time_string'] = (
                get_time_string_from_unix_time(self.started_time))
            self.db_updates_map[self.TABLE_TASK_JOBS].append({
                "time_run": self.summary['started_time_string']})
            execution_timeout = self._get_events_conf('execution timeout')
            if execution_timeout:
                self.state.execution_timer_timeout = (
                    self.started_time + execution_timeout
                )
            else:
                self.state.execution_timer_timeout = None

            # submission was successful so reset submission try number
            self.sub_try_state.num = 0
            self.setup_event_handlers('started', 'job started')
            self.execution_poll_timer.set_timer()

        elif (msg == TASK_OUTPUT_SUCCEEDED and
                self.state.status in [
                    TASK_STATUS_READY, TASK_STATUS_SUBMITTED,
                    TASK_STATUS_SUBMIT_FAILED, TASK_STATUS_RUNNING,
                    TASK_STATUS_FAILED]):
            # Received a 'task succeeded' message
            self.state.execution_timer_timeout = None
            self.state.hold_on_retry = False
            flags.pflag = True
            self.finished_time = time.time()
            self.summary['finished_time'] = self.finished_time
            self.summary['finished_time_string'] = (
                get_time_string_from_unix_time(self.finished_time))
            self.db_updates_map[self.TABLE_TASK_JOBS].append({
                "run_status": 0,
                "time_run_exit": self.summary['finished_time_string'],
            })
            # Update mean elapsed time only on task succeeded.
            self.tdef.update_mean_total_elapsed_time(
                self.started_time, self.finished_time)
            self.setup_event_handlers("succeeded", "job succeeded")
            self.state.set_execution_succeeded(msg_was_polled)

        elif (msg == TASK_OUTPUT_FAILED and
                self.state.status in [
                    TASK_STATUS_READY, TASK_STATUS_SUBMITTED,
                    TASK_STATUS_SUBMIT_FAILED, TASK_STATUS_RUNNING]):
            # (submit- states in case of very fast submission and execution).
            self.job_execution_failed()

        elif msg.startswith(TaskMessage.FAIL_MESSAGE_PREFIX):
            # capture and record signals sent to task proxy
            self.db_events_insert(event="signaled", message=msg)
            signal = msg.replace(TaskMessage.FAIL_MESSAGE_PREFIX, "")
            self.db_updates_map[self.TABLE_TASK_JOBS].append(
                {"run_signal": signal})

        elif msg.startswith(TaskMessage.VACATION_MESSAGE_PREFIX):
            flags.pflag = True
            self.state.set_state(TASK_STATUS_SUBMITTED)
            self.db_events_insert(event="vacated", message=msg)
            self.state.execution_timer_timeout = None
            # TODO - check summary item value compat with GUI:
            self.summary['started_time'] = None
            self.summary['started_time_string'] = None
            self.sub_try_state.num = 0
            self.job_vacated = True

        elif msg == "submission failed":
            # This can arrive via a poll.
            self.state.submission_timer_timeout = None
            self.job_submission_failed()

        else:
            # Unhandled messages. These include:
            #  * general non-output/progress messages
            #  * poll messages that repeat previous results
            # Note that all messages are logged already at the top.
            self.log(DEBUG, '(current: %s) unhandled: %s' % (
                self.state.status, msg))
            if priority in [CRITICAL, ERROR, WARNING, INFO, DEBUG]:
                priority = getLevelName(priority)
            self.db_events_insert(
                event=("message %s" % str(priority).lower()), message=msg)

    def spawn(self, state):
        """Spawn the successor of this task proxy."""
        self.has_spawned = True
        next_point = self.next_point()
        if next_point:
            return TaskProxy(
                self.tdef, next_point, state, False, self.stop_point,
                message_queue=self.message_queue)
        else:
            # next_point instance is out of the sequence bounds
            return None

    def ready_to_spawn(self):
        """Spawn successor on any state beyond submit (except submit-failed, to
        prevent multi-spawning a task with bad job submission config).

        Allows successive instances to run in parallel, but not out of order.

        """
        if self.tdef.is_coldstart:
            self.has_spawned = True
        return (not self.has_spawned and
                self.state.is_greater_than(TASK_STATUS_READY) and
                self.state.status != TASK_STATUS_SUBMIT_FAILED)

    def get_state_summary(self):
        """Return a dict containing the state summary of this task proxy."""
        self.summary['state'] = self.state.status
        self.summary['spawned'] = str(self.has_spawned)
        self.summary['mean total elapsed time'] = (
            self.tdef.mean_total_elapsed_time)
        return self.summary

    def next_point(self):
        """Return the next cycle point."""
        p_next = None
        adjusted = []
        for seq in self.tdef.sequences:
            nxt = seq.get_next_point(self.point)
            if nxt:
                # may be None if beyond the sequence bounds
                adjusted.append(nxt)
        if adjusted:
            p_next = min(adjusted)
        return p_next

    def _create_job_log_path(self, new_mode=False):
        """Return a new job log path on the suite host, in two parts.

        /part1/part2

        * part1: the top level job log directory on the suite host.
        * part2: the rest, which is also used on remote task hosts.

        The full local job log directory is created if necessary, and its
        parent symlinked to NN (submit number).

        """

        suite_job_log_dir = GLOBAL_CFG.get_derived_host_item(
            self.suite_name, "suite job log directory")

        the_rest_dir = os.path.join(
            str(self.point), self.tdef.name, "%02d" % int(self.submit_num))
        the_rest = os.path.join(the_rest_dir, "job")

        local_log_dir = os.path.join(suite_job_log_dir, the_rest_dir)

        if new_mode:
            try:
                rmtree(local_log_dir)
            except OSError:
                pass

        mkdir_p(local_log_dir)
        target = os.path.join(os.path.dirname(local_log_dir), "NN")
        try:
            os.unlink(target)
        except OSError:
            pass
        try:
            os.symlink(os.path.basename(local_log_dir), target)
        except OSError as exc:
            if not exc.filename:
                exc.filename = target
            raise exc
        return suite_job_log_dir, the_rest
