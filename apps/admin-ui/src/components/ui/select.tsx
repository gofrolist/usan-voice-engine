import { forwardRef, type SelectHTMLAttributes } from "react";
import { cn } from "../../lib/cn";

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...props }, ref) {
    return (
      <select
        ref={ref}
        className={cn(
          "w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500",
          className,
        )}
        {...props}
      >
        {children}
      </select>
    );
  },
);
