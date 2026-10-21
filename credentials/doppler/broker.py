from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socketserver
import subprocess


def dispatch(deployment, config_directory=Path("/etc/prodenv-doppler"), runner=subprocess.run):
    if deployment not in ("fullstack-starter-stage", "fullstack-starter-prod"):
        return 403
    config = config_directory / (deployment + ".json")
    if not config.is_file() or config.is_symlink():
        return 404
    try:
        result = runner(["/usr/bin/python3", "/opt/prodenv-doppler/pilot.py", "deploy", str(config)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=1800, env={"PATH": os.defpath}, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 503
    return 200 if result.returncode == 0 else 503


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *arguments):
        pass

    def do_POST(self):
        status = 400
        try:
            length = self.headers.get("Content-Length", "")
            if self.path != "/deploy" or not re.fullmatch(r"[0-9]{1,3}", length) or not 0 < int(length) <= 256:
                raise ValueError
            request = json.loads(self.rfile.read(int(length)))
            if set(request) != {"deployment"} or not isinstance(request["deployment"], str):
                raise ValueError
            status = dispatch(request["deployment"])
        except (ValueError, TypeError):
            pass
        payload = json.dumps({"ok": status == 200, "message": "Deployment completed" if status == 200 else "Managed deployment rejected or failed; legacy fallback disabled"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    os.umask(0o077)
    path = Path("/run/prodenv-doppler/deploy.sock")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.exists():
        path.unlink()
    with socketserver.UnixStreamServer(str(path), Handler) as server:
        path.chmod(0o600)
        server.serve_forever()