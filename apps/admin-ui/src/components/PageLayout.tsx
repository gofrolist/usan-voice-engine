import { Outlet } from "react-router-dom";

// Layout route for the simple (non-editor) pages: the scrolling, centered, max-width
// body. AppLayout's <main> no longer scrolls or pads, so this owns both. Padding is
// fluid (tighter on mobile). The editor route opts out and renders full-height directly.
export function PageLayout() {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <Outlet />
      </div>
    </div>
  );
}
