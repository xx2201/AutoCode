import { mergeWorkEvent } from "../../conversation.js";

export const initialRunState = Object.freeze({
  streamText: "",
  stage: "",
  work: [],
  startedAt: 0,
  activeTurnId: "",
});

export function runStateReducer(state, action) {
  switch (action.type) {
    case "begin":
      return {
        ...initialRunState,
        stage: "queued",
        startedAt: action.startedAt,
        activeTurnId: action.turnId || "",
      };
    case "reset_timeline":
      return {
        ...state,
        streamText: "",
        work: [],
      };
    case "append_token":
      return {
        ...state,
        streamText: state.streamText + (action.text || ""),
      };
    case "clear_stream":
      return {
        ...state,
        streamText: "",
      };
    case "accept_work":
      if (action.event.phase === "guidance") {
        const precedingWork = state.streamText
          ? mergeWorkEvent(state.work, {
              phase: "narrative",
              work_id: `${action.event.work_id || "guidance"}-preceding-response`,
              content: state.streamText,
            })
          : state.work;
        return {
          ...state,
          streamText: "",
          work: mergeWorkEvent(precedingWork, action.event),
        };
      }
      return {
        ...state,
        streamText: action.event.phase === "narrative" ? "" : state.streamText,
        work: mergeWorkEvent(state.work, action.event),
      };
    case "set_stage":
      return { ...state, stage: action.stage || "" };
    case "set_turn":
      return { ...state, activeTurnId: action.turnId || "" };
    case "reset":
      return { ...initialRunState };
    default:
      throw new Error(`Unknown run state action: ${action.type}`);
  }
}
