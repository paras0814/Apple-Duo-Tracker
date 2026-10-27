"""Start, stop, and inspect the local monitor without PowerShell policy changes."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib import request
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-discord", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    cfg_path = ROOT / "config.local.json"
    if not cfg_path.exists():
        shutil.copyfile(ROOT / "config.example.json", cfg_path)
        print("Created config.local.json. Enter a Discord webhook there to enable alerts.")
    if args.test:
        import unittest
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / "tests")))
        return 0 if result.wasSuccessful() else 1
    import monitor
    try:
        cfg = monitor.load_config(cfg_path)
    except (ValueError, OSError):
        print("Invalid config.local.json. Compare it with config.example.json.")
        return 1
    if args.once or args.test_discord:
        sys.argv = [str(ROOT / "monitor.py"), "--once" if args.once else "--test-discord"]
        monitor.main()
        return 0
    url = f'http://127.0.0.1:{cfg["dashboard_port"]}'
    opener = request.build_opener(request.ProxyHandler({}))
    def healthy():
        try:
            with opener.open(url + "/api/health", timeout=1) as response:
                return json.load(response).get("discord_mode") == "private-channel webhook"
        except (OSError, ValueError):
            return False
    if args.stop:
        if not healthy():
            print("Monitor is already stopped.")
            return 0
        req = request.Request(url + "/api/stop", data=b"{}", headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=2):
            pass
        for _ in range(48):
            if not (ROOT / ".data/monitor.pid").exists():
                print("Duo Radar stopped.")
                return 0
            time.sleep(0.25)
        print("Shutdown requested; a pending network operation is finishing.")
        return 0
    if not healthy():
        data = ROOT / ".data"
        data.mkdir(exist_ok=True)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with (data / "stdout.log").open("ab") as out, (data / "monitor.log").open("ab") as err:
            child = subprocess.Popen([sys.executable, "-u", str(ROOT / "monitor.py")], cwd=ROOT,
                                     env=environment, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                     creationflags=flags, start_new_session=os.name != "nt")
        for _ in range(32):
            if healthy():
                break
            if child.poll() is not None:
                print("Monitor failed to start. See .data/monitor.log.")
                return 1
            time.sleep(0.25)
        else:
            print("Monitor did not become ready. See .data/monitor.log.")
            return 1
    print(f"Monitor is running: {url}")
    print("Use Stop.cmd to stop it. Keep the PC awake for continuous monitoring.")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
