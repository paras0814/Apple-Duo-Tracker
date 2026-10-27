"""Check the actual registered REA stdio server without changing configuration."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def main():
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    server = config["mcp_servers"]["rea"]
    env = dict(os.environ)
    for key, value in server.get("env", {}).items():
        for old in list(env):
            if old.lower() == key.lower():
                del env[old]
        env[key] = str(value)
    command = [server["command"], *server.get("args", [])]
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    messages = queue.Queue()
    diagnostics = []
    def consume():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                diagnostics.append("Non-JSON stdout line received")
    def stderr():
        for line in process.stderr:
            diagnostics.append(line.strip()[:500])
    threading.Thread(target=consume, daemon=True).start()
    threading.Thread(target=stderr, daemon=True).start()
    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
    def rpc(identifier, method, params):
        send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(f"MCP error during {method}: {message['error']}")
                return message["result"]
        raise TimeoutError(method)
    try:
        initialized = rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                           "clientInfo": {"name": "nj-duo-install-verifier", "version": "1.0"}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tool_list = rpc(2, "tools/list", {})
        names = [t["name"] for t in tool_list["tools"]]
        assert "binary_session" in names and "analyze_javascript_application" in names
        session = rpc(3, "tools/call", {"name": "binary_session", "arguments": {}})
        assert not session.get("isError"), "binary_session returned a tool error"
        (ROOT / "research/rea-mcp-session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
        result = {"verified": True, "server": initialized["serverInfo"],
                  "protocol": initialized["protocolVersion"], "tool_count": len(names),
                  "javascript_analysis_advertised": True, "binary_session_succeeded": True,
                  "codex_session_reload_required": True}
        (ROOT / "research/rea-mcp-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
