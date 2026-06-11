import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";

// Mirrors apps/api/src/usan_api/schemas/custom_variables.py. Definitions are
// documentation/UX only — values arrive per call via dynamic_vars, never here.
export interface CustomVariable {
  id: string;
  name: string;
  description: string;
  example: string;
  phi: boolean;
  created_at: string;
  updated_at: string;
}

export interface CustomVariableCreate {
  name: string;
  description: string;
  example: string;
  phi: boolean;
}

// name is immutable after create (delete + recreate instead). The server PATCH
// schema is extra="forbid", so a name field here would 422.
export interface CustomVariableUpdate {
  description?: string;
  example?: string;
  phi?: boolean;
}

const KEY = ["custom-variables"] as const;
// useVariableCatalog()'s 5-minute staleTime assumes a slow-moving catalog; CRUD
// must invalidate it too so the editor palette/warnings refresh immediately.
const CATALOG_KEY = ["variable-catalog"] as const;

export function useCustomVariables() {
  return useQuery<CustomVariable[]>({
    queryKey: KEY,
    queryFn: () => api.get<CustomVariable[]>("/v1/admin/custom-variables"),
  });
}

export function useCreateCustomVariable() {
  const qc = useQueryClient();
  return useMutation<CustomVariable, ApiError, CustomVariableCreate>({
    mutationFn: (body) => api.post<CustomVariable>("/v1/admin/custom-variables", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KEY });
      void qc.invalidateQueries({ queryKey: CATALOG_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useUpdateCustomVariable() {
  const qc = useQueryClient();
  return useMutation<CustomVariable, ApiError, { id: string; body: CustomVariableUpdate }>({
    mutationFn: ({ id, body }) => api.patch<CustomVariable>(`/v1/admin/custom-variables/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KEY });
      void qc.invalidateQueries({ queryKey: CATALOG_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useDeleteCustomVariable() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/custom-variables/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KEY });
      void qc.invalidateQueries({ queryKey: CATALOG_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
