import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "textarea:not([disabled])",
  "select:not([disabled])",
  "a[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/** Shared modal/drawer lifecycle: focus entry, trap, Escape, scroll lock, and restore. */
export function useModalLifecycle(
  busy: boolean,
  onClose: () => void,
  containerRef: RefObject<HTMLElement>,
  initialFocusRef: RefObject<HTMLElement>,
  active = true,
): void {
  const busyRef = useRef(busy);
  const activeRef = useRef(active);
  const openerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  busyRef.current = busy;
  activeRef.current = active;
  closeRef.current = onClose;

  useEffect(() => {
    if (active && busy) containerRef.current?.focus();
  }, [active, busy, containerRef]);

  useEffect(() => {
    if (!active) return;
    if (openerRef.current === null && document.activeElement instanceof HTMLElement) {
      openerRef.current = document.activeElement;
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (!containerRef.current?.contains(document.activeElement)) initialFocusRef.current?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        containerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
      ).filter((element) =>
        !element.hidden &&
        element.getAttribute("aria-hidden") !== "true" &&
        element.closest("[hidden], [inert]") === null,
      );
      if (focusable.length === 0) {
        event.preventDefault();
        containerRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const focusInside = active !== containerRef.current && Boolean(containerRef.current?.contains(active));
      if (event.shiftKey && (active === first || !focusInside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !focusInside)) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      if (activeRef.current) {
        openerRef.current?.focus();
        openerRef.current = null;
      }
    };
  }, [active, containerRef, initialFocusRef]);

  useEffect(() => () => {
    if (openerRef.current?.isConnected) openerRef.current.focus();
    openerRef.current = null;
  }, []);
}
