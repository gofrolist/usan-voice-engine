import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import { ContactFormDialog } from "./ContactFormDialog";
import { useContact, useDeleteContact } from "./hooks";

// Per-contact home: header + edit/delete + call-now + schedules. Call-now (Task 4)
// and the schedules section (Task 6) mount at the marked extension points.
export function ContactDetailPage() {
  const isAdmin = useIsAdmin();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const contact = useContact(id);
  const del = useDeleteContact();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;
  if (contact.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted">
        <Spinner /> Loading contact…
      </div>
    );
  }
  if (contact.isError || !contact.data) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-red-700">Contact not found.</p>
        <Link className="text-sm text-accent hover:underline" to="/contacts">
          ← Back to contacts
        </Link>
      </div>
    );
  }

  const c = contact.data;
  return (
    <div className="space-y-5">
      <Link className="text-sm text-accent hover:underline" to="/contacts">
        ← Contacts
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl text-ink-strong">{c.name}</h1>
          <p className="font-mono text-sm text-faint">{c.masked_phone}</p>
          <p className="text-sm text-muted">
            {c.timezone}
            {c.agent_profile_name ? ` · ${c.agent_profile_name}` : ""}
            {c.external_id ? ` · ${c.external_id}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {/* CALL_NOW_BUTTON — Task 4 inserts the "Call now" button here. */}
          <Button variant="secondary" onClick={() => setEditOpen(true)}>
            Edit
          </Button>
          <Button variant="danger" onClick={() => setConfirmDelete(true)}>
            Delete
          </Button>
        </div>
      </div>

      {/* SCHEDULES_SECTION — Task 6 mounts <ScheduleSection contactId={c.id} /> here. */}

      <div>
        <Link className="text-sm text-accent hover:underline" to={`/calls?contact_id=${c.id}`}>
          Recent calls →
        </Link>
      </div>

      {editOpen ? (
        <ContactFormDialog mode="edit" contact={c} onClose={() => setEditOpen(false)} />
      ) : null}
      <ConfirmDialog
        open={confirmDelete}
        title="Delete contact?"
        body={
          <>
            Delete <strong>{c.name}</strong>? Their schedules are removed and past calls are kept
            but no longer linked to a name. This cannot be undone.
          </>
        }
        confirmLabel="Delete"
        busy={del.isPending}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() =>
          del.mutate(c.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              navigate("/contacts");
            },
          })
        }
      />
    </div>
  );
}
