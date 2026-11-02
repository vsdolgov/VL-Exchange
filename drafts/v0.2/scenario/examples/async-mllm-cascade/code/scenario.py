PROMPT = 'What is the dominant color of the image? Return only a JSON object like {"color":"red"}.'

"""Synthetic multi-model and async MLLM integration probe, not safety analytics."""

class Scenario:
    def __init__(self, *, target_label, min_count, deadline_ms):
        self.target_label = target_label
        self.min_count = min_count
        self.deadline_ms = deadline_ms

    def process(self, inputs, context, state, services):
        if "mllm_result" in inputs:
            completion = inputs["mllm_result"]
            pending = state["pending"].pop(completion["job_id"], None)
            if pending is None or pending["frame_id"] != completion["frame_id"] or pending["track_id"] != completion["track_id"]:
                raise ValueError("completion does not match pending original frame and track")
            if completion["status"] != "completed" or completion["structured"] is None:
                state["answers"][completion["job_id"]] = {"frame_id":pending["frame_id"], "track_id":pending["track_id"], "status":completion["status"], "color":"unknown"}
                return {"events":[], "state":state}
            color = completion["structured"]["color"]
            state["answers"][completion["job_id"]] = {"frame_id":pending["frame_id"], "track_id":pending["track_id"], "status":"completed", "color":color}
            return {"events":[{"event_type":"org.vlexchange.example.mllm-result.v1", "payload":{
                "original_frame_id":pending["frame_id"], "track_id":pending["track_id"], "color":color}}], "state":state}

        frame = inputs["frame"]
        request = {"frame_id":frame["frame_id"], "frame_ref":frame["frame_ref"], "object_id":None, "bbox":None}
        detections = services.infer("detector", request)["detections"]
        tracked = services.track("tracker", {"frame_id":frame["frame_id"], "detections":detections}, state["tracker"])
        streaks = {}
        for track in tracked["tracks"]:
            request = {"frame_id":frame["frame_id"], "frame_ref":frame["frame_ref"], "object_id":track["object_id"], "bbox":track["bbox"]}
            a = services.infer("classifier_a", request)["classification"]
            b = services.infer("classifier_b", request)["classification"]
            previous = state["streaks"].get(track["track_id"], 0)
            matched = a["label"] == self.target_label and b["label"] == self.target_label
            count = min(previous + 1, 1000000) if matched else 0
            streaks[track["track_id"]] = count
            if matched and previous < self.min_count <= count:
                schema = {"type":"object", "required":["color"], "additionalProperties":False,
                          "properties":{"color":{"type":"string", "enum":["red","blue","green","unknown"]}}}
                receipt = services.submit_mllm({"model_alias":"vision", "frame_id":frame["frame_id"],
                    "frame_ref":frame["frame_ref"], "bbox":track["bbox"], "tracker_alias":"tracker", "track_id":track["track_id"],
                    "payload":{"text":PROMPT, "response_schema":schema, "parameters":{"max_tokens":128,"temperature":0}},
                    "deadline_ms":self.deadline_ms})
                state["pending"][receipt["job_id"]] = {"frame_id":frame["frame_id"], "track_id":track["track_id"]}
        state["tracker"], state["streaks"] = tracked["state"], streaks
        return {"events":[], "state":state}
