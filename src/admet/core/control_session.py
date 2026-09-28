"""Asynchronous human actions against the same MCP owner used by the agent."""

from queue import Empty, Queue
import threading

from admet.mcp.client import OwnerClient


class ControlSession:
    def __init__(self, directory, client_factory=OwnerClient):
        self.client = client_factory(directory)
        self.results = Queue()
        self.pending = False
        self.notice = "Attaching…"
        self.library = []
        self.library_index = 0
        self.plan = None
        self.reviewed_id = None
        self.closed = False
        self.submit("observe", {})

    def submit(self, name, arguments):
        if self.pending:
            self.notice = "Request pending; emergency stop remains available"
            return False
        if self.closed:
            return False
        self.pending = True
        self.notice = f"{name}…"

        def request():
            try:
                result = self.client.call(name, arguments)
                self.results.put((name, result, None))
            except Exception as exc:
                self.client.close()
                self.results.put((name, None, str(exc)))

        threading.Thread(target=request, name="ControlRequest", daemon=True).start()
        return True

    def poll(self):
        try:
            name, result, error = self.results.get_nowait()
        except Empty:
            return None
        self.pending = False
        if error:
            self.notice = f"REFUSED: {error}"
            return "error"
        self.notice = "Attached" if name == "observe" else f"{name}: done"
        if name == "list_protocols":
            self.library = result["protocols"]
            self.library_index = 0
            return "library"
        if name == "plan_protocol_file":
            self.plan = result
            self.reviewed_id = None
            return "review"
        if name == "control_protocol":
            yielded = result.get("yield", result)
            self.notice = str(yielded.get("reason") or yielded.get("status") or "Action accepted")
        return "updated"

    def review(self, observation):
        plans = observation.get("planned_protocols") or []
        selected_id = self.plan.get("plan_id") if self.plan else None
        self.plan = next((p for p in plans if p.get("plan_id") == selected_id), None)
        self.plan = self.plan or next((p for p in plans if p.get("state") == "planned"), None)
        self.plan = self.plan or next((p for p in plans if p.get("state") == "executing"), None)
        self.reviewed_id = None
        if self.plan is None:
            self.notice = "No current plan; open a saved protocol or ask the agent to plan one"
            return False
        return True

    def next_plan(self, observation):
        plans = observation.get("planned_protocols") or []
        if not plans:
            return
        current = next((i for i, p in enumerate(plans)
                        if self.plan and p["plan_id"] == self.plan["plan_id"]), -1)
        self.plan = plans[(current + 1) % len(plans)]
        self.reviewed_id = None

    def execute(self):
        if not self.plan or self.reviewed_id != self.plan["plan_id"]:
            self.notice = "Review the plan before executing"
            return False
        if self.plan["state"] != "planned":
            self.notice = f"This plan is {self.plan['state']}; create a fresh plan to repeat it"
            return False
        return self.submit("control_protocol", {
            "action": "execute", "plan_id": self.plan["plan_id"], "timeout_s": 0.1,
        })

    def action(self, action):
        return self.submit("control_protocol", {"action": action, "timeout_s": 0.1})

    def open_selected(self):
        if not self.library:
            self.notice = "No saved protocols; ask the agent to save one"
            return
        item = self.library[self.library_index]
        if item.get("error"):
            self.notice = item["error"]
            return
        self.submit("plan_protocol_file", {"path": item["path"]})

    def close(self):
        self.closed = True
        self.client.close()
