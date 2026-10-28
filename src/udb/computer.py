"""Grant-gated computer operations, serialized through the existing headless worker."""

from . import computer_access as access
from .computer_files import Files
from .computer_jobs import Jobs
from .concurrency import MAX_CONCURRENT
from .core import PolicyError
from .resources import require_capacity, require_light_capacity


def status_report(agent, processes=None, *, worker_ready=True):
    """Report the local grant even when the process supervisor is unavailable."""
    from .desktop_control import browser_handoff, paused

    return dict(
        desktop_paused=paused(agent) or browser_handoff(agent),
        **access.summary(agent),
        worker_ready=worker_ready,
        command_limits=dict(
            max_running=MAX_CONCURRENT,
            shared_background_limit=MAX_CONCURRENT,
            timeout_seconds=3600,
            output_bytes=4 * 1024 * 1024,
            memory_mib=4096,
        ),
        processes=processes
        if processes is not None
        else dict(jobs=[], available=False),
    )


class Computer:
    def __init__(self, agent, jobs_factory=Jobs):
        self.agent = agent
        self.files = Files(agent)
        self.desktop = None
        self.utilities = None

        def token():
            grant = access.load(agent)
            return grant["id"] if grant else None

        self.jobs = jobs_factory(agent, token)

    def dispatch(self, mode, args, resource):
        if mode == "status":
            return status_report(self.agent, self.jobs.process("list"))
        if mode == "process" and args.get("action") == "stop":
            return self.jobs.process("stop", args.get("job_id", ""))
        grant = access.require(self.agent)
        if mode == "desktop" and args.get("action") == "control_status":
            from .desktop_control import InputControl

            return (
                self.desktop.control.status()
                if self.desktop
                else InputControl(self.agent).status()
            )
        if mode == "process":
            return self.jobs.process(
                args.get("action", "read"),
                args.get("job_id", ""),
                args.get("offset", 0),
            )
        if mode == "files" or (
            mode == "desktop" and args.get("action") in ("list", "snapshot")
        ):
            require_light_capacity(resource)
        else:
            require_capacity(resource, 256 if mode == "execute" else 128)
        if mode == "utilities":
            if self.utilities is None:
                from .windows_utilities import Utilities

                self.utilities = Utilities(self.agent)
            return self.utilities.dispatch(**args)
        if mode == "files":
            return self.files.dispatch(**args)
        if mode == "execute":
            # CWD is checked against explicit protected roots as well as NTFS.
            self.files.path(args.get("cwd", ""))
            return self.jobs.start(grant=grant, **args)
        if mode == "desktop":
            if self.desktop is None:
                from .computer_desktop import Desktop
                from .desktop_control import InputControl

                self.desktop = Desktop(
                    guard=lambda: access.require(self.agent) == self.desktop_grant,
                    control=InputControl(self.agent),
                )
            self.desktop_grant = grant
            return self.desktop.dispatch(**args)
        raise PolicyError("Unknown computer operation")

    def close(self):
        self.jobs.close()
