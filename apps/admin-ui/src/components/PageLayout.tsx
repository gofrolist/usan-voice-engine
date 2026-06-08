import { Outlet } from "react-router-dom";

// Layout route for the simple (non-editor) pages: the scrolling, centered, max-width
// body. AppLayout's <main> no longer scrolls or pads, so this owns both. The editor
// route opts out of this layout and renders full-height directly in <main>.
export function PageLayout() {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-6xl px-8 py-7">
        <Outlet />
      </div>
    </div>
  );
}
