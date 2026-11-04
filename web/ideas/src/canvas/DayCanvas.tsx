// React's ScrollingList snapshot pattern, adapted to a document-level prepend.
// Read before DOM mutation, compensate before paint, and never touch editor focus/selection.
import { Component, createRef } from "react";
import type { State, Store } from "../store";
import { DaySection } from "./DaySection";

interface Props {
  store: Store;
  state: State;
  settleMs: number;
  onSettled: (day: string, from: number, to: number) => void;
}

interface ScrollAnchor {
  element: HTMLElement;
  top: number;
}

export class DayCanvas extends Component<Props> {
  private readonly page = createRef<HTMLDivElement>();

  getSnapshotBeforeUpdate(previous: Props): ScrollAnchor | null {
    if (previous.state.sections[0]?.day === this.props.state.sections[0]?.day) return null;
    const element = this.page.current?.firstElementChild;
    return element instanceof HTMLElement ? { element, top: element.getBoundingClientRect().top } : null;
  }

  componentDidUpdate(_previous: Props, _state: unknown, anchor: ScrollAnchor | null): void {
    if (!anchor) return;
    // At page top the normal day gap is shorter than the top bar + page padding.
    // Reserve that space too, so no part of the newly prepended day enters the viewport.
    const gap = parseFloat(getComputedStyle(anchor.element).marginTop) || 0;
    anchor.element.style.setProperty("--rollover-gap", `${Math.max(gap, anchor.top + 1)}px`);
    const delta = anchor.element.getBoundingClientRect().top - anchor.top;
    const page = this.page.current;
    const scroller = document.scrollingElement;
    if (!page || !scroller || !delta) return;
    // A short draft may not fill the viewport: without trailing room, scrolling is clamped
    // and the old content still moves. Add only the missing blank extent, below the draft.
    const target = window.scrollY + delta;
    let padding = parseFloat(getComputedStyle(page).paddingBottom) || 0;
    // The first increase can consume unused body min-height rather than grow scrollHeight.
    // A second measurement accounts for that slack before the final scroll.
    for (let pass = 0; pass < 2; pass++) {
      const missing = target - (scroller.scrollHeight - window.innerHeight);
      if (missing <= 0) break;
      padding += missing + 1;
      page.style.paddingBottom = `${padding}px`;
    }
    window.scrollBy({ top: delta, behavior: "instant" });
  }

  render() {
    const { store, state, settleMs, onSettled } = this.props;
    return (
      <div className="page" ref={this.page}>
        {state.sections.map((section) => (
          <DaySection
            key={section.day}
            store={store}
            state={state}
            section={section}
            now={state.now}
            settleMs={settleMs}
            onSettled={onSettled}
          />
        ))}
      </div>
    );
  }
}
