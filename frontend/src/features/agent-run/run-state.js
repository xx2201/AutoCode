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
    case "accept_work":
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
