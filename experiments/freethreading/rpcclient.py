"""Scripted `tau --mode rpc` client: get_state, one prompt, wait for agent_end, quit."""
import collections
import json
import subprocess
import sys
import time

tau, model, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
p = subprocess.Popen([tau, "--mode", "rpc", "--model", model, "--no-session", "-nc"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def send(obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()


kinds = collections.Counter()
send({"jsonrpc": "2.0", "id": 1, "method": "get_state"})
send({"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": prompt}})
t0 = time.time()
final_text = None
responses = {}
for line in p.stdout:
    msg = json.loads(line)
    if "id" in msg and ("result" in msg or "error" in msg):
        responses[msg["id"]] = "error" if "error" in msg else "ok"
        if "error" in msg:
            print("ERROR", msg["error"])
        continue
    params = msg.get("params", {})
    ev = params.get("event", params) if isinstance(params, dict) else {}
    etype = ev.get("type") if isinstance(ev, dict) else None
    kinds[etype or msg.get("method")] += 1
    if etype == "agent_end":
        break
    if time.time() - t0 > 120:
        print("TIMEOUT")
        break
send({"jsonrpc": "2.0", "id": 3, "method": "get_last_assistant_text"})
for line in p.stdout:
    msg = json.loads(line)
    if msg.get("id") == 3:
        final_text = msg.get("result")
        break
p.stdin.close()
rc = p.wait(30)
print("responses", responses)
print("events", dict(kinds))
print("last_text", json.dumps(final_text)[:300])
print("rc", rc, "stderr_tail", p.stderr.read()[-500:])
