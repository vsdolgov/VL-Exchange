"""Standalone scenario: emit once when a classification streak reaches a threshold.

This synthetic example illustrates the host interface; it does not import the
VL block runtime or require a visual editor. Production delivery is encrypted.
"""


class Scenario:
    def __init__(self, *, target_label, min_score, min_count):
        self.target_label = target_label
        self.min_score = min_score
        self.min_count = min_count

    def process(self, inputs, context, state):
        classification = inputs["classification"]
        matched = (classification["label"] == self.target_label
                   and classification["score"] >= self.min_score)
        previous_count = state["count"]
        count = min(previous_count + 1, 1_000_000) if matched else 0
        crossed = matched and previous_count < self.min_count <= count
        events = []
        if crossed:
            events.append({"event_type": "org.vlexchange.target-observed.v1",
                           "payload": {"label": classification["label"],
                                       "score": classification["score"],
                                       "count": count}})
        return {"events": events, "state": {"count": count}}
