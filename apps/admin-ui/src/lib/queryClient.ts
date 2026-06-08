import { QueryClient } from "@tanstack/react-query";

// retry:false so a 401 (which triggers a full-page redirect to /v1/auth/login)
// is not retried and masked. staleTime keeps the UI snappy without hammering the API.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      staleTime: 10_000,
      refetchOnWindowFocus: false,
    },
  },
});
