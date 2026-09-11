"""QoS: priority configuration/readback and invalid-priority rejection."""

from bench.common import connected, entry


def run(options):
    with connected(options) as (server, client, startup):
        results = []
        for priority in (0, 3, 7, 8, -1):
            header, reply, started, finished = client.rpc("/qos", {"priority": priority})
            results.append({"requested": priority, **reply.param})
        client.rpc("/qos", {"priority": 0})
    valid = all(item["applied"] and item["priority"] == item["requested"] for item in results[:3])
    invalid_rejected = all(not item["applied"] for item in results[3:])
    return "partial" if valid and invalid_rejected else "fail", {
        "priority_probes": results, "invalid_values_rejected": invalid_rejected,
        "wire_dscp_capture": "not_tested", "congestion_priority_effect": "not_tested",
        "dds_style_policy_equivalence": "not_assumed"}, [
        "Source evidence: vsoa/server.py Client.priority and vsoa/sockopt.py priority",
        "API readback and successful setsockopt do not prove switch scheduling or on-wire DSCP"]


if __name__ == "__main__":
    raise SystemExit(entry("qos", "feature", __doc__, run))
