import { useCallback, useReducer } from "react";

import { initialRunState, runStateReducer } from "./run-state";

export default function useAgentRun() {
  const [state, dispatch] = useReducer(runStateReducer, initialRunState);

  const begin = useCallback((turnId = "") => {
    dispatch({ type: "begin", turnId, startedAt: Date.now() });
  }, []);
  const resetTimeline = useCallback(() => {
    dispatch({ type: "reset_timeline" });
  }, []);
  const appendToken = useCallback((text) => {
    dispatch({ type: "append_token", text });
  }, []);
  const acceptWork = useCallback((event) => {
    dispatch({ type: "accept_work", event });
  }, []);
  const setStage = useCallback((stage) => {
    dispatch({ type: "set_stage", stage });
  }, []);
  const setTurn = useCallback((turnId) => {
    dispatch({ type: "set_turn", turnId });
  }, []);
  const reset = useCallback(() => {
    dispatch({ type: "reset" });
  }, []);

  return {
    ...state,
    acceptWork,
    appendToken,
    begin,
    reset,
    resetTimeline,
    setStage,
    setTurn,
  };
}
