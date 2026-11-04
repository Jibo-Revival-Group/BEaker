"""S71jibo-ota-os-only skips the stock final services check only after a finished os-only update."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "tools" / "os-overlay" / "etc" / "init.d" / "S71jibo-ota-os-only"
NODE6 = "/root/node6/node-v6.17.1-linux-x64/bin"  # Jibo's os runs Node 6


def run(state, arg="start"):
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "ota.json"
        if state is not None:
            path.write_text(state if isinstance(state, str) else json.dumps(state))
        env = dict(os.environ, OTA_STATE=str(path))
        env["NODE"] = NODE6 + "/node"
        subprocess.run(["sh", str(HOOK), arg], env=env, check=True, capture_output=True)
        return path.exists()


def main() -> None:
    os_item = {"subsystem": "os", "step": "verify", "from": "3.3.4", "to": "13.0.0"}
    assert not run({"idx": None, "items": [os_item]}), "finished os-only update must skip the final check"
    assert run({"idx": 0, "items": [os_item]}), "the os verify step still needs the state file"
    assert run({"idx": 0, "items": [dict(os_item, step="retry")]})
    assert run({"idx": None, "items": [os_item, {"subsystem": "services"}]}), "services updates keep the final check"
    assert run({"idx": None, "items": [{"subsystem": "@be/be"}]})
    assert run("{not json"), "unreadable state is left for the stock updater"
    assert run({"idx": None, "items": [os_item]}, arg="stop"), "only acts at boot (start)"
    assert not run(None)
    print("ok")


if __name__ == "__main__":
    main()
