"""Observer lifecycle tests use disposable state and a fake VM, never host execution."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from udb.core import Agent, PolicyError
from udb import sandbox


@pytest.fixture
def observer(tmp_path, monkeypatch):
    from udb import resources
    monkeypatch.setattr(resources, 'guard_new_heavy_task', lambda *a: None)
    agent = Agent(tmp_path/'bridge')
    source = tmp_path/'source'
    source.mkdir()
    (source/'hello.py').write_text('value = 1\n', encoding='utf-8')
    agent.register_local('demo', str(source), True)
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    (runtime/'python.exe').write_bytes(b'FAKE RUNTIME - NEVER EXECUTED')
    (agent.state/'runtime.json').write_text(json.dumps({'python_runtime':str(runtime)}))
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    targets = []

    class DeferredThread:
        def __init__(self, target, daemon):
            targets.append(target)
        def start(self):
            pass

    class FakeVM:
        code = None
        terminated = False
        def poll(self):
            return self.code
        def terminate(self):
            self.terminated = True
            self.code = 1

    vm = FakeVM()
    def popen(argv, shell):
        assert shell is False
        assert Path(argv[0]).name == 'WindowsSandbox.exe'
        assert Path(argv[1]).suffix == '.wsb'
        return vm

    monkeypatch.setattr(sandbox.threading, 'Thread', DeferredThread)
    monkeypatch.setattr(sandbox.subprocess, 'Popen', popen)
    payload = {'project':'demo', 'root':str(source), 'language':'python', 'script':'print("observer proof")\n',
               'manifest':agent.snapshot('demo'), 'timeout':5}
    sandbox.launch(agent, 'a'*32, payload)
    return agent, source, payload, vm, targets[0]


def test_observer_records_result_before_closing_and_blocks_second_vm(observer, monkeypatch):
    agent, source, payload, vm, monitor = observer
    folder = agent.state/'jobs'/('a'*32)
    (folder/'output'/'result.json').write_text(json.dumps({'exit_code':0}))
    seen = []
    def wait(_seconds):
        status = agent.job_status('a'*32)
        seen.append(status['status'])
        assert status['status'] == 'reviewing'
        assert status['info']['result']['exit_code'] == 0
        with pytest.raises(PolicyError, match='One Sandbox job'):
            sandbox.launch(agent, 'b'*32, payload)
        vm.code = 0
    monkeypatch.setattr(sandbox.time, 'sleep', wait)
    monitor()
    assert seen == ['reviewing']
    assert agent.job_status('a'*32)['status'] == 'completed'
    assert (folder/'input'/'task.py').read_text() == payload['script']
    assert (source/'hello.py').read_text() == 'value = 1\n'
    assert not vm.terminated


def test_observer_closed_before_result_is_not_success(observer):
    agent, _, _, vm, monitor = observer
    vm.code = 0
    monitor()
    assert agent.job_status('a'*32)['status'] == 'failed'


def test_observer_output_limit_stops_only_owned_vm(observer):
    agent, _, _, vm, monitor = observer
    (agent.state/'jobs'/('a'*32)/'output'/'output.log').write_bytes(b'x'*(8*1024*1024+1))
    monitor()
    assert agent.job_status('a'*32)['status'] == 'output_limit'
    assert vm.terminated


def test_observer_timeout_stops_owned_vm(observer, monkeypatch):
    agent, _, _, vm, monitor = observer
    ticks = iter((0, 1000))
    monkeypatch.setattr(sandbox.time, 'monotonic', lambda: next(ticks))
    monitor()
    assert agent.job_status('a'*32)['status'] == 'timeout'
    assert vm.terminated


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell parser required')
@pytest.mark.parametrize('language', ['python','powershell'])
def test_guest_runner_parses_without_executing(tmp_path, language):
    script = tmp_path/'guest-runner.ps1'
    script.write_text(sandbox.build_runner(language), encoding='utf-8-sig')
    env = os.environ.copy()
    env['UDB_PARSER_INPUT'] = str(script)
    command = ("$tokens=$null;$errors=$null;"
               "[System.Management.Automation.Language.Parser]::ParseFile($env:UDB_PARSER_INPUT,[ref]$tokens,[ref]$errors) | Out-Null;"
               "if($errors.Count){$errors | ForEach-Object {$_.Message};exit 1}")
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                            env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
