"""Real SDK ordering: response starts, tools finish, caller barges in, item arrives."""

from app.livekit_runtime import worker
from app.services.call_disposition import (
    apply_grounding_quality_guard,
    normalize_call_analysis,
    summarize_runtime_grounding,
)


def telemetry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    return clock, worker._LiveKitRuntimeTelemetry(
        runtime_metrics={}, end_to_end_samples=[], opened_at=1.0
    )


def test_tool_completion_is_not_the_assistant_generation_start(monkeypatch):
    clock, t = telemetry(monkeypatch)
    t.on_final_transcript("How many doctors are listed in your directory?")
    trace = t.begin_knowledge_lookup()
    clock[0] = 103.0
    t.record_knowledge_lookup(elapsed_ms=3000, result="verified", originating_trace=trace)
    clock[0] = 110.0
    t.on_assistant_content(
        "I couldn't confirm the exact number of doctors.", created_at=100.5, item_id="reply-1"
    )
    assert trace["grounding_response_item_id"] == "reply-1"
    assert trace["reported_missing_information"] is True
    assert trace["response_action"] == "reported_missing_information"
    assert not t.runtime_metrics.get("ignored_stale_assistant_item_count")


def test_late_interrupted_item_updates_its_old_turn_not_the_next_question(monkeypatch):
    clock, t = telemetry(monkeypatch)
    t.on_final_transcript("Do you offer fillers and Botox?")
    old = t.begin_knowledge_lookup()
    clock[0] = 103.0
    t.record_knowledge_lookup(elapsed_ms=3000, result="verified", originating_trace=old)
    t.on_agent_state(new_state="speaking")
    clock[0] = 110.0
    t.on_user_state(old_state="listening", new_state="speaking", agent_state="speaking")
    t.on_final_transcript("Which doctor can I consult?")
    t.commit_suspended_interruption()
    new = t.begin_knowledge_lookup()
    clock[0] = 113.0
    t.record_knowledge_lookup(elapsed_ms=3000, result="verified", originating_trace=new)
    t.on_assistant_content(
        "Fillers are listed. However, I couldn't confirm Botox.",
        created_at=100.5,
        item_id="old-reply",
        interrupted=True,
    )
    assert old["reported_missing_information"] is True
    assert old["grounding_response_observation"] == "assistant_item_interrupted"
    assert "reported_missing_information" not in new
    assert t.pending_grounding_trace is new
    t.on_agent_state(new_state="speaking")
    t.on_assistant_content("Dr Amina Ali is listed.", created_at=110.5, item_id="new-reply")
    assert new["grounding_response_item_id"] == "new-reply"
    counts = summarize_runtime_grounding({"runtime": t.runtime_metrics})
    assert counts["reported_missing_information"] == 1
    analysis = normalize_call_analysis(
        {"resolution": "resolved", "confidence": 0.9, "disposition": "information_provided"},
        profile="general",
    )
    details = apply_grounding_quality_guard(analysis, grounding=counts)["disposition_details"]
    assert details["resolution"] == "partially_resolved"
    assert details["needs_review"] is True


def test_old_unmapped_timestamp_never_contaminates_new_tool_result(monkeypatch):
    _, t = telemetry(monkeypatch)
    t.on_final_transcript("Where is the clinic?")
    trace = t.begin_knowledge_lookup()
    t.record_knowledge_lookup(elapsed_ms=10, result="verified", originating_trace=trace)
    t.on_assistant_content("I couldn't confirm the previous question.", created_at=90.0)
    assert "reported_missing_information" not in trace
    assert t.pending_grounding_trace is trace
