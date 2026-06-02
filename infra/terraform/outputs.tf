output "vm_external_ip" {
  description = "Static public IP. Create a DNS A record for your API domain pointing here, and point Telnyx inbound SIP at this IP:5060."
  value       = google_compute_address.usan.address
}

output "ssh_command" {
  description = "SSH into the VM."
  value       = "ssh ${var.ssh_user}@${google_compute_address.usan.address}"
}

output "secret_name" {
  description = "Secret Manager secret to populate with the production .env contents (see Task 6)."
  value       = google_secret_manager_secret.env.secret_id
}
