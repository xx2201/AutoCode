import { useCallback, useEffect, useRef, useState } from "react";

export default function useToast() {
  const [toast, setToast] = useState("");
  const timerRef = useRef(null);

  const showToast = useCallback((message) => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    setToast(message);
    timerRef.current = window.setTimeout(() => {
      setToast("");
      timerRef.current = null;
    }, 2800);
  }, []);

  useEffect(() => () => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
  }, []);

  return { toast, showToast };
}
