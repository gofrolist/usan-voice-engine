import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import type { ContactCreate, ContactDetail, ContactUpdate } from "../../types/api";
import { useCreateContact, useUpdateContact } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

function Field({ htmlFor, children }: { htmlFor: string; children: string }) {
  return (
    <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor={htmlFor}>
      {children}
    </label>
  );
}

interface ContactFormDialogProps {
  mode: "create" | "edit";
  contact?: ContactDetail; // required when mode === "edit"
  onClose: () => void;
}

// Hand-rolled useState form (codebase convention — no RHF). On edit the phone field
// starts EMPTY (the raw number is never sent to the browser); leaving it blank omits
// phone_e164 from the PATCH. Server 409/422 surface inline below the form.
export function ContactFormDialog({ mode, contact, onClose }: ContactFormDialogProps) {
  const isEdit = mode === "edit";
  const [name, setName] = useState(contact?.name ?? "");
  const [phone, setPhone] = useState(""); // always starts blank
  const [timezone, setTimezone] = useState(contact?.timezone ?? "");
  const [externalId, setExternalId] = useState(contact?.external_id ?? "");
  const [preferredVoice, setPreferredVoice] = useState(contact?.preferred_voice ?? "");
  const [metadataText, setMetadataText] = useState(
    contact && Object.keys(contact.metadata).length > 0
      ? JSON.stringify(contact.metadata, null, 2)
      : "",
  );
  const [localError, setLocalError] = useState<string | null>(null);

  const create = useCreateContact();
  const update = useUpdateContact();
  const busy = create.isPending || update.isPending;
  const serverError =
    (create.error as ApiError | null)?.detail ?? (update.error as ApiError | null)?.detail;

  function parseMetadata(): Record<string, unknown> | undefined {
    const t = metadataText.trim();
    if (t.length === 0) return undefined;
    let parsed: unknown;
    try {
      parsed = JSON.parse(t);
    } catch {
      throw new Error("Metadata must be a valid JSON object.");
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("Metadata must be a valid JSON object.");
    }
    return parsed as Record<string, unknown>;
  }

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    const trimmedName = name.trim();
    if (trimmedName.length === 0) {
      setLocalError("Name is required.");
      return;
    }
    const wantsPhone = phone.trim().length > 0;
    if ((!isEdit || wantsPhone) && !E164.test(phone.trim())) {
      setLocalError("Phone must be E.164 format, e.g. +19495551234.");
      return;
    }
    if (timezone.trim().length === 0) {
      setLocalError("Timezone is required.");
      return;
    }
    let metadata: Record<string, unknown> | undefined;
    try {
      metadata = parseMetadata();
    } catch (err) {
      setLocalError((err as Error).message);
      return;
    }

    if (isEdit && contact) {
      const body: ContactUpdate = { name: trimmedName };
      if (wantsPhone) body.phone_e164 = phone.trim();
      body.timezone = timezone.trim();
      body.external_id = externalId.trim() || null;
      body.preferred_voice = preferredVoice.trim() || null;
      if (metadata !== undefined) body.metadata = metadata;
      update.mutate({ id: contact.id, body }, { onSuccess: onClose });
      return;
    }

    const body: ContactCreate = {
      name: trimmedName,
      phone_e164: phone.trim(),
      timezone: timezone.trim(),
    };
    if (externalId.trim()) body.external_id = externalId.trim();
    if (preferredVoice.trim()) body.preferred_voice = preferredVoice.trim();
    if (metadata !== undefined) body.metadata = metadata;
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={isEdit ? "Edit contact" : "New contact"}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <Field htmlFor="cf-name">Name</Field>
          <Input id="cf-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Field htmlFor="cf-phone">Phone</Field>
          <Input
            id="cf-phone"
            placeholder="+19495551234 (E.164)"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
          {isEdit ? (
            <p className="mt-1 text-xs text-slate-500">
              Current: {contact?.masked_phone}. Leave blank to keep it; enter a full number to
              replace.
            </p>
          ) : null}
        </div>
        <div>
          <Field htmlFor="cf-tz">Timezone</Field>
          <Input
            id="cf-tz"
            placeholder="America/New_York"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
          />
        </div>
        <div>
          <Field htmlFor="cf-ext">External ID</Field>
          <Input id="cf-ext" value={externalId} onChange={(e) => setExternalId(e.target.value)} />
        </div>
        <div>
          <Field htmlFor="cf-voice">Preferred voice</Field>
          <Input
            id="cf-voice"
            value={preferredVoice}
            onChange={(e) => setPreferredVoice(e.target.value)}
          />
        </div>
        <div>
          <Field htmlFor="cf-meta">Metadata (advanced, JSON object)</Field>
          <Textarea
            id="cf-meta"
            rows={3}
            placeholder="{}"
            value={metadataText}
            onChange={(e) => setMetadataText(e.target.value)}
          />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {isEdit ? (busy ? "Saving…" : "Save") : busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
