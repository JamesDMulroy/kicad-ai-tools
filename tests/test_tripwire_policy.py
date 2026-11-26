import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location("tripwire_conftest", Path(__file__).with_name("conftest.py"))
conftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conftest)


def make_tripwire(scan, children, kill_tree=lambda pid: None):
    return conftest.SessionTripwire(scan=scan, children=children, kill_tree=kill_tree)


def test_unobservable_desktop_without_launches_passes():
    tripwire = make_tripwire(lambda roots: None, lambda: [])
    tripwire._sample()
    record = tripwire.stop()

    passed, line = conftest.tripwire_verdict(record)

    assert passed
    assert "not observable" in line


def test_unobservable_desktop_after_launch_fails():
    tripwire = make_tripwire(lambda roots: None, lambda: [1234])
    tripwire._sample()
    record = tripwire.stop()

    passed, _ = conftest.tripwire_verdict(record)

    assert not passed


def test_visible_window_fails_and_kills_offending_pid():
    killed = []
    tripwire = make_tripwire(
        lambda roots: [{"pid": 5678, "visible": True}],
        lambda: [],
        kill_tree=killed.append,
    )
    tripwire._sample()
    record = tripwire.stop()

    passed, _ = conftest.tripwire_verdict(record)

    assert not passed
    assert killed == [5678]
