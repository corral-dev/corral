// Small inline icons. Stroke icons inherit currentColor.

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg class={`chev${open ? " is-open" : ""}`} viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  );
}

export function CloseIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
    </svg>
  );
}

export function SendIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path d="M8 13V3.5M3.8 7.6L8 3.4l4.2 4.2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  );
}

export function CheckCircle() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <circle cx="8" cy="8" r="7.25" fill="currentColor" />
      <path d="M4.9 8.3l2.1 2.1 4.1-4.6" fill="none" stroke="var(--on-status)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  );
}

export function Spark() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        d="M8 1.6c.35 2.9 1.5 4.3 4.6 4.75-3.1.45-4.25 1.85-4.6 4.75C7.65 8.2 6.5 6.8 3.4 6.35 6.5 5.9 7.65 4.5 8 1.6z"
        fill="currentColor"
      />
      <circle cx="12.6" cy="12.4" r="1.4" fill="currentColor" opacity=".55" />
    </svg>
  );
}
