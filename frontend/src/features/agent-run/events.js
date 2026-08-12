export function handleRunEvent(event, {
  run,
  onResult,
  onTurnLifecycle = () => {},
  errorMessage,
}) {
  if (event.type === "turn" && event.phase === "queued_starting") {
    onTurnLifecycle(event);
    run.resetTimeline();
  } else if (event.type === "turn" && event.phase === "started" && event.queued) {
    onTurnLifecycle(event);
  } else if (
    event.type === "turn_message"
    && event.phase === "consumed"
    && event.mode === "steer"
  ) {
    onTurnLifecycle(event);
    run.acceptWork({
      type: "work",
      phase: "guidance",
      work_id: event.message_id,
      content: event.content,
    });
  } else if (event.type === "token") {
    run.appendToken(event.text);
  } else if (event.type === "tombstone") {
    run.clearStream();
  } else if (event.type === "stage") {
    run.setStage(event.stage || "");
  } else if (event.type === "work") {
    run.acceptWork(event);
  } else if (event.type === "result") {
    onResult(event.data, event.timings || null);
  } else if (event.type === "error") {
    const error = new Error(event.error || errorMessage);
    error.status = event.status_code;
    throw error;
  }

  const turnId = event.turn_id
    || event.expected_turn_id
    || (event.type === "turn" ? event.turn_id : event.details?.turn_id);
  if (turnId) run.setTurn(turnId);
}
