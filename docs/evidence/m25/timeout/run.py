from dpagent.pipelines import fixture
from dpagent.cli import main

original = fixture.run_fixture

def with_short_timeout(*args, **kwargs):
    kwargs["wait_timeout"] = 60.0
    kwargs["poll_interval"] = 1.0
    return original(*args, **kwargs)

fixture.run_fixture = with_short_timeout
main()
