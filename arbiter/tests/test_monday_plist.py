import plistlib
from pathlib import Path


def test_monday_plist_valid_and_scheduled():
    p = Path("deploy/com.arbiter.monday.plist")
    data = plistlib.loads(p.read_bytes())
    assert data["Label"] == "com.arbiter.monday"
    assert data["RunAtLoad"] is False and data["KeepAlive"] is False
    cal = data["StartCalendarInterval"]
    assert cal["Weekday"] == 1 and cal["Hour"] == 8 and cal["Minute"] == 0
    assert data["ProgramArguments"][-1] == "monday-refresh"
