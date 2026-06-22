
### NemoClaw & Agent Troubleshooting Guardrail
When troubleshooting NemoClaw sandboxes, Hermes agents, or OpenShell networking failures (e.g., missing gateway containers, proxy 403 errors, or webhook delivery failures):
1. **Fix the Flow, Don't Bypass It:** Do NOT use complex host-side hacks (like injecting `socat`, manually altering Docker networks, or manually curling API endpoints) to bypass broken sandbox infrastructure.
2. **Standard Recovery:** Rely on standard OpenShell/NemoClaw recovery commands (e.g., `nemoclaw onboard`, `nemoclaw gateway`, or having the agent reset its state).
3. **Delegate to the User/Agent:** If you cannot fix the sandbox infrastructure directly using standard tools, stop and instruct the user or the Hermes agent on what actions they need to take from their end. It is perfectly acceptable to pause and ask the user to run commands.
