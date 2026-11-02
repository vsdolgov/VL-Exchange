"""Synthetic color-based multi-model interoperability example, not PPE analytics."""

class Scenario:
    def __init__(self, *, target_label, min_count):
        self.target_label = target_label
        self.min_count = min_count

    def process(self, inputs, context, state, services):
        frame = inputs["frame"]
        request = {"frame_id": frame["frame_id"], "frame_ref": frame["frame_ref"],
                   "object_id": None, "bbox": None}
        detections = services.infer("detector", request)["detections"]
        tracked = services.track("tracker", {"frame_id": frame["frame_id"],
                                 "detections": detections}, state["tracker"])
        streaks, events = {}, []
        for track in tracked["tracks"]:
            request = {"frame_id": frame["frame_id"], "frame_ref": frame["frame_ref"],
                       "object_id": track["object_id"], "bbox": track["bbox"]}
            a = services.infer("classifier_a", request)["classification"]
            b = services.infer("classifier_b", request)["classification"]
            prior = state["streaks"].get(track["track_id"], 0)
            matches = a["label"] == self.target_label and b["label"] == self.target_label
            count = min(prior + 1, 1000000) if matches else 0
            streaks[track["track_id"]] = count
            if matches and prior < self.min_count <= count:
                events.append({"event_type": "org.vlexchange.example.color-alert.v1", "payload": {
                    "frame_id": frame["frame_id"], "track_id": track["track_id"],
                    "classifier_a": a["label"], "classifier_b": b["label"], "count": count}})
        return {"events": events, "state": {"tracker": tracked["state"], "streaks": streaks}}
