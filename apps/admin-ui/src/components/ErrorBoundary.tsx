import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  // Rendered instead of children when a render/lazy-load error is thrown below.
  fallback: ReactNode;
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

// A minimal error boundary. React's <Suspense fallback> only covers the PENDING state
// of a lazy import — it does NOT catch a REJECTED import (e.g. a code-split chunk 404s
// after a deploy). Wrapping such a subtree in this boundary lets a failed lazy load
// degrade to a usable fallback instead of blanking the page.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // Intentionally swallow: the fallback renders instead. No PHI is involved and a
    // chunk-load failure is not independently actionable here.
  }

  render(): ReactNode {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}
