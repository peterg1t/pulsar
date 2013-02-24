import subprocess
import os
import shutil
import thread
import platform
from threading import Lock

from lwr.util import kill_pid, JobDirectory

JOB_FILE_SUBMITTED = "submitted"
JOB_FILE_CANCELLED = "cancelled"
JOB_FILE_PID = "pid"
JOB_FILE_RETURN_CODE = "return_code"
JOB_FILE_STANDARD_OUTPUT = "stdout"
JOB_FILE_STANDARD_ERROR = "stderr"
JOB_FILE_TOOL_ID = "tool_id"
JOB_FILE_TOOL_VERSION = "tool_version"

JOB_DIRECTORY_INPUTS = "inputs"
JOB_DIRECTORY_OUTPUTS = "outputs"
JOB_DIRECTORY_WORKING = "working"
JOB_DIRECTORY_CONFIGS = "configs"
JOB_DIRECTORY_TOOL_FILES = "tool"


class Manager(object):
    """
    A simple job manager that just directly runs jobs as given (no
    queueing). Preserved for compatibilty with older versions of LWR
    client code where Galaxy is used to maintain queue (like Galaxy's
    local job runner).

    >>> import tempfile
    >>> from lwr.util import Bunch
    >>> from lwr.tools.authorization import get_authorizer
    >>> staging_directory = tempfile.mkdtemp()
    >>> shutil.rmtree(staging_directory)
    >>> class PersistedJobStore:
    ...     def next_id(self):
    ...         yield 1
    ...         yield 2
    >>> app = Bunch(staging_directory=staging_directory, persisted_job_store=PersistedJobStore(), authorizer=get_authorizer(None))
    >>> manager = Manager('_default_', app)
    >>> assert os.path.exists(staging_directory)
    >>> command = "python -c \\"import sys; sys.stdout.write('Hello World!'); sys.stderr.write('moo')\\""
    >>> job_id = manager.setup_job("123", "tool1", "1.0.0")
    >>> manager.launch(job_id, command)
    >>> while not manager.check_complete(job_id): pass
    >>> manager.return_code(job_id)
    0
    >>> manager.stdout_contents(job_id)
    'Hello World!'
    >>> manager.stderr_contents(job_id)
    'moo'
    >>> manager.clean_job_directory(job_id)
    >>> os.listdir(staging_directory)
    []
    >>> job_id = manager.setup_job("124", "tool1", "1.0.0")
    >>> command = "python -c \\"import time; time.sleep(10000)\\""
    >>> manager.launch(job_id, command)
    >>> import time
    >>> time.sleep(0.1)
    >>> manager.kill(job_id)
    >>> manager.kill(job_id)  # Make sure kill doesn't choke if pid doesn't exist
    >>> while not manager.check_complete(job_id): pass
    >>> manager.clean_job_directory(job_id)
    """
    manager_type = "unqueued"

    def __init__(self, name, app, **kwds):
        self.name = name
        self.setup_staging_directory(app.staging_directory)
        self.job_locks = dict({})
        self.authorizer = app.authorizer

    def setup_staging_directory(self, staging_directory):
        assert not staging_directory == None
        if not os.path.exists(staging_directory):
            os.makedirs(staging_directory)
        assert os.path.isdir(staging_directory)
        self.staging_directory = staging_directory

    def __job_directory(self, job_id):
        return JobDirectory(self.staging_directory, job_id)

    def __read_job_file(self, job_id, name):
        return self.__job_directory(job_id).read_file(name)

    def __write_job_file(self, job_id, name, contents):
        self.__job_directory(job_id).write_file(name, contents)

    def _record_submission(self, job_id):
        self.__write_job_file(job_id, JOB_FILE_SUBMITTED, 'true')

    def _record_cancel(self, job_id):
        self.__write_job_file(job_id, JOB_FILE_CANCELLED, 'true')

    def _is_cancelled(self, job_id):
        return self.__job_directory(job_id).contains_file(JOB_FILE_CANCELLED)

    def _record_pid(self, job_id, pid):
        self.__write_job_file(job_id, JOB_FILE_PID, str(pid))

    def get_pid(self, job_id):
        pid = None
        try:
            pid = self.__read_job_file(job_id, JOB_FILE_PID)
            if pid != None:
                pid = int(pid)
        except:
            pass
        return pid

    def __get_authorization(self, job_id, tool_id=None):
        job_directory = self.__job_directory(job_id)
        if tool_id is None and job_directory.contains_file(JOB_FILE_TOOL_ID):
            tool_id = job_directory.read_file(JOB_FILE_TOOL_ID)
        return self.authorizer.get_authorization(tool_id)

    def setup_job(self, input_job_id, tool_id, tool_version):
        job_id = self._register_job(input_job_id, True)

        authorization = self.__get_authorization(job_id, tool_id)
        authorization.authorize_setup()

        job_directory = self.__job_directory(job_id)
        job_directory.setup()
        for directory in [JOB_DIRECTORY_INPUTS,
                          JOB_DIRECTORY_WORKING,
                          JOB_DIRECTORY_OUTPUTS,
                          JOB_DIRECTORY_CONFIGS,
                          JOB_DIRECTORY_TOOL_FILES]:
            job_directory.make_directory(directory)

        tool_id = str(tool_id) if tool_id else ""
        tool_version = str(tool_version) if tool_version else ""

        job_directory.write_file(JOB_FILE_TOOL_ID, tool_id)
        job_directory.write_file(JOB_FILE_TOOL_VERSION, tool_version)
        return job_id

    def _get_job_id(self, galaxy_job_id):
        return str(galaxy_job_id)

    def _register_job(self, job_id, new=True):
        if new:
            galaxy_job_id = job_id
            job_id = self._get_job_id(galaxy_job_id)
        self.job_locks[job_id] = Lock()
        return job_id

    def _unregister_job(self, job_id):
        del self.job_locks[job_id]

    def _get_job_lock(self, job_id):
        return self.job_locks[job_id]

    def clean_job_directory(self, job_id):
        job_directory = self.job_directory(job_id)
        if os.path.exists(job_directory):
            try:
                shutil.rmtree(job_directory)
            except:
                pass
        self._unregister_job(job_id)

    def job_directory(self, job_id):
        return os.path.join(self.staging_directory, job_id)

    def working_directory(self, job_id):
        return os.path.join(self.job_directory(job_id), JOB_DIRECTORY_WORKING)

    def inputs_directory(self, job_id):
        return os.path.join(self.job_directory(job_id), JOB_DIRECTORY_INPUTS)

    def outputs_directory(self, job_id):
        return os.path.join(self.job_directory(job_id), JOB_DIRECTORY_OUTPUTS)

    def configs_directory(self, job_id):
        return os.path.join(self.job_directory(job_id), JOB_DIRECTORY_CONFIGS)

    def tool_files_directory(self, job_id):
        return os.path.join(self.job_directory(job_id), JOB_DIRECTORY_TOOL_FILES)

    def check_complete(self, job_id):
        return not self.__job_directory(job_id).contains_file(JOB_FILE_SUBMITTED)

    def return_code(self, job_id):
        return int(self.__read_job_file(job_id, JOB_FILE_RETURN_CODE))

    def stdout_contents(self, job_id):
        return self.__read_job_file(job_id, JOB_FILE_STANDARD_OUTPUT)

    def stderr_contents(self, job_id):
        return self.__read_job_file(job_id, JOB_FILE_STANDARD_ERROR)

    def get_status(self, job_id):
        with self._get_job_lock(job_id):
            return self._get_status(job_id)

    def _get_status(self, job_id):
        job_directory = self.__job_directory(job_id)
        if self._is_cancelled(job_id):
            return 'cancelled'
        elif job_directory.contains_file(JOB_FILE_PID):
            return 'running'
        elif job_directory.contains_file(JOB_FILE_SUBMITTED):
            return 'queued'
        else:
            return 'complete'

    def __check_pid(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def is_windows(self):
        return platform.system() == 'Windows'

    def kill(self, job_id):
        with self._get_job_lock(job_id):
            status = self._get_status(job_id)
            if status not in ['running', 'queued']:
                return

            pid = self.get_pid(job_id)
            if pid == None:
                self._record_cancel(job_id)
                self.__job_directory(job_id).remove_file(JOB_FILE_SUBMITTED)

        if pid:
            kill_pid(pid)

    def _monitor_execution(self, job_id, proc, stdout, stderr):
        try:
            proc.wait()
            stdout.close()
            stderr.close()
            return_code = proc.returncode
            self.__write_job_file(job_id, JOB_FILE_RETURN_CODE, str(return_code))
        finally:
            with self._get_job_lock(job_id):
                self._finish_execution(job_id)

    def _finish_execution(self, job_id):
        self.__job_directory(job_id).remove_file(JOB_FILE_SUBMITTED)
        self.__job_directory(job_id).remove_file(JOB_FILE_PID)

    def _run(self, job_id, command_line, async=True):
        with self._get_job_lock(job_id):
            if self._is_cancelled(job_id):
                return
        working_directory = self.working_directory(job_id)
        preexec_fn = None
        if not self.is_windows():
            preexec_fn = os.setpgrp
        stdout = self.__job_directory(job_id).open_file(JOB_FILE_STANDARD_OUTPUT, 'w')
        stderr = self.__job_directory(job_id).open_file(JOB_FILE_STANDARD_ERROR, 'w')
        proc = subprocess.Popen(args=command_line,
                                shell=True,
                                cwd=working_directory,
                                stdout=stdout,
                                stderr=stderr,
                                preexec_fn=preexec_fn)
        with self._get_job_lock(job_id):
            self._record_pid(job_id, proc.pid)
        if async:
            thread.start_new_thread(self._monitor_execution, (job_id, proc, stdout, stderr))
        else:
            self._monitor_execution(job_id, proc, stdout, stderr)

    def launch(self, job_id, command_line):
        self._record_submission(job_id)
        self._run(job_id, command_line)

__all__ = [Manager]
